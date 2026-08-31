//! HTTP types, auth, errors, and JSON routes for the sidecar.

use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{DefaultBodyLimit, FromRequest, FromRequestParts, Request, State};
use axum::http::request::Parts;
use axum::http::{header, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use nostr::event::EventId;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::RwLock;
use vector_sdk::nostr::{PublicKey, ToBech32};
use vector_sdk::vector_core::deletion::delete_own_reaction;
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
    /// Outbound reaction rumors we posted: (peer npub, target message id) →
    /// (emoji, reaction rumor id). Unreact uses this instead of hoping
    /// `Channel::history` has already echoed the chip.
    own_reactions: Mutex<HashMap<(String, String), Vec<(String, String)>>>,
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
            own_reactions: Mutex::new(HashMap::new()),
        }))
    }

    fn remember_own_reaction(&self, npub: &str, message_id: &str, emoji: &str, reaction_id: &str) {
        let mut map = self
            .0
            .own_reactions
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let slot = map
            .entry((npub.to_string(), message_id.to_string()))
            .or_default();
        slot.retain(|(e, _)| e != emoji);
        slot.push((emoji.to_string(), reaction_id.to_string()));
    }

    fn take_own_reactions(
        &self,
        npub: &str,
        message_id: &str,
        emoji: &str,
    ) -> Vec<(String, String)> {
        let mut map = self
            .0
            .own_reactions
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let key = (npub.to_string(), message_id.to_string());
        let slot = map.remove(&key).unwrap_or_default();
        if emoji.is_empty() {
            return slot;
        }
        let (taken, rest): (Vec<_>, Vec<_>) = slot.into_iter().partition(|(e, _)| e == emoji);
        if !rest.is_empty() {
            map.insert(key, rest);
        }
        taken
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
    #[serde(default)]
    remove: bool,
    #[serde(default)]
    emoji_url: Option<String>,
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
    state.require_ready().await?;
    if req.remove {
        return unreact(&state, &npub, message_id, req.emoji.trim()).await;
    }
    let emoji = req.emoji.trim();
    if emoji.is_empty() {
        return Err(ApiError::bad_request("emoji is required"));
    }
    let emoji_url = req
        .emoji_url
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());
    if let Some(bot) = state.bot().await {
        // core.send_reaction returns the rumor id so we can NIP-09 it later.
        // Channel::react drops that id, and history echo is best-effort — the
        // 👀 → ✅ swap was leaving both chips because unreact found nothing.
        let id = bot
            .core()
            .send_reaction(&npub, message_id, emoji, emoji_url)
            .await
            .map_err(|err| {
                eprintln!("[vector-bridge] react failed: {err}");
                ApiError::internal()
            })?;
        state.remember_own_reaction(&npub, message_id, emoji, &id);
    }
    Ok(Json(json!({ "ok": true })))
}

fn same_pubkey(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    match (PublicKey::parse(a.trim()), PublicKey::parse(b.trim())) {
        (Ok(x), Ok(y)) => x == y,
        _ => a.eq_ignore_ascii_case(b),
    }
}

fn same_event_id(a: &str, b: &str) -> bool {
    a == b || a.eq_ignore_ascii_case(b)
}

/// Retract our NIP-25 reaction(s) on `message_id` (NIP-09 of the reaction rumor).
/// `emoji` empty = all of ours on that target; otherwise that slot only.
async fn unreact(
    state: &AppState,
    npub: &str,
    message_id: &str,
    emoji: &str,
) -> Result<Json<Value>, ApiError> {
    let recipient = PublicKey::parse(npub).map_err(|_| ApiError::invalid_npub())?;
    let mut ids: Vec<String> = state
        .take_own_reactions(npub, message_id, emoji)
        .into_iter()
        .map(|(_, id)| id)
        .collect();
    if let Some(bot) = state.bot().await {
        let me = bot.npub();
        let history = bot.dm(npub).history(200).await;
        if let Some(msg) = history.iter().find(|m| same_event_id(&m.id, message_id)) {
            for reaction in &msg.reactions {
                if !same_pubkey(&reaction.author_id, me) {
                    continue;
                }
                if !emoji.is_empty() && reaction.emoji != emoji {
                    continue;
                }
                if ids.iter().any(|id| same_event_id(id, &reaction.id)) {
                    continue;
                }
                ids.push(reaction.id.clone());
            }
        }
    } else if ids.is_empty() {
        return Ok(Json(json!({ "ok": true })));
    }
    if ids.is_empty() {
        eprintln!("[vector-bridge] unreact: no own reaction on id={message_id}");
        return Ok(Json(json!({ "ok": true })));
    }
    let mut any_err = false;
    for reaction_id in ids {
        let Ok(id) = EventId::from_hex(&reaction_id) else {
            eprintln!("[vector-bridge] unreact skip bad id={reaction_id}");
            continue;
        };
        if let Err(err) = delete_own_reaction(&id, recipient).await {
            eprintln!("[vector-bridge] unreact failed id={reaction_id}: {err}");
            any_err = true;
        }
    }
    if any_err {
        return Err(ApiError::internal());
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

fn optional_abs_file(raw: Option<&str>, bad: &'static str) -> Result<Option<PathBuf>, ApiError> {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remembers_and_takes_own_reactions() {
        let state = AppState::new("tok".into(), Duration::from_secs(1));
        state.remember_own_reaction("npub1a", "msg", "👀", "rid-eyes");
        state.remember_own_reaction("npub1a", "msg", "✅", "rid-ok");
        let eyes = state.take_own_reactions("npub1a", "msg", "👀");
        assert_eq!(eyes, vec![("👀".into(), "rid-eyes".into())]);
        let rest = state.take_own_reactions("npub1a", "msg", "");
        assert_eq!(rest, vec![("✅".into(), "rid-ok".into())]);
        assert!(state.take_own_reactions("npub1a", "msg", "").is_empty());
    }

    #[test]
    fn same_pubkey_matches_hex_and_npub() {
        let npub = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6";
        let hex = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d";
        assert!(same_pubkey(npub, npub));
        assert!(same_pubkey(hex, npub));
        assert!(same_event_id("Ab", "ab"));
    }
}
