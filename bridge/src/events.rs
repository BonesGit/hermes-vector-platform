//! Single-client last-writer-wins SSE (`GET /events`) from `BotEvent`.
//!
//! Delivery is at-least-once within a bounded window. Every published item is
//! retained in a replay ring so a client that reconnects with `Last-Event-ID`
//! resumes where it stopped instead of losing the gap. The `missed-seen.json`
//! cursor advances only when an item is actually handed to a live client, so
//! anything the ring cannot cover is still ❌'d by the next catch-up.

use std::collections::{HashMap, HashSet, VecDeque};
use std::convert::Infallible;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::task::{Context, Poll};
use std::time::Duration;

use axum::extract::State;
use axum::http::HeaderMap;
use axum::response::sse::{Event, Sse};
use axum::Json;
use futures_util::Stream;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::time::{Instant, Interval, MissedTickBehavior};
use vector_sdk::nostr::PublicKey;
use vector_sdk::{Attachment, BotEvent, IncomingMessage, Message, Reaction, VectorBot};

use crate::api::{ready_item, ApiError, AppState, Auth, JsonBody};

/// How many recent items stay available for `Last-Event-ID` replay. This is a
/// retention bound, not a work bound — see `DEFAULT_REPLAY_MAX`.
const REPLAY_CAPACITY: usize = 256;
/// Channel depth. Must exceed `REPLAY_CAPACITY` so a full replay fits without
/// the backlog itself overflowing the queue it is being written into.
const CHANNEL_CAPACITY: usize = REPLAY_CAPACITY * 2;
/// Items replayed per reconnect unless `VECTOR_SSE_REPLAY_MAX` says otherwise.
/// Well under `REPLAY_CAPACITY`: a long outage should reach the user as ❌
/// catch-up marks, not as a wall of queued agent turns.
const DEFAULT_REPLAY_MAX: usize = 5;
/// Replay horizon unless `VECTOR_SSE_REPLAY_MAX_AGE_SECS` says otherwise.
const DEFAULT_REPLAY_MAX_AGE_SECS: usize = 600;

/// Per-message bookkeeping carried alongside the payload.
///
/// Three jobs. `track_seen` drives the missed-DM cursor, which must reflect
/// what Hermes actually received rather than what the relay handed us —
/// attaching it to the item is what lets a *replayed* message advance the
/// cursor exactly like a live one. `at_ms` lets replay skip stale messages, and
/// `chat_id` lets it collapse a burst so only the newest message per chat costs
/// an agent turn.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ItemMeta {
    pub chat_id: String,
    pub at_ms: u64,
    pub id: String,
    /// DMs advance `missed-seen.json` on delivery. Group messages do not —
    /// Concord catch-up is deliberately DM-only.
    pub track_seen: bool,
}

#[derive(Clone)]
pub struct SseItem {
    pub id: Option<String>,
    pub payload: String,
    meta: Option<ItemMeta>,
}

impl SseItem {
    pub fn new(id: Option<String>, payload: String) -> Self {
        Self {
            id,
            payload,
            meta: None,
        }
    }

    pub fn with_meta(mut self, meta: ItemMeta) -> Self {
        self.meta = Some(meta);
        self
    }

    /// Re-serialize the payload with replay flags inside `data` so the adapter
    /// can tell a replayed message from a live one, and a superseded one from
    /// the newest in its chat.
    fn mark_replayed(mut self, superseded: bool) -> Self {
        let Ok(mut parsed) = serde_json::from_str::<Value>(&self.payload) else {
            return self;
        };
        if let Some(data) = parsed.get_mut("data").and_then(Value::as_object_mut) {
            data.insert("replayed".into(), Value::Bool(true));
            if superseded {
                data.insert("superseded".into(), Value::Bool(true));
            }
            self.payload = parsed.to_string();
        }
        self
    }

    fn into_event(self) -> Event {
        let mut event = Event::default().data(self.payload);
        if let Some(id) = self.id {
            event = event.id(id);
        }
        event
    }
}

fn env_usize(key: &str, default: usize) -> usize {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .unwrap_or(default)
}

/// Cap on items replayed in one reconnect. `0` disables replay entirely, so
/// every gap falls through to the missed-DM ❌ catch-up.
fn replay_max() -> usize {
    env_usize("VECTOR_SSE_REPLAY_MAX", DEFAULT_REPLAY_MAX).min(REPLAY_CAPACITY)
}

