//! Single-client last-writer-wins SSE (`GET /events`) from `BotEvent`.

use std::convert::Infallible;
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
use vector_sdk::{BotEvent, IncomingMessage, VectorBot};

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
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub reply_to: String,
    #[serde(default)]
    pub reply_to_text: Option<String>,
    #[serde(default)]
    pub at_ms: i64,
}

impl MessageEventData {
    fn sse_item(&self) -> SseItem {
        SseItem {
            id: Some(self.id.clone()),
            payload: json!({ "type": "message", "data": self }).to_string(),
        }
    }
}

/// Map an inbound Vector message to SSE payload. Drops `is_mine`, `is_group`,
/// and empty-body events (including empty file messages).
pub(crate) fn map_incoming(incoming: &IncomingMessage) -> Option<MessageEventData> {
    if incoming.is_mine() {
        eprintln!("[vector-bridge] skip is_mine id={}", incoming.message.id);
        return None;
    }
    if incoming.is_group {
        eprintln!("[vector-bridge] skip is_group id={}", incoming.message.id);
        return None;
    }
    if incoming.text().is_empty() {
        eprintln!(
            "[vector-bridge] skip empty id={} is_file={}",
            incoming.message.id, incoming.is_file
        );
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
        is_file: incoming.is_file,
        text: incoming.text().to_string(),
        reply_to: incoming.message.replied_to.clone(),
        reply_to_text: incoming.message.replied_to_content.clone(),
        at_ms: incoming.message.at as i64,
    })
}

pub(crate) async fn handle_bot_event(
    state: &AppState,
    bot: &VectorBot,
    event: BotEvent,
    bot_name: &str,
) {
    match event {
        BotEvent::Ready { .. } => {
            state.mark_ready(bot.npub()).await;
            if !bot.update_profile(bot_name, "", "", "").await {
                eprintln!("[vector-bridge] sidecar-boot update_profile failed");
            }
        }
        BotEvent::Message(msg) => {
            if let Some(data) = map_incoming(&msg) {
                state.events().publish(data.sse_item());
            }
        }
        BotEvent::Invite { community_id } => {
            eprintln!("[vector-bridge] invite parked community_id={community_id}");
        }
        _ => {}
    }
}

pub async fn inject(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(data): JsonBody<MessageEventData>,
) -> Result<Json<Value>, ApiError> {
    if data.is_group || data.is_mine {
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
                text: "hello".into(),
                reply_to: "parent-id".into(),
                reply_to_text: Some("quoted".into()),
                at_ms: 1_785_979_414_499,
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
    fn skips_group() {
        let msg = incoming("id1", "channel-id", None, "hi", false, true, false);
        assert!(map_incoming(&msg).is_none());
    }

    #[test]
    fn skips_empty_text_and_empty_file() {
        let empty = incoming("id1", "npub1peer", None, "", false, false, false);
        assert!(map_incoming(&empty).is_none());
        let file = incoming("id2", "npub1peer", None, "", false, false, true);
        assert!(map_incoming(&file).is_none());
    }
}
