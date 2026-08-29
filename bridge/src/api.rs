//! HTTP types, auth, errors, and JSON routes for the sidecar stub.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::extract::{DefaultBodyLimit, FromRequest, FromRequestParts, Request, State};
use axum::http::request::Parts;
use axum::http::{header, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::RwLock;
use vector_sdk::nostr::{PublicKey, ToBech32};

use crate::events::{self, EventHub, SseItem};

pub const MAX_BODY: usize = 64 * 1024;
pub const TOKEN_HEADER: &str = "x-hermes-sidecar-token";
const STUB_NPUB: &str = "npub1stub";

#[derive(Clone)]
pub struct AppState(Arc<Inner>);

struct Inner {
    token: String,
    health: RwLock<Health>,
    events: EventHub,
    ping_interval: Duration,
    send_seq: AtomicU64,
}

struct Health {
    ready: bool,
    npub: Option<String>,
}

impl AppState {
    pub fn new(token: String, ping_interval: Duration) -> Self {
        Self(Arc::new(Inner {
            token,
            health: RwLock::new(Health {
                ready: false,
                npub: None,
            }),
            events: EventHub::new(),
            ping_interval,
            send_seq: AtomicU64::new(1),
        }))
    }

    pub fn token(&self) -> &str {
        &self.0.token
    }

    pub fn events(&self) -> &EventHub {
        &self.0.events
    }

    pub fn ping_interval(&self) -> Duration {
        self.0.ping_interval
    }

    pub async fn mark_ready(&self, npub: impl Into<String>) {
        let npub = npub.into();
        {
            let mut health = self.0.health.write().await;
            health.ready = true;
            health.npub = Some(npub.clone());
        }
        self.0.events.publish(ready_item(&npub));
    }

    pub async fn is_ready(&self) -> bool {
        self.0.health.read().await.ready
    }

    pub async fn npub(&self) -> Option<String> {
        self.0.health.read().await.npub.clone()
    }

    pub async fn require_ready(&self) -> Result<String, ApiError> {
        let health = self.0.health.read().await;
        if health.ready {
            if let Some(npub) = health.npub.clone() {
                return Ok(npub);
            }
        }
        Err(ApiError::not_ready())
    }

    fn next_event_id(&self) -> String {
        let n = self.0.send_seq.fetch_add(1, Ordering::Relaxed);
        format!("{n:064x}")
    }
}

pub fn ready_item(npub: &str) -> SseItem {
    SseItem {
        id: None,
        payload: json!({"type": "ready", "data": {"npub": npub}}).to_string(),
    }
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/live", get(live))
        .route("/health", get(health))
        .route("/npub", get(npub))
        .route("/events", get(events::get_events))
        .route("/send", post(send))
        .route("/typing", post(typing))
        .route("/profile", post(profile).get(not_implemented))
        .route("/react", post(not_implemented))
        .route("/send-file", post(not_implemented))
        .route("/download-attachment", post(not_implemented))
        .route("/block", post(not_implemented))
        .route("/__test/ready", post(test_ready))
        .route("/__test/inject", post(events::inject))
        .layer(DefaultBodyLimit::max(MAX_BODY))
        .layer(middleware::from_fn(rewrite_payload_too_large))
        .with_state(state)
}

pub struct Auth;

impl FromRequestParts<AppState> for Auth {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        let provided = parts
            .headers
            .get(TOKEN_HEADER)
            .and_then(|v| v.to_str().ok());
        match provided {
            Some(token) if token == state.token() => Ok(Auth),
            _ => Err(ApiError::unauthorized()),
        }
    }
}

pub struct JsonBody<T>(pub T);

impl<T, S> FromRequest<S> for JsonBody<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        let bytes = match axum::body::Bytes::from_request(req, state).await {
            Ok(b) => b,
            Err(rej) => {
                let status = rej.into_response().status();
                if status == StatusCode::PAYLOAD_TOO_LARGE {
                    return Err(ApiError::payload_too_large());
                }
                return Err(ApiError::bad_request("failed to read body"));
            }
        };
        if bytes.len() > MAX_BODY {
            return Err(ApiError::payload_too_large());
        }
        serde_json::from_slice(&bytes)
            .map(JsonBody)
            .map_err(|_| ApiError::bad_request("invalid json"))
    }
}