/// Messages older than this are not replayed. Answering a long-stale DM is
/// worse than ❌-ing it: the cursor stays put, so the catch-up flags it.
fn replay_max_age_ms() -> u64 {
    env_usize(
        "VECTOR_SSE_REPLAY_MAX_AGE_SECS",
        DEFAULT_REPLAY_MAX_AGE_SECS,
    ) as u64
        * 1000
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Shape a raw backlog into what is actually worth replaying.
///
/// Three passes, in order: drop stale messages, keep only the newest
/// `replay_max` items, then flag every message that a later message in the same
/// chat supersedes. Items with no `meta` (deletes, community joins) are never
/// stale and never superseded — they are cheap and carry no turn.
fn plan_replay(backlog: Vec<SseItem>, budget: usize, horizon: u64) -> Vec<SseItem> {
    if backlog.is_empty() {
        return backlog;
    }
    if budget == 0 {
        eprintln!(
            "[vector-bridge] sse replay disabled; {} item(s) left for missed-DM catch-up",
            backlog.len()
        );
        return Vec::new();
    }

    let now = now_ms();
    let total = backlog.len();
    let mut fresh: Vec<SseItem> = backlog
        .into_iter()
        .filter(|item| match &item.meta {
            None => true,
            Some(meta) => horizon == 0 || now.saturating_sub(meta.at_ms) <= horizon,
        })
        .collect();
    let stale = total - fresh.len();

    let over_budget = fresh.len().saturating_sub(budget);
    if over_budget > 0 {
        fresh.drain(..over_budget);
    }
    if stale > 0 || over_budget > 0 {
        eprintln!(
            "[vector-bridge] sse replay trimmed: {stale} stale, {over_budget} over budget \
             (max={budget}); those stay unseen for the missed-DM catch-up"
        );
    }

    // Newest message per chat keeps its turn; everything before it is context.
    let mut newest_per_chat: HashMap<&str, usize> = HashMap::new();
    for (idx, item) in fresh.iter().enumerate() {
        if let Some(meta) = &item.meta {
            newest_per_chat.insert(meta.chat_id.as_str(), idx);
        }
    }
    let keep: HashSet<usize> = newest_per_chat.into_values().collect();
    let turns = keep.len();
    let planned: Vec<SseItem> = fresh
        .into_iter()
        .enumerate()
        .map(|(idx, item)| {
            let superseded = item.meta.is_some() && !keep.contains(&idx);
            item.mark_replayed(superseded)
        })
        .collect();

    eprintln!(
        "[vector-bridge] sse replaying {} item(s); {turns} will start a turn, the rest are context",
        planned.len()
    );
    planned
}

struct HubState {
    tx: Option<mpsc::Sender<SseItem>>,
    replay: VecDeque<SseItem>,
}

pub struct EventHub {
    state: Mutex<HubState>,
    dropped: AtomicU64,
    data_dir: Option<PathBuf>,
}

impl EventHub {
    pub fn new(data_dir: Option<PathBuf>) -> Self {
        Self {
            state: Mutex::new(HubState {
                tx: None,
                replay: VecDeque::with_capacity(REPLAY_CAPACITY),
            }),
            dropped: AtomicU64::new(0),
            data_dir,
        }
    }

    /// Attach a client. When `last_event_id` names an item still in the ring,
    /// everything after it is replayed first. An unknown id means the gap
    /// outran the ring, so the whole retained window is replayed and the
    /// client's own dedupe absorbs the overlap.
    ///
    /// Replay is deliberately cheap to consume: stale messages are skipped, the
    /// batch is capped, and all but the newest message per chat is flagged
    /// `superseded` so the adapter files it as context instead of starting an
    /// agent turn. A reconnect therefore costs about one turn per active chat,
    /// not one per queued message.
    pub fn connect(&self, last_event_id: Option<&str>) -> mpsc::Receiver<SseItem> {
        let (tx, rx) = mpsc::channel(CHANNEL_CAPACITY);
        let backlog = {
            let mut state = self.lock();
            state.tx = Some(tx.clone());
            match last_event_id {
                None => Vec::new(),
                Some(id) => {
                    let from = state
                        .replay
                        .iter()
                        .position(|item| item.id.as_deref() == Some(id))
                        .map(|pos| pos + 1);
                    match from {
                        Some(pos) => state.replay.iter().skip(pos).cloned().collect(),
                        None => {
                            eprintln!(
                                "[vector-bridge] sse resume id={id} is outside the retained \
                                 window; replaying {} item(s)",
                                state.replay.len()
                            );
                            state.replay.iter().cloned().collect()
                        }
                    }
                }
            }
        };
        for item in plan_replay(backlog, replay_max(), replay_max_age_ms()) {
            self.hand_off(&tx, item);
        }
        rx
    }

    /// Queue an item for the live client and retain it for replay. Returns
    /// whether a live client accepted it.
    ///
    /// Items with no SSE id (`ready`) are transient: `get_events` re-emits them
    /// on every connect, so they are neither retained nor counted as a loss.
    /// Without that exemption every healthy startup would report a drop, since
    /// the adapter polls `/health` before it subscribes.
    pub fn publish(&self, item: SseItem) -> bool {
        let transient = item.id.is_none();
        let tx = {
            let mut state = self.lock();
            if !transient {
                state.replay.push_back(item.clone());
                while state.replay.len() > REPLAY_CAPACITY {
                    state.replay.pop_front();
                }
            }
            state.tx.clone()
        };
        match tx {
            Some(tx) => self.hand_off(&tx, item),
            None => {
                if !transient {
                    self.log_drop(item.id.as_deref(), "no client attached");
                }
                false
            }
        }
    }

    fn hand_off(&self, tx: &mpsc::Sender<SseItem>, item: SseItem) -> bool {
        let label = item.id.clone();
        let meta = item.meta.clone();
        match tx.try_send(item) {
            Ok(()) => {
                if let Some(mark) = meta.filter(|m| m.track_seen) {
                    if let Some(dir) = self.data_dir.as_deref() {
                        crate::missed::note_live(dir, &mark.chat_id, mark.at_ms, &mark.id);
                    }
                }
                true
            }
            Err(err) => {
                let reason = match err {
                    mpsc::error::TrySendError::Full(_) => "client queue full",
                    mpsc::error::TrySendError::Closed(_) => "client went away",
                };
                if label.is_some() {
                    self.log_drop(label.as_deref(), reason);
                }
                false
            }
        }
    }

    fn log_drop(&self, id: Option<&str>, reason: &str) {
        let n = self.dropped.fetch_add(1, Ordering::Relaxed) + 1;
        eprintln!(
            "[vector-bridge] sse drop id={} ({reason}); retained for replay, dropped_total={n}",
            id.unwrap_or("-")
        );
    }

    /// Items a live client never accepted. Non-zero means Hermes missed events;
    /// replay or the missed-DM catch-up has to cover them.
    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    pub fn retained(&self) -> usize {
        self.lock().replay.len()
    }

    /// Detach the client. The replay ring is kept so a reconnect can resume.
    pub fn disconnect_all(&self) {
        self.lock().tx = None;
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, HubState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }
}

pub(crate) struct ClientStream {
    rx: mpsc::Receiver<SseItem>,
    ping: Interval,
}

impl Stream for ClientStream {
    type Item = Result<Event, Infallible>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        match this.rx.poll_recv(cx) {
            Poll::Ready(Some(item)) => Poll::Ready(Some(Ok(item.into_event()))),
            Poll::Ready(None) => Poll::Ready(None),
            Poll::Pending => match this.ping.poll_tick(cx) {
                Poll::Ready(_) => Poll::Ready(Some(Ok(Event::default().comment("ping")))),
                Poll::Pending => Poll::Pending,
            },
        }
    }
}

