//! Single-client last-writer-wins SSE (`GET /events`) plus test inject.

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

#[derive(Debug, Clone, Serialize, Deserialize)]
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

pub async fn inject(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(data): JsonBody<MessageEventData>,
) -> Result<Json<Value>, ApiError> {
    if data.is_group || data.is_mine {
        eprintln!("[vector-bridge] dropping inject id={}", data.id);
        return Ok(Json(json!({ "ok": true })));
    }
    let id = data.id.clone();
    let payload = json!({ "type": "message", "data": data }).to_string();
    state.events().publish(SseItem {
        id: Some(id),
        payload,
    });
    Ok(Json(json!({ "ok": true })))
}