pub struct ApiError {
    status: StatusCode,
    code: &'static str,
    error: &'static str,
}

impl ApiError {
    pub fn unauthorized() -> Self {
        Self {
            status: StatusCode::UNAUTHORIZED,
            code: "unauthorized",
            error: "unauthorized",
        }
    }

    pub fn bad_request(error: &'static str) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "bad_request",
            error,
        }
    }

    pub fn invalid_npub() -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "invalid_npub",
            error: "invalid npub",
        }
    }

    pub fn not_ready() -> Self {
        Self {
            status: StatusCode::SERVICE_UNAVAILABLE,
            code: "not_ready",
            error: "sidecar is not ready",
        }
    }

    pub fn payload_too_large() -> Self {
        Self {
            status: StatusCode::PAYLOAD_TOO_LARGE,
            code: "payload_too_large",
            error: "request body too large",
        }
    }

    pub fn not_implemented() -> Self {
        Self {
            status: StatusCode::NOT_IMPLEMENTED,
            code: "not_implemented",
            error: "not implemented",
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = json!({ "error": self.error, "code": self.code });
        (self.status, Json(body)).into_response()
    }
}

async fn rewrite_payload_too_large(req: Request, next: Next) -> Response {
    let response = next.run(req).await;
    if response.status() == StatusCode::PAYLOAD_TOO_LARGE {
        let json = response
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .is_some_and(|ct| ct.starts_with("application/json"));
        if !json {
            return ApiError::payload_too_large().into_response();
        }
    }
    response
}

async fn live() -> Json<Value> {
    Json(json!({ "ok": true }))
}

#[derive(Serialize)]
struct HealthBody {
    status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    npub: Option<String>,
}

async fn health(State(state): State<AppState>, _auth: Auth) -> Json<HealthBody> {
    let health = state.0.health.read().await;
    if health.ready {
        Json(HealthBody {
            status: "ready",
            npub: health.npub.clone(),
        })
    } else {
        Json(HealthBody {
            status: "starting",
            npub: None,
        })
    }
}

async fn npub(State(state): State<AppState>, _auth: Auth) -> Result<Json<Value>, ApiError> {
    let npub = state.require_ready().await?;
    Ok(Json(json!({ "npub": npub })))
}

#[derive(Deserialize)]
struct SendRequest {
    to: String,
    body: String,
    #[serde(default)]
    reply_to: Option<String>,
}

async fn send(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(req): JsonBody<SendRequest>,
) -> Result<Json<Value>, ApiError> {
    let _ = parse_npub(&req.to)?;
    let _ = req.body;
    let _ = req.reply_to;
    state.require_ready().await?;
    Ok(Json(json!({ "id": state.next_event_id() })))
}

#[derive(Deserialize)]
struct TypingRequest {
    to: String,
}

async fn typing(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(req): JsonBody<TypingRequest>,
) -> Result<Json<Value>, ApiError> {
    let _ = parse_npub(&req.to)?;
    state.require_ready().await?;
    Ok(Json(json!({ "ok": true })))
}

#[derive(Deserialize)]
struct ProfileRequest {
    name: String,
    about: String,
}

async fn profile(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(req): JsonBody<ProfileRequest>,
) -> Result<Json<Value>, ApiError> {
    let _ = (req.name, req.about);
    state.require_ready().await?;
    Ok(Json(json!({ "ok": true })))
}

async fn not_implemented(_auth: Auth) -> ApiError {
    ApiError::not_implemented()
}

async fn test_ready(State(state): State<AppState>, _auth: Auth) -> Json<Value> {
    state.mark_ready(STUB_NPUB).await;
    Json(json!({ "ok": true }))
}

pub fn parse_npub(raw: &str) -> Result<String, ApiError> {
    PublicKey::parse(raw.trim())
        .map_err(|_| ApiError::invalid_npub())?
        .to_bech32()
        .map_err(|_| ApiError::invalid_npub())
}

pub fn stub_npub() -> &'static str {
    STUB_NPUB
}
