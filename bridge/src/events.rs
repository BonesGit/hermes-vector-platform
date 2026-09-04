//! Single-client last-writer-wins SSE (`GET /events`) from `BotEvent`.

use std::convert::Infallible;
use std::path::Path;
use std::pin::Pin;
use std::sync::Mutex;
use std::task::{Context, Poll};
use std::time::Duration;

use axum::extract::State;
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

pub struct SseItem {
    pub id: Option<String>,
    pub payload: String,
}

impl SseItem {
    fn into_event(self) -> Event {
        let mut event = Event::default().data(self.payload);
        if let Some(id) = self.id {
            event = event.id(id);
        }
        event
    }
}

pub struct EventHub {
    slot: Mutex<Option<mpsc::Sender<SseItem>>>,
}

impl EventHub {
    pub fn new() -> Self {
        Self {
            slot: Mutex::new(None),
        }
    }

    pub fn connect(&self) -> mpsc::Receiver<SseItem> {
        let (tx, rx) = mpsc::channel(32);
        *self.lock() = Some(tx);
        rx
    }

    pub fn publish(&self, item: SseItem) {
        if let Some(tx) = self.lock().as_ref() {
            let _ = tx.try_send(item);
        }
    }

    pub fn disconnect_all(&self) {
        *self.lock() = None;
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Option<mpsc::Sender<SseItem>>> {
        self.slot.lock().unwrap_or_else(|e| e.into_inner())
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

pub(crate) async fn get_events(State(state): State<AppState>, _auth: Auth) -> Sse<ClientStream> {
    let rx = state.events().connect();
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
        SseItem {
            id: Some(self.id.clone()),
            payload: json!({ "type": "message", "data": self }).to_string(),
        }
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
        SseItem {
            id: Some(format!("update:{}", self.id)),
            payload: json!({ "type": "message_update", "data": self }).to_string(),
        }
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
                if !data.is_group {
                    if let Some(dir) = data_dir {
                        crate::missed::note_live(
                            dir,
                            &data.chat_id,
                            data.at_ms.max(0) as u64,
                            &data.id,
                        );
                    }
                }
                state.events().publish(data.sse_item());
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
    SseItem {
        id: Some(format!("joined:{community_id}")),
        payload: json!({
            "type": "community_joined",
            "data": {
                "community_id": community_id,
                "name": name,
                "channels": channels,
            }
        })
        .to_string(),
    }
}

pub(crate) fn delete_item(chat_id: &str, message_id: &str) -> SseItem {
    SseItem {
        id: Some(format!("delete:{message_id}")),
        payload: json!({
            "type": "message_delete",
            "data": {
                "id": message_id,
                "chat_id": chat_id,
            }
        })
        .to_string(),
    }
}

async fn community_display_name(bot: &VectorBot, community_id: &str) -> String {
    bot.core()
        .list_communities()
        .await
        .into_iter()
        .find(|v| {
            v.get("community_id").and_then(|i| i.as_str()) == Some(community_id)
        })
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
    state.events().publish(data.sse_item());
    Ok(Json(json!({ "ok": true })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use vector_sdk::Message;

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
