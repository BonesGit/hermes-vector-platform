//! HTTP types, auth, errors, and JSON routes for the sidecar.

use std::path::{Component, Path, PathBuf};
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
use vector_sdk::{Attachment, VectorBot};

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
    bot: RwLock<Option<VectorBot>>,
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
            bot: RwLock::new(None),
        }))
    }

    pub async fn set_bot(&self, bot: VectorBot) {
        *self.0.bot.write().await = Some(bot);
    }

    pub async fn bot(&self) -> Option<VectorBot> {
        self.0.bot.read().await.clone()
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
        .route("/react", post(react))
        .route("/send-file", post(send_file))
        .route("/download-attachment", post(download_attachment))
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

    pub fn internal() -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "internal",
            error: "internal error",
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
    let npub = parse_npub(&req.to)?;
    state.require_ready().await?;
    if let Some(bot) = state.bot().await {
        let channel = bot.dm(&npub);
        let result = match req.reply_to.as_deref() {
            Some(id) if !id.is_empty() => channel.reply(id, &req.body).await,
            _ => channel.send(&req.body).await,
        };
        let id = result.map_err(|err| {
            eprintln!("[vector-bridge] send failed: {err}");
            ApiError::internal()
        })?;
        Ok(Json(json!({ "id": id })))
    } else {
        Ok(Json(json!({ "id": state.next_event_id() })))
    }
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
    let npub = parse_npub(&req.to)?;
    state.require_ready().await?;
    if let Some(bot) = state.bot().await {
        bot.dm(&npub).typing().await.map_err(|err| {
            eprintln!("[vector-bridge] typing failed: {err}");
            ApiError::internal()
        })?;
    }
    Ok(Json(json!({ "ok": true })))
}

#[derive(Deserialize)]
struct ReactRequest {
    to: String,
    message_id: String,
    #[serde(default)]
    emoji: String,
}

async fn react(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(req): JsonBody<ReactRequest>,
) -> Result<Json<Value>, ApiError> {
    let npub = parse_npub(&req.to)?;
    let message_id = req.message_id.trim();
    if message_id.is_empty() {
        return Err(ApiError::bad_request("message_id is required"));
    }
    let emoji = req.emoji.trim();
    if emoji.is_empty() {
        return Err(ApiError::bad_request("emoji is required"));
    }
    state.require_ready().await?;
    if let Some(bot) = state.bot().await {
        bot.dm(&npub)
            .react(message_id, emoji)
            .await
            .map_err(|err| {
                eprintln!("[vector-bridge] react failed: {err}");
                ApiError::internal()
            })?;
    }
    Ok(Json(json!({ "ok": true })))
}

#[derive(Deserialize)]
struct ProfileRequest {
    #[serde(default)]
    name: String,
    #[serde(default)]
    about: String,
    #[serde(default)]
    avatar_path: Option<String>,
    #[serde(default)]
    banner_path: Option<String>,
}

fn optional_abs_file(
    raw: Option<&str>,
    bad: &'static str,
) -> Result<Option<PathBuf>, ApiError> {
    let Some(s) = raw.map(str::trim).filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    let path = PathBuf::from(s);
    if !path.is_absolute() || !path.is_file() {
        return Err(ApiError::bad_request(bad));
    }
    Ok(Some(path))
}

async fn profile(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(req): JsonBody<ProfileRequest>,
) -> Result<Json<Value>, ApiError> {
    state.require_ready().await?;
    let avatar_path = optional_abs_file(
        req.avatar_path.as_deref(),
        "avatar_path must be an existing absolute file",
    )?;
    let banner_path = optional_abs_file(
        req.banner_path.as_deref(),
        "banner_path must be an existing absolute file",
    )?;
    let data_dir = std::env::var("VECTOR_DATA_DIR")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from);
    if let Some(bot) = state.bot().await {
        if !crate::profile::apply_own_profile(
            &bot,
            &req.name,
            &req.about,
            avatar_path.as_deref(),
            banner_path.as_deref(),
            data_dir.as_deref(),
        )
        .await
        {
            return Err(ApiError::internal());
        }
    }
    Ok(Json(json!({ "ok": true })))
}

#[derive(Deserialize)]
struct SendFileRequest {
    to: String,
    path: String,
}

async fn send_file(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(req): JsonBody<SendFileRequest>,
) -> Result<Json<Value>, ApiError> {
    let npub = parse_npub(&req.to)?;
    state.require_ready().await?;
    let path = PathBuf::from(&req.path);
    if !path.is_absolute() || !path.is_file() {
        return Err(ApiError::bad_request(
            "path must be an existing absolute file",
        ));
    }
    if let Some(bot) = state.bot().await {
        let id = bot.dm(&npub).send_file(&path).await.map_err(|err| {
            eprintln!("[vector-bridge] send_file failed: {err}");
            ApiError::internal()
        })?;
        Ok(Json(json!({ "id": id })))
    } else {
        Ok(Json(json!({ "id": state.next_event_id() })))
    }
}

#[derive(Deserialize)]
struct DownloadAttachmentRequest {
    attachment: Attachment,
    dest: String,
    #[serde(default)]
    author_npub: Option<String>,
}

fn dest_is_safe(dest: &Path) -> bool {
    dest.is_absolute()
        && dest
            .components()
            .all(|c| !matches!(c, Component::ParentDir))
}

async fn download_attachment(
    State(state): State<AppState>,
    _auth: Auth,
    JsonBody(req): JsonBody<DownloadAttachmentRequest>,
) -> Result<Json<Value>, ApiError> {
    state.require_ready().await?;
    let dest = PathBuf::from(&req.dest);
    if !dest_is_safe(&dest) {
        return Err(ApiError::bad_request(
            "dest must be an absolute path without ..",
        ));
    }
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).map_err(|_| ApiError::internal())?;
    }
    let size = if let Some(bot) = state.bot().await {
        let author = req.author_npub.as_deref().filter(|s| !s.is_empty());
        let bytes = bot
            .download_attachment_from(&req.attachment, author)
            .await
            .map_err(|err| {
                eprintln!("[vector-bridge] download_attachment failed: {err}");
                ApiError::internal()
            })?;
        let n = bytes.len();
        std::fs::write(&dest, bytes).map_err(|_| ApiError::internal())?;
        n
    } else {
        std::fs::write(&dest, b"").map_err(|_| ApiError::internal())?;
        0
    };
    Ok(Json(json!({
        "path": dest.to_string_lossy(),
        "size": size,
    })))
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