pub(crate) async fn get_events(
    State(state): State<AppState>,
    _auth: Auth,
    headers: HeaderMap,
) -> Sse<ClientStream> {
    let last_event_id = headers
        .get("last-event-id")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|v| !v.is_empty());
    let rx = state.events().connect(last_event_id);
    if state.is_ready().await {
        if let Some(npub) = state.npub().await {
            state.events().publish(ready_item(&npub));
        }
    }
    Sse::new(ClientStream {
        rx,
        ping: ping_interval(state.ping_interval()),
    })
}

fn ping_interval(period: Duration) -> Interval {
    let period = period.max(Duration::from_millis(1));
    let mut ping = tokio::time::interval_at(Instant::now() + period, period);
    ping.set_missed_tick_behavior(MissedTickBehavior::Delay);
    ping
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MessageEventData {
    pub id: String,
    pub chat_id: String,
    pub npub: String,
    #[serde(default)]
    pub is_group: bool,
    #[serde(default)]
    pub is_mine: bool,
    #[serde(default)]
    pub is_file: bool,
    /// Set when the sidecar's Vector slash-command handler forwarded this
    /// invocation. Adapter mention-gates skip it; people-gate still applies.
    #[serde(default, skip_serializing_if = "is_false")]
    pub is_command: bool,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub reply_to: String,
    #[serde(default)]
    pub reply_to_text: Option<String>,
    #[serde(default)]
    pub at_ms: i64,
    #[serde(default)]
    pub attachments: Vec<Attachment>,
    /// Concord community id when `is_group`. Absent for DMs / when unknown.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub community_id: Option<String>,
}

fn is_false(v: &bool) -> bool {
    !*v
}

impl MessageEventData {
    pub(crate) fn sse_item(&self) -> SseItem {
        SseItem::new(
            Some(self.id.clone()),
            json!({ "type": "message", "data": self }).to_string(),
        )
    }
}

/// Map an inbound Vector message to SSE payload. Drops `is_mine`. Empty text is
/// dropped unless the message carries attachments. Community channel messages
/// (`is_group`) are forwarded; the adapter mention-gates them.
pub(crate) fn map_incoming(incoming: &IncomingMessage) -> Option<MessageEventData> {
    if incoming.is_mine() {
        eprintln!("[vector-bridge] skip is_mine id={}", incoming.message.id);
        return None;
    }
    let has_files = incoming.is_file || !incoming.message.attachments.is_empty();
    if incoming.text().is_empty() && !has_files {
        eprintln!("[vector-bridge] skip empty id={}", incoming.message.id);
        return None;
    }
    Some(MessageEventData {
        id: incoming.message.id.clone(),
        chat_id: incoming.chat_id.clone(),
        npub: incoming
            .message
            .npub
            .clone()
            .unwrap_or_else(|| incoming.chat_id.clone()),
        is_group: incoming.is_group,
        is_mine: incoming.is_mine(),
        is_file: has_files,
        is_command: false,
        text: incoming.text().to_string(),
        reply_to: incoming.message.replied_to.clone(),
        reply_to_text: incoming.message.replied_to_content.clone(),
        at_ms: incoming.message.at as i64,
        attachments: incoming.message.attachments.clone(),
        community_id: community_id_of(incoming),
    })
}

fn community_id_of(incoming: &IncomingMessage) -> Option<String> {
    incoming.community().map(|c| c.id().to_string())
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MessageUpdateData {
    pub id: String,
    pub chat_id: String,
    pub npub: String,
    #[serde(default)]
    pub mine: bool,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub reactions: Vec<Reaction>,
}

impl MessageUpdateData {
    fn sse_item(&self) -> SseItem {
        SseItem::new(
            Some(format!("update:{}", self.id)),
            json!({ "type": "message_update", "data": self }).to_string(),
        )
    }
}

/// Map a DM `MessageUpdate` (reaction/edit snapshot). Non-DM chat ids (Concord
/// channel ids) are dropped — v1 is DM-only.
pub(crate) fn map_update(chat_id: &str, message: &Message) -> Option<MessageUpdateData> {
    if PublicKey::parse(chat_id.trim()).is_err() {
        return None;
    }
    if message.reactions.is_empty() {
        return None;
    }
    Some(MessageUpdateData {
        id: message.id.clone(),
        chat_id: chat_id.to_string(),
        npub: message.npub.clone().unwrap_or_else(|| chat_id.to_string()),
        mine: message.mine,
        text: message.content.clone(),
        reactions: message.reactions.clone(),
    })
}

pub(crate) async fn handle_bot_event(
    state: &AppState,
    bot: &VectorBot,
    event: BotEvent,
    bot_name: &str,
    bot_about: &str,
    avatar_path: Option<&Path>,
    banner_path: Option<&Path>,
    data_dir: Option<&Path>,
) {
    match event {
        BotEvent::Ready { communities } => {
            eprintln!("[vector-bridge] ready communities={communities}");
            crate::commands::register_hermes_commands(bot, state);
            crate::commands::spawn_manifest_publish();
            state.mark_ready(bot.npub()).await;
            // Public kind-0: slash picker needs bot: true on discovery
            // relays. Name/about/images stay optional (no default "Hermes").
            // Empty strings do *not* wipe a prior kind-0 (SDK merge).
            if crate::profile::should_publish_own_profile(
                crate::commands::slash_commands_enabled(),
                bot_name,
                bot_about,
                avatar_path,
                banner_path,
            ) {
                crate::profile::apply_own_profile(
                    bot,
                    bot_name,
                    bot_about,
                    avatar_path,
                    banner_path,
                    data_dir,
                )
                .await;
            }
            if let Some(dir) = data_dir.map(Path::to_path_buf) {
                let bot = bot.clone();
                tokio::spawn(async move {
                    crate::missed::ack_missed_while_down(&bot, &dir).await;
                });
            }
        }
        BotEvent::Message(msg) => {
            if let Some(data) = map_incoming(&msg) {
                // The cursor mark rides on the item so it advances on delivery
                // rather than on receipt: a message dropped during an SSE gap
                // stays unseen and the next catch-up ❌'s it. `chat_id` and
                // `at_ms` also let replay skip stale messages and collapse a
                // burst down to one turn per chat.
                let item = data.sse_item().with_meta(ItemMeta {
                    chat_id: data.chat_id.clone(),
                    at_ms: data.at_ms.max(0) as u64,
                    id: data.id.clone(),
                    track_seen: !data.is_group,
                });
                state.events().publish(item);
            }
        }
        BotEvent::MessageUpdate { chat_id, message } => {
            if let Some(data) = map_update(&chat_id, &message) {
                state.events().publish(data.sse_item());
            }
        }
        BotEvent::Delete {
            chat_id,
            message_id,
        } => {
            state.events().publish(delete_item(&chat_id, &message_id));
        }
        BotEvent::Invite { community_id } => {
            eprintln!(
                "[vector-bridge] community invite community_id={community_id} \
                 (auto-join per invite policy)"
            );
            // apply_invite_policy is spawned by the SDK, so channels() is often
            // empty on this tick. Retry briefly; parked invites stay empty.
            let bot = bot.clone();
            let state = state.clone();
            tokio::spawn(async move {
                for delay_ms in [200_u64, 800, 2000] {
                    tokio::time::sleep(Duration::from_millis(delay_ms)).await;
                    let channels = list_channel_values(&bot, &community_id).await;
                    if !channels.is_empty() {
                        let name = community_display_name(&bot, &community_id).await;
                        log_joined_channels(&community_id, &channels);
                        state.events().publish(community_joined_item(
                            &community_id,
                            &name,
                            channels,
                        ));
                        return;
                    }
                }
                eprintln!(
                    "[vector-bridge] community_id={community_id} has no channels \
                     after join wait (parked, or still joining)"
                );
            });
        }
        BotEvent::ChannelKeyed {
            community_id,
            channel_id,
            ..
        } => {
            eprintln!(
                "[vector-bridge] channel keyed channel_id={channel_id} \
                 community_id={community_id}"
            );
            let channel_name = bot
                .community(&community_id)
                .channels()
                .await
                .into_iter()
                .find(|ch| ch.id() == channel_id)
                .map(|ch| ch.name().to_string())
                .unwrap_or_default();
            let name = community_display_name(bot, &community_id).await;
            state.events().publish(community_joined_item(
                &community_id,
                &name,
                vec![json!({ "channel_id": channel_id, "name": channel_name })],
            ));
        }
        _ => {}
    }
}

fn community_joined_item(community_id: &str, name: &str, channels: Vec<Value>) -> SseItem {
    SseItem::new(
        Some(format!("joined:{community_id}")),
        json!({
            "type": "community_joined",
            "data": {
                "community_id": community_id,
                "name": name,
                "channels": channels,
            }
        })
        .to_string(),
    )
}

pub(crate) fn delete_item(chat_id: &str, message_id: &str) -> SseItem {
    SseItem::new(
        Some(format!("delete:{message_id}")),
        json!({
            "type": "message_delete",
            "data": {
                "id": message_id,
                "chat_id": chat_id,
            }
        })
        .to_string(),
    )
}

async fn community_display_name(bot: &VectorBot, community_id: &str) -> String {
    bot.core()
        .list_communities()
        .await
        .into_iter()
        .find(|v| v.get("community_id").and_then(|i| i.as_str()) == Some(community_id))
        .and_then(|v| v.get("name").and_then(Value::as_str).map(str::to_string))
        .unwrap_or_default()
}

async fn list_channel_values(bot: &VectorBot, community_id: &str) -> Vec<Value> {
    bot.community(community_id)
        .channels()
        .await
        .into_iter()
        .map(|ch| json!({ "channel_id": ch.id(), "name": ch.name() }))
        .collect()
}

fn log_joined_channels(community_id: &str, channels: &[Value]) {
    for ch in channels {
        let id = ch.get("channel_id").and_then(Value::as_str).unwrap_or("");
        let name = ch.get("name").and_then(Value::as_str).unwrap_or("");
        eprintln!("[vector-bridge] joined channel_id={id} name={name} community_id={community_id}");
    }
}

pub async fn inject(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(data): JsonBody<MessageEventData>,
) -> Result<Json<Value>, ApiError> {
    if data.is_mine {
        eprintln!("[vector-bridge] dropping inject id={}", data.id);
        return Ok(Json(json!({ "ok": true })));
    }
    // Attach the same meta the live `BotEvent::Message` path does, so replay
    // planning (age, budget, supersede) is exercised rather than bypassed.
    let item = data.sse_item().with_meta(ItemMeta {
        chat_id: data.chat_id.clone(),
        at_ms: data.at_ms.max(0) as u64,
        id: data.id.clone(),
        track_seen: !data.is_group,
    });
    state.events().publish(item);
    Ok(Json(json!({ "ok": true })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use vector_sdk::Message;

    /// `0` means "no age limit", so tests that are not about staleness do not
    /// depend on the wall clock.
    const NO_HORIZON: u64 = 0;
    /// A budget high enough never to trim, so tests about coalescing and age
    /// stay independent of whatever `DEFAULT_REPLAY_MAX` happens to be.
    const NO_BUDGET: usize = REPLAY_CAPACITY;

    fn dm_item_at(chat: &str, id: &str, at_ms: u64) -> SseItem {
        SseItem::new(
            Some(id.to_string()),
            json!({ "type": "message", "data": { "id": id, "chat_id": chat } }).to_string(),
        )
        .with_meta(ItemMeta {
            chat_id: chat.into(),
            at_ms,
            id: id.to_string(),
            track_seen: true,
        })
    }

    fn dm_item_for(chat: &str, id: &str) -> SseItem {
        dm_item_at(chat, id, now_ms())
    }

    fn dm_item(id: &str) -> SseItem {
        dm_item_for("npub1peer", id)
    }

    fn is_superseded(item: &SseItem) -> bool {
        serde_json::from_str::<Value>(&item.payload)
            .ok()
            .and_then(|v| {
                v.get("data")
                    .and_then(|d| d.get("superseded"))
                    .and_then(Value::as_bool)
            })
            .unwrap_or(false)
    }

    fn cursor(dir: &Path) -> String {
        std::fs::read_to_string(dir.join("missed-seen.json")).unwrap_or_default()
    }

    /// The core invariant: an undelivered DM must stay unseen so the next
    /// catch-up can ❌ it. Advancing the cursor here is what made lost
    /// messages invisible.
    #[test]
    fn dropped_dm_does_not_advance_the_missed_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let hub = EventHub::new(Some(dir.path().to_path_buf()));

        assert!(!hub.publish(dm_item("m1")), "no client means no delivery");

        assert_eq!(cursor(dir.path()), "", "cursor must not advance");
        assert_eq!(hub.dropped(), 1);
        assert_eq!(hub.retained(), 1, "still available for replay");
    }

    #[test]
    fn delivered_dm_advances_the_missed_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let hub = EventHub::new(Some(dir.path().to_path_buf()));
        let _rx = hub.connect(None);

        assert!(hub.publish(dm_item("m1")));

        let raw = cursor(dir.path());
        assert!(raw.contains("npub1peer"), "{raw}");
        assert!(raw.contains("m1"), "{raw}");
        assert_eq!(hub.dropped(), 0);
    }

    /// Replay is a real delivery: it hands the item over *and* advances the
    /// cursor, so a resumed message is not ❌'d later as if it were missed.
    #[test]
    fn replay_delivers_and_advances_the_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let hub = EventHub::new(Some(dir.path().to_path_buf()));

        hub.publish(dm_item("m1"));
        assert_eq!(cursor(dir.path()), "");

        let mut rx = hub.connect(Some("unknown-id"));

        assert!(rx.try_recv().is_ok(), "replay should hand over the item");
        let raw = cursor(dir.path());
        assert!(raw.contains("m1"), "{raw}");
    }

    #[test]
    fn resume_skips_items_the_client_already_acked() {
        let hub = EventHub::new(None);
        hub.publish(dm_item("m1"));
        hub.publish(dm_item("m2"));

        let mut rx = hub.connect(Some("m1"));

        let got = rx.try_recv().expect("m2 should replay");
        assert!(got.payload.contains("message"));
        assert!(rx.try_recv().is_err(), "m1 must not replay again");
    }

    #[test]
    fn replay_ring_is_bounded() {
        let hub = EventHub::new(None);
        for i in 0..(REPLAY_CAPACITY + 50) {
            hub.publish(dm_item(&format!("m{i}")));
        }
        assert_eq!(hub.retained(), REPLAY_CAPACITY);
    }

    /// The GPU-cost guarantee: a burst from one peer replays in full, but only
    /// the newest message is allowed to start an agent turn. The rest are
    /// flagged for the adapter to file as context.
    #[test]
    fn replay_collapses_a_burst_to_one_turn_per_chat() {
        let planned = plan_replay(
            vec![
                dm_item_for("npub1alice", "a1"),
                dm_item_for("npub1alice", "a2"),
                dm_item_for("npub1alice", "a3"),
                dm_item_for("npub1bob", "b1"),
            ],
            NO_BUDGET,
            NO_HORIZON,
        );

        assert_eq!(planned.len(), 4, "everything is still delivered");
        let live: Vec<&str> = planned
            .iter()
            .filter(|i| !is_superseded(i))
            .map(|i| i.id.as_deref().unwrap_or(""))
            .collect();
        assert_eq!(
            live,
            vec!["a3", "b1"],
            "only the newest per chat starts a turn"
        );
    }

    #[test]
    fn replay_marks_every_item_as_replayed() {
        let planned = plan_replay(vec![dm_item("only")], NO_BUDGET, NO_HORIZON);
        let payload: Value = serde_json::from_str(&planned[0].payload).unwrap();
        assert_eq!(payload["data"]["replayed"], true);
        assert!(payload["data"].get("superseded").is_none());
    }

    /// A long outage must not turn into a wall of queued turns. Anything past
    /// the budget stays unseen so the ❌ catch-up reports it instead.
    #[test]
    fn replay_budget_trims_the_oldest_items() {
        let planned = plan_replay(
            vec![
                dm_item_for("c1", "m1"),
                dm_item_for("c2", "m2"),
                dm_item_for("c3", "m3"),
                dm_item_for("c4", "m4"),
            ],
            2,
            NO_HORIZON,
        );
        let ids: Vec<&str> = planned
            .iter()
            .map(|i| i.id.as_deref().unwrap_or(""))
            .collect();
        assert_eq!(ids, vec!["m3", "m4"], "keeps the newest, drops the rest");
    }

    /// The README documents this number under `vector.replay.max_messages`.
    /// Fail here rather than let the docs drift away from the code.
    #[test]
    fn default_replay_budget_matches_the_docs() {
        assert_eq!(DEFAULT_REPLAY_MAX, 5);
        assert!(
            DEFAULT_REPLAY_MAX < REPLAY_CAPACITY,
            "the budget is a work bound; retention is a separate, larger bound"
        );
    }

    #[test]
    fn replay_can_be_disabled_entirely() {
        assert!(plan_replay(vec![dm_item("m1")], 0, NO_HORIZON).is_empty());
    }

    /// Stale messages are better ❌'d than answered late, so they are skipped
    /// and their cursor is left alone.
    #[test]
    fn replay_skips_messages_past_the_age_horizon() {
        let stale = now_ms() - 3_600_000;
        let planned = plan_replay(
            vec![
                dm_item_at("npub1peer", "old", stale),
                dm_item_for("npub1peer", "fresh"),
            ],
            NO_BUDGET,
            60_000,
        );
        let ids: Vec<&str> = planned
            .iter()
            .map(|i| i.id.as_deref().unwrap_or(""))
            .collect();
        assert_eq!(ids, vec!["fresh"]);
    }

    #[test]
    fn replay_keeps_items_without_meta() {
        let planned = plan_replay(vec![delete_item("npub1peer", "gone")], NO_BUDGET, 60_000);
        assert_eq!(planned.len(), 1);
        assert!(!is_superseded(&planned[0]), "deletes carry no turn");
    }

    /// `ready` has no id and is re-emitted on connect, so dropping it is not a
    /// loss. Counting it would make every healthy startup look lossy.
    #[test]
    fn transient_ready_item_is_not_counted_or_retained() {
        let hub = EventHub::new(None);
        assert!(!hub.publish(crate::api::ready_item("npub1bot")));
        assert_eq!(hub.dropped(), 0);
        assert_eq!(hub.retained(), 0);
    }

    fn incoming(
        id: &str,
        chat_id: &str,
        npub: Option<&str>,
        text: &str,
        mine: bool,
        group: bool,
        file: bool,
    ) -> IncomingMessage {
        IncomingMessage {
            chat_id: chat_id.to_string(),
            is_group: group,
            is_file: file,
            message: Message {
                id: id.to_string(),
                content: text.to_string(),
                mine,
                npub: npub.map(str::to_string),
                replied_to: "parent-id".to_string(),
                replied_to_content: Some("quoted".to_string()),
                at: 1_785_979_414_499,
                ..Default::default()
            },
        }
    }

    #[test]
    fn community_joined_payload_has_full_channel_id() {
        let community_id = "cc".repeat(32);
        let channel_id = "ab".repeat(32);
        let item = community_joined_item(
            &community_id,
            "Ada's house",
            vec![json!({ "channel_id": channel_id, "name": "general" })],
        );
        let payload: Value = serde_json::from_str(&item.payload).unwrap();
        assert_eq!(payload["type"], "community_joined");
        assert_eq!(payload["data"]["community_id"], community_id);
        assert_eq!(payload["data"]["name"], "Ada's house");
        assert_eq!(payload["data"]["channels"][0]["channel_id"], channel_id);
        assert_eq!(payload["data"]["channels"][0]["name"], "general");
    }

    #[test]
    fn delete_payload_has_chat_and_message_id() {
        let item = delete_item("npub1peer", "deadbeef");
        assert_eq!(item.id.as_deref(), Some("delete:deadbeef"));
        let payload: Value = serde_json::from_str(&item.payload).unwrap();
        assert_eq!(payload["type"], "message_delete");
        assert_eq!(payload["data"]["id"], "deadbeef");
        assert_eq!(payload["data"]["chat_id"], "npub1peer");
    }

    #[test]
    fn maps_dm_fields_from_incoming_message() {
        let msg = incoming(
            "deadbeef",
            "npub1peer",
            Some("npub1from"),
            "hello",
            false,
            false,
            false,
        );
        let data = map_incoming(&msg).expect("mapped");
        assert_eq!(
            data,
            MessageEventData {
                id: "deadbeef".into(),
                chat_id: "npub1peer".into(),
                npub: "npub1from".into(),
                is_group: false,
                is_mine: false,
                is_file: false,
                is_command: false,
                text: "hello".into(),
                reply_to: "parent-id".into(),
                reply_to_text: Some("quoted".into()),
                at_ms: 1_785_979_414_499,
                attachments: vec![],
                community_id: None,
            }
        );
        let payload: Value = serde_json::from_str(&data.sse_item().payload).unwrap();
        assert_eq!(payload["type"], "message");
        assert_eq!(payload["data"]["id"], "deadbeef");
        assert_eq!(payload["data"]["chat_id"], "npub1peer");
        assert_eq!(payload["data"]["npub"], "npub1from");
        assert_eq!(payload["data"]["text"], "hello");
        assert_eq!(payload["data"]["reply_to"], "parent-id");
        assert_eq!(payload["data"]["reply_to_text"], "quoted");
        assert_eq!(payload["data"]["at_ms"], 1_785_979_414_499u64);
        assert!(payload["data"].get("is_command").is_none());
    }

    #[test]
    fn npub_falls_back_to_chat_id() {
        let msg = incoming("id1", "npub1peer", None, "hi", false, false, false);
        let data = map_incoming(&msg).expect("mapped");
        assert_eq!(data.npub, "npub1peer");
        assert_eq!(data.chat_id, "npub1peer");
    }

    #[test]
    fn skips_mine() {
        let msg = incoming("id1", "npub1peer", None, "hi", true, false, false);
        assert!(map_incoming(&msg).is_none());
    }

    #[test]
    fn maps_group_channel_message() {
        let channel = "a".repeat(64);
        let msg = incoming(
            "id1",
            &channel,
            Some("npub1from"),
            "hi group",
            false,
            true,
            false,
        );
        let data = map_incoming(&msg).expect("mapped");
        assert!(data.is_group);
        assert_eq!(data.chat_id, channel);
        assert_eq!(data.npub, "npub1from");
        assert_eq!(data.text, "hi group");
        let payload: Value = serde_json::from_str(&data.sse_item().payload).unwrap();
        assert_eq!(payload["data"]["is_group"], true);
        assert_eq!(payload["data"]["chat_id"], channel);
    }

    #[test]
    fn skips_empty_text_without_files() {
        let empty = incoming("id1", "npub1peer", None, "", false, false, false);
        assert!(map_incoming(&empty).is_none());
    }

    #[test]
    fn maps_dm_reaction_update() {
        let peer = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6";
        let mut message = Message {
            id: "target-1".into(),
            content: "hello".into(),
            mine: true,
            npub: Some(peer.into()),
            ..Default::default()
        };
        message.reactions.push(Reaction {
            id: "react-1".into(),
            reference_id: "target-1".into(),
            author_id: peer.into(),
            emoji: "👍".into(),
            emoji_url: None,
        });
        let data = map_update(peer, &message).expect("mapped");
        assert_eq!(data.id, "target-1");
        assert!(data.mine);
        assert_eq!(data.reactions.len(), 1);
        assert_eq!(data.reactions[0].emoji, "👍");
        let payload: Value = serde_json::from_str(&data.sse_item().payload).unwrap();
        assert_eq!(payload["type"], "message_update");
        assert_eq!(payload["data"]["id"], "target-1");
        assert_eq!(payload["data"]["reactions"][0]["emoji"], "👍");
    }

    #[test]
    fn skips_update_without_reactions() {
        let peer = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6";
        let message = Message {
            id: "target-1".into(),
            content: "edited".into(),
            mine: true,
            ..Default::default()
        };
        assert!(map_update(peer, &message).is_none());
    }

    #[test]
    fn skips_update_for_community_channel_id() {
        let mut message = Message {
            id: "target-1".into(),
            content: "hi".into(),
            ..Default::default()
        };
        message.reactions.push(Reaction {
            id: "react-1".into(),
            reference_id: "target-1".into(),
            author_id: "npub1peer".into(),
            emoji: "👍".into(),
            emoji_url: None,
        });
        assert!(map_update("not-an-npub", &message).is_none());
    }

    #[test]
    fn maps_file_only_empty_caption() {
        let mut msg = incoming("id2", "npub1peer", None, "", false, false, true);
        msg.message.attachments.push(Attachment {
            id: "att1".into(),
            name: "notes.pdf".into(),
            extension: "pdf".into(),
            size: 12,
            ..Default::default()
        });
        let data = map_incoming(&msg).expect("file-only mapped");
        assert!(data.is_file);
        assert!(data.text.is_empty());
        assert_eq!(data.attachments.len(), 1);
        assert_eq!(data.attachments[0].name, "notes.pdf");
    }
}
