//! Concord community administration. Token-gated loopback routes over vector_sdk 0.14.
//!
//! Missing powers from `vector_sdk::manage` arrive as `VectorError::NotPermitted`.
//! Core moderation (`kick`, `ban`, channel edits, `hide`, nicknames) arrives as
//! `VectorError::Other`. [`map_sdk_error`] turns both into HTTP 403/404/409.

use std::time::Duration;

use axum::extract::{DefaultBodyLimit, Path as UrlPath, State};
use axum::routing::{delete, get, patch, post, put};
use axum::{Json, Router};
use base64::Engine;
use serde_json::{json, Value};
use vector_sdk::policy::{Policy, Preset};
use vector_sdk::{
    AuditAction, AuditLog, AuditTarget, NewChannel, Overwrite, OverwriteTarget, Permissions, Pin,
    Role, RoleSpec, VectorBot, VectorError,
};

use crate::api::{
    is_channel_id, parse_community_id, parse_npub, ApiError, AppState, FileJsonBody, JsonBody,
    MAX_FILE_BODY,
};
use crate::events::SseItem;

const MAX_TIMEOUT_SECS: u64 = 28 * 24 * 60 * 60;
const MAX_IMAGE: usize = 4 * 1024 * 1024;

pub fn routes() -> Router<AppState> {
    let image_limit = || DefaultBodyLimit::max(MAX_FILE_BODY);
    Router::new()
        .route(
            "/communities/{community_id}/capabilities",
            get(capabilities),
        )
        .route("/communities/{community_id}/roles", get(list_roles).post(create_role))
        .route(
            "/communities/{community_id}/roles/order",
            put(reorder_roles),
        )
        .route(
            "/communities/{community_id}/roles/everyone",
            get(everyone_permissions).put(set_everyone_permissions),
        )
        .route(
            "/communities/{community_id}/roles/{role_id}",
            patch(edit_role).delete(delete_role),
        )
        .route("/communities/{community_id}/members", get(list_members))
        .route("/communities/{community_id}/members/kick", post(kick_member))
        .route("/communities/{community_id}/members/ban", post(ban_members))
        .route("/communities/{community_id}/members/unban", post(unban_members))
        .route(
            "/communities/{community_id}/members/timeout",
            post(timeout_member).delete(remove_timeout),
        )
        .route("/communities/{community_id}/members/admin", post(set_admin))
        .route("/communities/{community_id}/members/roles", post(set_member_roles))
        .route(
            "/communities/{community_id}/members/{npub}/permissions",
            get(member_permissions),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/hide",
            post(hide_message),
        )
        .route("/communities/{community_id}/leave", post(leave_community))
        .route(
            "/communities/{community_id}/channels",
            get(list_channels).post(create_channel),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}",
            patch(update_channel).delete(delete_channel),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/duplicate",
            post(duplicate_channel),
        )
        .route(
            "/communities/{community_id}/categories",
            get(list_categories).post(create_category),
        )
        .route(
            "/communities/{community_id}/categories/{category_id}",
            patch(update_category).delete(delete_category),
        )
        .route(
            "/communities/{community_id}/categories/{category_id}/overwrites",
            put(set_category_overwrite),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/overwrites",
            get(list_overwrites).put(set_channel_overwrite).delete(clear_channel_overwrite),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/sync",
            post(sync_channel),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/permissions/{npub}",
            get(channel_permissions),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/access",
            get(channel_access),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/access/grant",
            post(grant_access),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/access/revoke",
            post(revoke_access),
        )
        .route(
            "/communities/{community_id}/system-channel",
            put(set_system_channel),
        )
        .route(
            "/communities/{community_id}/landing-channel",
            put(set_landing_channel),
        )
        .route(
            "/communities/{community_id}",
            patch(edit_community),
        )
        .route(
            "/communities/{community_id}/icon",
            put(set_icon).delete(remove_icon).layer(image_limit()),
        )
        .route(
            "/communities/{community_id}/banner",
            put(set_banner).delete(remove_banner).layer(image_limit()),
        )
        .route(
            "/communities/{community_id}/channels/{channel_id}/pins",
            get(list_pins).post(pin_message).delete(unpin_message),
        )
        .route(
            "/communities/{community_id}/nicknames",
            get(list_nicknames).put(set_nickname),
        )
        .route("/communities/{community_id}/audit-log", get(audit_log))
        .route("/communities/{community_id}/relays", get(list_relays).put(move_relays))
        .route("/communities/{community_id}/relays/check", post(check_relays))
        .route("/communities/{community_id}/voice-relays", put(set_voice_relays))
        .route("/communities/{community_id}/invites", get(list_invites).post(create_invite).delete(revoke_all_invites))
        .route(
            "/communities/{community_id}/invites/{token}",
            delete(revoke_invite),
        )
        .route("/communities/{community_id}/raid-report", get(raid_report))
        .route("/communities/{community_id}/contain-raid", post(contain_raid))
        .route("/communities/{community_id}/epoch", get(explain_epoch))
        .route("/communities/{community_id}/repair-roster", post(repair_roster))
        .route("/communities/{community_id}/rescue-stranded", post(rescue_stranded))
        .route("/communities/{community_id}/refound", post(refound))
        .route(
            "/communities/{community_id}/policies",
            get(list_policies).post(mutate_policy),
        )
        .route("/communities/{community_id}/verdicts", get(verdicts))
        .route("/communities/{community_id}/dissolve", post(dissolve))
}

pub(crate) fn map_sdk_error(err: VectorError) -> ApiError {
    match err {
        VectorError::NotPermitted { needs, action } => ApiError::forbidden_needs(&action, needs.0),
        VectorError::NotFound(msg) => ApiError::not_found(msg),
        VectorError::NotInitialized(msg) => ApiError::unavailable(msg),
        VectorError::Other(msg) => classify_other(msg),
        other => ApiError::sdk(other.to_string()),
    }
}

fn classify_other(msg: String) -> ApiError {
    let lower = msg.to_ascii_lowercase();
    if lower.contains("follows the permissions of category") || lower.contains("would not apply") {
        return ApiError::conflict(msg);
    }
    if lower.contains("pins are sealed") {
        return ApiError::conflict(msg);
    }
    if lower.contains("isn't an npub") || lower.contains("must be 32-byte hex") {
        return ApiError::bad_request(msg);
    }
    if lower.contains("not found")
        || lower.contains("no policy")
        || lower.contains("no community")
        || lower.contains("no channel")
        || lower.contains("no role")
    {
        return ApiError::not_found(msg);
    }
    if lower.contains("not authorized")
        || lower.contains("permission")
        || lower.contains("outrank")
        || lower.contains("needs the")
        || lower.contains("you are banned")
    {
        return ApiError::forbidden(msg);
    }
    ApiError::sdk(msg)
}

fn stub(mut body: Value) -> Json<Value> {
    if let Some(obj) = body.as_object_mut() {
        obj.insert("stub".into(), json!(true));
    }
    Json(body)
}

fn audit(action: &str, community_id: &str, extra: &str) {
    eprintln!("[vector-bridge] admin action={action} community_id={community_id} {extra}");
}

async fn ready_bot(state: &AppState) -> Result<Option<VectorBot>, ApiError> {
    state.require_ready().await?;
    Ok(state.bot().await)
}

fn parse_hex_id(raw: &str, what: &str) -> Result<String, ApiError> {
    let s = raw.trim();
    if s.is_empty() {
        return Err(ApiError::bad_request(format!("{what} is required")));
    }
    if is_channel_id(s) {
        return Ok(s.to_ascii_lowercase());
    }
    Err(ApiError::bad_request(format!(
        "{what} must be 64 hex characters"
    )))
}

fn body_npub(body: &Value) -> Result<String, ApiError> {
    let raw = body
        .get("npub")
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request("npub is required"))?;
    parse_npub(raw)
}

fn npub_list(value: &Value) -> Result<Vec<String>, ApiError> {
    let arr = value
        .as_array()
        .ok_or_else(|| ApiError::bad_request("npubs must be a list"))?;
    if arr.len() > 500 {
        return Err(ApiError::bad_request("npubs is capped at 500"));
    }
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let raw = item
            .as_str()
            .ok_or_else(|| ApiError::bad_request("npubs must be strings"))?;
        out.push(parse_npub(raw)?);
    }
    Ok(out)
}

fn one_or_many(body: &Value) -> Result<Vec<String>, ApiError> {
    let has_one = body.get("npub").is_some();
    let has_many = body.get("npubs").is_some();
    if has_one == has_many {
        return Err(ApiError::bad_request("send npub or npubs"));
    }
    if has_one {
        return Ok(vec![body_npub(body)?]);
    }
    let list = npub_list(body.get("npubs").expect("npubs"))?;
    if list.is_empty() {
        return Err(ApiError::bad_request("npubs is empty"));
    }
    Ok(list)
}

fn refs<'a>(npubs: &'a [String]) -> Vec<&'a str> {
    npubs.iter().map(String::as_str).collect()
}

fn perm_bit(name: &str) -> Result<u64, ApiError> {
    Ok(match name.trim().to_ascii_lowercase().as_str() {
        "manage_roles" => Permissions::MANAGE_ROLES,
        "manage_channels" => Permissions::MANAGE_CHANNELS,
        "manage_metadata" => Permissions::MANAGE_METADATA,
        "kick" => Permissions::KICK,
        "ban" => Permissions::BAN,
        "manage_messages" => Permissions::MANAGE_MESSAGES,
        "create_invite" => Permissions::CREATE_INVITE,
        "view_audit_log" => Permissions::VIEW_AUDIT_LOG,
        "mention_everyone" => Permissions::MENTION_EVERYONE,
        "pin_messages" => Permissions::PIN_MESSAGES,
        "view_channel" => Permissions::VIEW_CHANNEL,
        "send_messages" => Permissions::SEND_MESSAGES,
        "send_in_threads" => Permissions::SEND_IN_THREADS,
        "attach_files" => Permissions::ATTACH_FILES,
        "add_reactions" => Permissions::ADD_REACTIONS,
        "read_message_history" => Permissions::READ_MESSAGE_HISTORY,
        "connect" => Permissions::CONNECT,
        "speak" => Permissions::SPEAK,
        "video" => Permissions::VIDEO,
        "priority_speaker" => Permissions::PRIORITY_SPEAKER,
        "mute_members" => Permissions::MUTE_MEMBERS,
        "deafen_members" => Permissions::DEAFEN_MEMBERS,
        "move_members" => Permissions::MOVE_MEMBERS,
        "moderate_members" => Permissions::MODERATE_MEMBERS,
        "manage_nicknames" => Permissions::MANAGE_NICKNAMES,
        "change_nickname" => Permissions::CHANGE_NICKNAME,
        other => {
            return Err(ApiError::bad_request(format!("unknown permission {other}")));
        }
    })
}

fn parse_permissions(value: &Value) -> Result<Permissions, ApiError> {
    if let Some(bits) = value.as_u64() {
        return Ok(Permissions(bits));
    }
    if let Some(text) = value.as_str() {
        if let Ok(bits) = text.trim().parse::<u64>() {
            return Ok(Permissions(bits));
        }
        return Ok(Permissions(perm_bit(text)?));
    }
    let Some(arr) = value.as_array() else {
        return Err(ApiError::bad_request(
            "permissions must be a bit mask or a list of names",
        ));
    };
    let mut bits = 0u64;
    for item in arr {
        let name = item
            .as_str()
            .ok_or_else(|| ApiError::bad_request("permission names must be strings"))?;
        bits |= perm_bit(name)?;
    }
    Ok(Permissions(bits))
}

fn role_spec(body: &Value, require_name: bool) -> Result<RoleSpec, ApiError> {
    let mut spec = if require_name {
        let name = body
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| ApiError::bad_request("name is required"))?;
        RoleSpec::new(name)
    } else {
        let mut spec = RoleSpec::unchanged();
        if let Some(name) = body.get("name").and_then(Value::as_str) {
            spec = spec.name(name);
        }
        spec
    };
    if let Some(color) = body.get("color").and_then(Value::as_u64) {
        spec = spec.color(color as u32);
    }
    if let Some(perms) = body.get("permissions") {
        spec = spec.permissions(parse_permissions(perms)?);
    }
    if let Some(channel) = body.get("channel_id").and_then(Value::as_str) {
        spec = spec.in_channel(parse_hex_id(channel, "channel_id")?);
    }
    if let Some(shown) = body.get("shown").and_then(Value::as_bool) {
        spec = if shown { spec.shown() } else { spec.hidden() };
    }
    if let Some(mentionable) = body.get("mentionable").and_then(Value::as_bool) {
        spec = spec.mentionable(mentionable);
    }
    Ok(spec)
}

fn parse_overwrite(body: &Value) -> Result<Overwrite, ApiError> {
    let target = body
        .get("target")
        .ok_or_else(|| ApiError::bad_request("target is required"))?;
    let mut overwrite = if let Some(kind) = target.as_str() {
        match kind {
            "everyone" => Overwrite::everyone(),
            _ => return Err(ApiError::bad_request("target must be everyone, role, or member")),
        }
    } else if let Some(obj) = target.as_object() {
        if let Some(role) = obj.get("role").and_then(Value::as_str) {
            Overwrite {
                target: OverwriteTarget::Role(parse_hex_id(role, "role")?),
                allow: Permissions(0),
                deny: Permissions(0),
            }
        } else if let Some(member) = obj.get("member").and_then(Value::as_str) {
            Overwrite::member(parse_npub(member)?)
        } else {
            return Err(ApiError::bad_request("target must be everyone, role, or member"));
        }
    } else {
        return Err(ApiError::bad_request("target must be everyone, role, or member"));
    };
    if let Some(allow) = body.get("allow") {
        overwrite = overwrite.allow(parse_permissions(allow)?);
    }
    if let Some(deny) = body.get("deny") {
        overwrite = overwrite.deny(parse_permissions(deny)?);
    }
    Ok(overwrite)
}

fn parse_target(value: &Value) -> Result<OverwriteTarget, ApiError> {
    if let Some(kind) = value.as_str() {
        return match kind {
            "everyone" => Ok(OverwriteTarget::Everyone),
            _ => Err(ApiError::bad_request("target must be everyone, role, or member")),
        };
    }
    let Some(obj) = value.as_object() else {
        return Err(ApiError::bad_request("target must be everyone, role, or member"));
    };
    if let Some(role) = obj.get("role").and_then(Value::as_str) {
        return Ok(OverwriteTarget::Role(parse_hex_id(role, "role")?));
    }
    if let Some(member) = obj.get("member").and_then(Value::as_str) {
        return Ok(OverwriteTarget::Member(parse_npub(member)?));
    }
    Err(ApiError::bad_request("target must be everyone, role, or member"))
}

fn overwrite_json(overwrite: &Overwrite) -> Value {
    json!({
        "target": overwrite.target,
        "allow": overwrite.allow.0,
        "deny": overwrite.deny.0,
    })
}

fn relay_list(body: &Value) -> Result<Vec<String>, ApiError> {
    let arr = body
        .get("relays")
        .and_then(Value::as_array)
        .ok_or_else(|| ApiError::bad_request("relays is required"))?;
    if arr.len() > 16 {
        return Err(ApiError::bad_request("too many relays"));
    }
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let relay = item
            .as_str()
            .map(str::trim)
            .filter(|s| !s.is_empty() && s.len() <= 300)
            .ok_or_else(|| ApiError::bad_request("relay url is invalid"))?;
        out.push(relay.to_string());
    }
    Ok(out)
}

fn message_id(body: &Value) -> Result<String, ApiError> {
    let raw = body
        .get("message_id")
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request("message_id is required"))?;
    parse_hex_id(raw, "message_id")
}

fn optional_channel(body: &Value) -> Result<Option<String>, ApiError> {
    if !body
        .as_object()
        .is_some_and(|obj| obj.contains_key("channel_id"))
    {
        return Err(ApiError::bad_request(
            "channel_id is required (null clears it)",
        ));
    }
    match body.get("channel_id") {
        Some(Value::Null) => Ok(None),
        Some(Value::String(raw)) => Ok(Some(parse_hex_id(raw, "channel_id")?)),
        _ => Err(ApiError::bad_request("channel_id must be 64 hex or null")),
    }
}

fn is_supported_image(bytes: &[u8]) -> bool {
    const PNG: &[u8] = b"\x89PNG\r\n\x1a\n";
    const JPEG: &[u8] = &[0xff, 0xd8, 0xff];
    bytes.starts_with(PNG)
        || bytes.starts_with(JPEG)
        || bytes.starts_with(b"GIF87a")
        || bytes.starts_with(b"GIF89a")
        || (bytes.len() >= 12 && &bytes[0..4] == b"RIFF" && &bytes[8..12] == b"WEBP")
}

fn decode_image(body: &Value) -> Result<Vec<u8>, ApiError> {
    if body.get("path").is_some_and(|v| !v.is_null()) {
        return Err(ApiError::bad_request(
            "path is not supported; send bytes_b64 instead",
        ));
    }
    let Some(b64) = body.get("bytes_b64").and_then(Value::as_str) else {
        return Err(ApiError::bad_request("bytes_b64 is required"));
    };
    let raw = base64::engine::general_purpose::STANDARD
        .decode(b64.trim())
        .map_err(|_| ApiError::bad_request("bytes_b64 is not valid base64"))?;
    if raw.is_empty() || raw.len() > MAX_IMAGE {
        return Err(ApiError::bad_request(
            "image must be between 1 byte and 4 MiB",
        ));
    }
    if !is_supported_image(&raw) {
        return Err(ApiError::bad_request(
            "image must be PNG, JPEG, WebP, or GIF",
        ));
    }
    Ok(raw)
}

fn public_invites_allowed() -> bool {
    std::env::var("VECTOR_ALLOW_PUBLIC_INVITES")
        .ok()
        .is_some_and(|s| {
            matches!(
                s.trim().to_ascii_lowercase().as_str(),
                "on" | "true" | "1" | "yes"
            )
        })
}

fn publish_channel(state: &AppState, community_id: &str, name: &str, channel_id: &str, channel_name: &str) {
    state.events().publish(SseItem::new(
        Some(format!("joined:{community_id}:{channel_id}")),
        json!({
            "type": "community_joined",
            "data": {
                "community_id": community_id,
                "name": name,
                "channels": [{ "channel_id": channel_id, "name": channel_name }],
            }
        })
        .to_string(),
    ));
}

fn pin_json(pin: &Pin) -> Value {
    json!({
        "message_id": pin.message_id,
        "author": pin.author,
        "content": pin.content,
        "at_ms": pin.at_ms,
    })
}

fn audit_action_name(action: &AuditAction) -> String {
    match action {
        AuditAction::CommunityCreate => "community_create",
        AuditAction::CommunityUpdate => "community_update",
        AuditAction::RolesActivate => "roles_activate",
        AuditAction::EveryoneUpdate => "everyone_update",
        AuditAction::RoleCreate => "role_create",
        AuditAction::RoleUpdate => "role_update",
        AuditAction::RoleDelete => "role_delete",
        AuditAction::MemberRolesUpdate => "member_roles_update",
        AuditAction::MemberBan => "member_ban",
        AuditAction::MemberUnban => "member_unban",
        AuditAction::BanlistUpdate => "banlist_update",
        AuditAction::MemberKick => "member_kick",
        AuditAction::MemberTimeout => "member_timeout",
        AuditAction::MemberTimeoutRemove => "member_timeout_remove",
        AuditAction::MemberNickUpdate => "member_nick_update",
        AuditAction::ChannelCreate => "channel_create",
        AuditAction::ChannelUpdate => "channel_update",
        AuditAction::ChannelDelete => "channel_delete",
        AuditAction::ChannelSync => "channel_sync",
        AuditAction::ChannelUnsync => "channel_unsync",
        AuditAction::CategoryCreate => "category_create",
        AuditAction::CategoryUpdate => "category_update",
        AuditAction::CategoryDelete => "category_delete",
        AuditAction::OverwriteCreate => "overwrite_create",
        AuditAction::OverwriteUpdate => "overwrite_update",
        AuditAction::OverwriteDelete => "overwrite_delete",
        AuditAction::MessagePin => "message_pin",
        AuditAction::MessageUnpin => "message_unpin",
        AuditAction::PinsUpdate => "pins_update",
        AuditAction::MessageDelete => "message_delete",
        AuditAction::InviteCreate => "invite_create",
        AuditAction::InviteDelete => "invite_delete",
        AuditAction::KeysRotate => "keys_rotate",
        AuditAction::VoiceMute => "voice_mute",
        AuditAction::VoiceUnmute => "voice_unmute",
        AuditAction::VoiceDeafen => "voice_deafen",
        AuditAction::VoiceUndeafen => "voice_undeafen",
        AuditAction::VoiceMove => "voice_move",
        AuditAction::VoiceDisconnect => "voice_disconnect",
        AuditAction::Other(other) => return other.clone(),
        _ => "other",
    }
    .to_string()
}

fn audit_target_json(target: &AuditTarget) -> Value {
    match target {
        AuditTarget::Member { npub } => json!({"kind": "member", "npub": npub}),
        AuditTarget::Everyone => json!({"kind": "everyone"}),
        AuditTarget::Role { id, name } => json!({"kind": "role", "id": id, "name": name}),
        AuditTarget::Channel { id, name } => json!({"kind": "channel", "id": id, "name": name}),
        AuditTarget::Category { id, name } => json!({"kind": "category", "id": id, "name": name}),
        AuditTarget::Message { id, author, channel_id } => {
            json!({"kind": "message", "id": id, "author": author, "channel_id": channel_id})
        }
        AuditTarget::Overwrite { target, scope } => json!({
            "kind": "overwrite",
            "for": audit_target_json(target),
            "in": audit_target_json(scope),
        }),
        AuditTarget::Other(value) => value.clone(),
        _ => json!({"kind": "other"}),
    }
}

fn audit_json(log: &AuditLog) -> Value {
    let entries: Vec<Value> = log
        .entries
        .iter()
        .map(|entry| {
            json!({
                "id": entry.id,
                "at_ms": entry.at_ms,
                "actor": entry.actor,
                "action": audit_action_name(&entry.action),
                "target": entry.target.as_ref().map(audit_target_json),
                "changes": entry.changes,
                "partial": entry.partial,
            })
        })
        .collect();
    json!({"entries": entries, "compacted": log.compacted})
}

fn build_policy(body: &Value) -> Result<Policy, ApiError> {
    if let Some(preset) = body.get("preset").and_then(Value::as_str) {
        let preset = match preset.trim().to_ascii_lowercase().as_str() {
            "vectordefaults" | "vector_defaults" => Preset::VectorDefaults,
            "scamlinks" | "scam_links" => Preset::ScamLinks,
            "nospam" | "no_spam" => Preset::NoSpam,
            "wordfilter" | "word_filter" => Preset::WordFilter,
            _ => return Err(ApiError::bad_request("unknown policy preset")),
        };
        return Policy::preset(preset).map_err(map_sdk_error);
    }
    if let Some(document) = body.get("document") {
        let text = if let Some(raw) = document.as_str() {
            raw.to_string()
        } else {
            serde_json::to_string(document)
                .map_err(|_| ApiError::bad_request("document is not json"))?
        };
        return Policy::from_json(&text).map_err(map_sdk_error);
    }
    Err(ApiError::bad_request("preset or document is required"))
}

fn policy_id(body: &Value) -> Result<String, ApiError> {
    let id = body.get("id").and_then(Value::as_str).unwrap_or("").trim();
    if id.is_empty()
        || id.len() > 64
        || !id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return Err(ApiError::bad_request("policy id is invalid"));
    }
    Ok(id.to_string())
}

async fn capabilities(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "capabilities": {}})));
    };
    let capabilities = bot
        .community(&community_id)
        .capabilities()
        .map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "capabilities": capabilities})))
}

async fn list_roles(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "owner": null, "admins": [], "roles": []})));
    };
    let community = bot.community(&community_id);
    let owner = community.owner();
    let admins = community.admins().map_err(map_sdk_error)?;
    let roles = community.roles().map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "owner": owner, "admins": admins, "roles": roles})))
}

async fn list_members(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "members": [], "banned": [], "timeouts": []})));
    };
    let members = bot.core().get_community_members(&community_id).await;
    let community = bot.community(&community_id);
    let banned = community.banned();
    let timeouts: Vec<Value> = community
        .timeouts()
        .map_err(map_sdk_error)?
        .into_iter()
        .map(|(npub, until)| json!({"npub": npub, "until_secs": until}))
        .collect();
    Ok(Json(json!({
        "community_id": community_id,
        "members": members,
        "banned": banned,
        "timeouts": timeouts,
    })))
}

async fn kick_member(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npub = body_npub(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npub": npub})));
    };
    bot.community(&community_id)
        .member(&npub)
        .kick()
        .await
        .map_err(map_sdk_error)?;
    audit("kick", &community_id, &format!("npub={npub}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "npub": npub})))
}

async fn ban_members(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npubs = one_or_many(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npubs": npubs})));
    };
    let community = bot.community(&community_id);
    if npubs.len() == 1 {
        community.member(&npubs[0]).ban().await.map_err(map_sdk_error)?;
    } else {
        community.ban_many(&refs(&npubs)).await.map_err(map_sdk_error)?;
    }
    audit("ban", &community_id, &format!("npubs={}", npubs.join(",")));
    Ok(Json(json!({"ok": true, "community_id": community_id, "npubs": npubs})))
}

async fn unban_members(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npubs = one_or_many(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npubs": npubs, "unbanned": npubs})));
    };
    let community = bot.community(&community_id);
    let banned: Vec<String> = npubs
        .iter()
        .filter(|npub| community.member(*npub).is_banned())
        .cloned()
        .collect();
    if !banned.is_empty() {
        if banned.len() == 1 {
            community.member(&banned[0]).unban().await.map_err(map_sdk_error)?;
        } else {
            community.unban_many(&refs(&banned)).await.map_err(map_sdk_error)?;
        }
    }
    audit(
        "unban",
        &community_id,
        &format!("requested={} unbanned={}", npubs.join(","), banned.join(",")),
    );
    Ok(Json(json!({
        "ok": true,
        "community_id": community_id,
        "npubs": npubs,
        "unbanned": banned,
    })))
}

async fn timeout_member(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npub = body_npub(&body)?;
    let seconds = body
        .get("seconds")
        .and_then(Value::as_u64)
        .ok_or_else(|| ApiError::bad_request("seconds is required"))?;
    if seconds == 0 || seconds > MAX_TIMEOUT_SECS {
        return Err(ApiError::bad_request(
            "seconds must be from 1 to 2419200 (28 days)",
        ));
    }
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npub": npub, "seconds": seconds})));
    };
    bot.community(&community_id)
        .member(&npub)
        .timeout(Duration::from_secs(seconds))
        .await
        .map_err(map_sdk_error)?;
    audit("timeout", &community_id, &format!("npub={npub} seconds={seconds}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "npub": npub, "seconds": seconds})))
}

async fn remove_timeout(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npub = body_npub(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npub": npub})));
    };
    bot.community(&community_id)
        .member(&npub)
        .remove_timeout()
        .await
        .map_err(map_sdk_error)?;
    audit("remove_timeout", &community_id, &format!("npub={npub}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "npub": npub})))
}

async fn hide_message(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let message_id = message_id(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "message_id": message_id})));
    };
    bot.channel(&channel_id)
        .hide(&message_id)
        .await
        .map_err(map_sdk_error)?;
    audit(
        "hide_message",
        &community_id,
        &format!("channel_id={channel_id} message_id={message_id}"),
    );
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "message_id": message_id})))
}

async fn leave_community(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id})));
    };
    bot.community(&community_id)
        .leave()
        .await
        .map_err(map_sdk_error)?;
    audit("leave", &community_id, "");
    Ok(Json(json!({"ok": true, "community_id": community_id})))
}

async fn list_channels(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "channels": [], "system_channel_id": null, "landing_channel_id": null})));
    };
    let community = bot.community(&community_id);
    let readable: std::collections::HashMap<String, bool> = community
        .channels()
        .await
        .into_iter()
        .map(|ch| (ch.id().to_string(), ch.is_readable()))
        .collect();
    let mut channels = Vec::new();
    for info in community.channel_infos().map_err(map_sdk_error)? {
        let mut value = serde_json::to_value(&info).unwrap_or(json!({}));
        if let Some(obj) = value.as_object_mut() {
            obj.insert(
                "readable".into(),
                json!(readable.get(&info.channel_id).copied().unwrap_or(true)),
            );
        }
        channels.push(value);
    }
    let system = community
        .system_channel()
        .map_err(map_sdk_error)?
        .map(|ch| ch.id().to_string());
    let landing = community
        .landing_channel()
        .map_err(map_sdk_error)?
        .map(|ch| ch.id().to_string());
    Ok(Json(json!({
        "community_id": community_id,
        "channels": channels,
        "system_channel_id": system,
        "landing_channel_id": landing,
    })))
}

async fn create_channel(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let name = required_name(&body)?;
    let mut opts = NewChannel::default();
    if body.get("private").and_then(Value::as_bool).unwrap_or(false) {
        opts = opts.private();
    }
    if let Some(category) = body.get("category_id").and_then(Value::as_str) {
        opts = opts.in_category(parse_hex_id(category, "category_id")?);
    }
    if let Some(view) = body.get("view").and_then(Value::as_str) {
        opts = opts.view(view);
    }
    if let Some(topic) = body.get("topic").and_then(Value::as_str) {
        opts = opts.topic(topic);
    }
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "name": name, "channel_id": "c".repeat(64)})));
    };
    let community = bot.community(&community_id);
    let channel = community
        .create_channel_with(&name, opts)
        .await
        .map_err(map_sdk_error)?;
    let channel_id = channel.id().to_string();
    let channel_name = channel.info().map(|info| info.name).unwrap_or(name.clone());
    let community_name = community.name().await;
    publish_channel(&state, &community_id, &community_name, &channel_id, &channel_name);
    audit("create_channel", &community_id, &format!("channel_id={channel_id} name={channel_name}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "name": channel_name})))
}

fn required_name(body: &Value) -> Result<String, ApiError> {
    body.get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .ok_or_else(|| ApiError::bad_request("name is required"))
}

async fn update_channel(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})));
    };
    let community = bot.community(&community_id);
    let channel = community.channel(&channel_id);
    if let Some(name) = body.get("name").and_then(Value::as_str) {
        channel.rename(name).await.map_err(map_sdk_error)?;
    }
    if body.get("clear_topic").and_then(Value::as_bool).unwrap_or(false) {
        channel.set_topic(None).await.map_err(map_sdk_error)?;
    } else if let Some(topic) = body.get("topic").and_then(Value::as_str) {
        channel.set_topic(Some(topic)).await.map_err(map_sdk_error)?;
    }
    if let Some(view) = body.get("view").and_then(Value::as_str) {
        channel.set_view(view).await.map_err(map_sdk_error)?;
    }
    if let Some(private) = body.get("private").and_then(Value::as_bool) {
        community
            .set_channel_private(&channel_id, private)
            .await
            .map_err(map_sdk_error)?;
    }
    if let Some(read_only) = body.get("read_only").and_then(Value::as_bool) {
        channel.set_read_only(read_only).await.map_err(map_sdk_error)?;
    }
    if body.get("category_id").is_some() || body.get("index").is_some() {
        let category = if body.get("category_id").is_some() {
            match body.get("category_id") {
                Some(Value::String(raw)) => Some(community.category(parse_hex_id(raw, "category_id")?)),
                Some(Value::Null) => None,
                _ => return Err(ApiError::bad_request("category_id must be 64 hex or null")),
            }
        } else {
            channel
                .info()
                .map_err(map_sdk_error)?
                .category_id
                .map(|id| community.category(id))
        };
        let index = body.get("index").and_then(Value::as_u64).unwrap_or(0) as usize;
        channel
            .move_to(category.as_ref(), index)
            .await
            .map_err(map_sdk_error)?;
    }
    audit("update_channel", &community_id, &format!("channel_id={channel_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})))
}

async fn duplicate_channel(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let name = required_name(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": "d".repeat(64), "name": name})));
    };
    let community = bot.community(&community_id);
    let created = community
        .channel(&channel_id)
        .duplicate(&name)
        .await
        .map_err(map_sdk_error)?;
    let new_id = created.id().to_string();
    let community_name = community.name().await;
    publish_channel(&state, &community_id, &community_name, &new_id, &name);
    audit("duplicate_channel", &community_id, &format!("channel_id={new_id} name={name}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": new_id, "name": name})))
}

async fn delete_channel(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})));
    };
    bot.community(&community_id)
        .delete_channel(&channel_id)
        .await
        .map_err(map_sdk_error)?;
    audit("delete_channel", &community_id, &format!("channel_id={channel_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})))
}

async fn list_categories(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "categories": []})));
    };
    let categories = bot
        .community(&community_id)
        .categories()
        .map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "categories": categories})))
}

async fn create_category(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let name = required_name(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "category_id": "e".repeat(64), "name": name})));
    };
    let category = bot
        .community(&community_id)
        .create_category(&name)
        .await
        .map_err(map_sdk_error)?;
    let category_id = category.id().to_string();
    audit("create_category", &community_id, &format!("category_id={category_id} name={name}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "category_id": category_id, "name": name})))
}

async fn update_category(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, category_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let category_id = parse_hex_id(&category_id, "category_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "category_id": category_id})));
    };
    let category = bot.community(&community_id).category(&category_id);
    if let Some(name) = body.get("name").and_then(Value::as_str) {
        category.rename(name).await.map_err(map_sdk_error)?;
    }
    if let Some(index) = body.get("index").and_then(Value::as_u64) {
        category.move_to(index as usize).await.map_err(map_sdk_error)?;
    }
    audit("update_category", &community_id, &format!("category_id={category_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "category_id": category_id})))
}

async fn delete_category(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, category_id)): UrlPath<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let category_id = parse_hex_id(&category_id, "category_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "category_id": category_id})));
    };
    bot.community(&community_id)
        .category(&category_id)
        .delete()
        .await
        .map_err(map_sdk_error)?;
    audit("delete_category", &community_id, &format!("category_id={category_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "category_id": category_id})))
}

async fn set_category_overwrite(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, category_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let category_id = parse_hex_id(&category_id, "category_id")?;
    let overwrite = parse_overwrite(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "category_id": category_id})));
    };
    bot.community(&community_id)
        .category(&category_id)
        .set_overwrite(overwrite)
        .await
        .map_err(map_sdk_error)?;
    audit("category_overwrite", &community_id, &format!("category_id={category_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "category_id": category_id})))
}

async fn list_overwrites(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "channel_id": channel_id, "overwrites": [], "synced_to": null})));
    };
    let channel = bot.community(&community_id).channel(&channel_id);
    let overwrites: Vec<Value> = channel
        .overwrites()
        .map_err(map_sdk_error)?
        .iter()
        .map(overwrite_json)
        .collect();
    let synced_to = channel
        .synced_to()
        .map_err(map_sdk_error)?
        .map(|category| category.id().to_string());
    Ok(Json(json!({
        "community_id": community_id,
        "channel_id": channel_id,
        "overwrites": overwrites,
        "synced_to": synced_to,
    })))
}

async fn set_channel_overwrite(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let overwrite = parse_overwrite(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})));
    };
    bot.community(&community_id)
        .channel(&channel_id)
        .set_overwrite(overwrite)
        .await
        .map_err(map_sdk_error)?;
    audit("channel_overwrite", &community_id, &format!("channel_id={channel_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})))
}

async fn clear_channel_overwrite(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let target = parse_target(
        body.get("target")
            .ok_or_else(|| ApiError::bad_request("target is required"))?,
    )?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})));
    };
    bot.community(&community_id)
        .channel(&channel_id)
        .clear_overwrite(target)
        .await
        .map_err(map_sdk_error)?;
    audit("clear_overwrite", &community_id, &format!("channel_id={channel_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})))
}

async fn sync_channel(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let category_id = optional_channel_key(&body, "category_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "category_id": category_id})));
    };
    let community = bot.community(&community_id);
    let category = category_id
        .as_deref()
        .map(|id| community.category(id));
    community
        .channel(&channel_id)
        .sync_permissions(category.as_ref())
        .await
        .map_err(map_sdk_error)?;
    audit("sync_channel", &community_id, &format!("channel_id={channel_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "category_id": category_id})))
}

fn optional_channel_key(body: &Value, key: &str) -> Result<Option<String>, ApiError> {
    if !body.as_object().is_some_and(|obj| obj.contains_key(key)) {
        return Err(ApiError::bad_request(format!(
            "{key} is required (null unsyncs the channel)"
        )));
    }
    match body.get(key) {
        Some(Value::Null) => Ok(None),
        Some(Value::String(raw)) => Ok(Some(parse_hex_id(raw, key)?)),
        _ => Err(ApiError::bad_request(format!("{key} must be 64 hex or null"))),
    }
}

async fn channel_permissions(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id, npub)): UrlPath<(String, String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let npub = parse_npub(&npub)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "channel_id": channel_id, "npub": npub, "permissions": 0})));
    };
    let permissions = bot
        .community(&community_id)
        .channel(&channel_id)
        .permissions_of(&npub)
        .map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "channel_id": channel_id, "npub": npub, "permissions": permissions.0})))
}

async fn channel_access(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "channel_id": channel_id, "access": {}, "members": []})));
    };
    let community = bot.community(&community_id);
    let access = community.channel_access(&channel_id).map_err(map_sdk_error)?;
    let members: Vec<String> = community
        .channel_members(&channel_id)
        .into_iter()
        .map(|member| member.npub().to_string())
        .collect();
    Ok(Json(json!({"community_id": community_id, "channel_id": channel_id, "access": access, "members": members})))
}

async fn grant_access(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let npub = body_npub(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "npub": npub})));
    };
    bot.community(&community_id)
        .grant_access(&channel_id, &npub)
        .await
        .map_err(map_sdk_error)?;
    audit("grant_access", &community_id, &format!("channel_id={channel_id} npub={npub}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "npub": npub})))
}

async fn revoke_access(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let npub = body_npub(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "npub": npub})));
    };
    bot.community(&community_id)
        .revoke_access(&channel_id, &npub)
        .await
        .map_err(map_sdk_error)?;
    audit("revoke_access", &community_id, &format!("channel_id={channel_id} npub={npub}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "npub": npub})))
}

async fn set_named_channel(
    state: AppState,
    community_id: String,
    body: Value,
    system: bool,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = optional_channel(&body)?;
    let action = if system { "system_channel" } else { "landing_channel" };
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})));
    };
    let community = bot.community(&community_id);
    let handle = channel_id.as_deref().map(|id| community.channel(id));
    if system {
        community
            .set_system_channel(handle.as_ref())
            .await
            .map_err(map_sdk_error)?;
    } else {
        community
            .set_landing_channel(handle.as_ref())
            .await
            .map_err(map_sdk_error)?;
    }
    audit(action, &community_id, &format!("channel_id={}", channel_id.as_deref().unwrap_or("")));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id})))
}

async fn set_system_channel(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    set_named_channel(state, community_id, body, true).await
}

async fn set_landing_channel(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    set_named_channel(state, community_id, body, false).await
}

async fn create_role(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let spec = role_spec(&body, true)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "role_id": "f".repeat(64)})));
    };
    let role = bot
        .community(&community_id)
        .create_role(spec)
        .await
        .map_err(map_sdk_error)?;
    let role_id = role.id().to_string();
    audit("create_role", &community_id, &format!("role_id={role_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "role_id": role_id})))
}

async fn edit_role(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, role_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let role_id = parse_hex_id(&role_id, "role_id")?;
    let spec = role_spec(&body, false)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "role_id": role_id})));
    };
    bot.community(&community_id)
        .role(&role_id)
        .edit(spec)
        .await
        .map_err(map_sdk_error)?;
    audit("edit_role", &community_id, &format!("role_id={role_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "role_id": role_id})))
}

async fn delete_role(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, role_id)): UrlPath<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let role_id = parse_hex_id(&role_id, "role_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "role_id": role_id})));
    };
    bot.community(&community_id)
        .role(&role_id)
        .delete()
        .await
        .map_err(map_sdk_error)?;
    audit("delete_role", &community_id, &format!("role_id={role_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "role_id": role_id})))
}

async fn reorder_roles(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let ids = body
        .get("role_ids")
        .and_then(Value::as_array)
        .ok_or_else(|| ApiError::bad_request("role_ids is required"))?;
    let mut role_ids = Vec::with_capacity(ids.len());
    for id in ids {
        let raw = id
            .as_str()
            .ok_or_else(|| ApiError::bad_request("role_ids must be strings"))?;
        role_ids.push(parse_hex_id(raw, "role_id")?);
    }
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "moved": role_ids.len()})));
    };
    let community = bot.community(&community_id);
    let roles: Vec<Role> = role_ids.iter().map(|id| community.role(id)).collect();
    let refs: Vec<&Role> = roles.iter().collect();
    let moved = community.reorder_roles(&refs).await.map_err(map_sdk_error)?;
    audit("reorder_roles", &community_id, &format!("moved={moved}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "moved": moved})))
}

async fn everyone_permissions(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "permissions": null})));
    };
    let permissions = bot
        .community(&community_id)
        .everyone_permissions()
        .map_err(map_sdk_error)?
        .map(|bits| bits.0);
    Ok(Json(json!({"community_id": community_id, "permissions": permissions})))
}

async fn set_everyone_permissions(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let permissions = parse_permissions(
        body.get("permissions")
            .ok_or_else(|| ApiError::bad_request("permissions is required"))?,
    )?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "permissions": permissions.0})));
    };
    bot.community(&community_id)
        .set_everyone_permissions(permissions)
        .await
        .map_err(map_sdk_error)?;
    audit("everyone_permissions", &community_id, &format!("permissions={}", permissions.0));
    Ok(Json(json!({"ok": true, "community_id": community_id, "permissions": permissions.0})))
}

async fn set_admin(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npub = body_npub(&body)?;
    let grant = body
        .get("grant")
        .and_then(Value::as_bool)
        .ok_or_else(|| ApiError::bad_request("grant is required"))?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npub": npub, "grant": grant})));
    };
    let member = bot.community(&community_id).member(&npub);
    if grant {
        member.grant_admin().await.map_err(map_sdk_error)?;
    } else {
        member.revoke_admin().await.map_err(map_sdk_error)?;
    }
    audit(
        if grant { "grant_admin" } else { "revoke_admin" },
        &community_id,
        &format!("npub={npub}"),
    );
    Ok(Json(json!({"ok": true, "community_id": community_id, "npub": npub, "grant": grant})))
}

async fn set_member_roles(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npub = body_npub(&body)?;
    let op = body
        .get("op")
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request("op is required"))?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npub": npub, "op": op})));
    };
    let community = bot.community(&community_id);
    let member = community.member(&npub);
    match op {
        "add" | "remove" => {
            let role_id = parse_hex_id(
                body.get("role_id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| ApiError::bad_request("role_id is required"))?,
                "role_id",
            )?;
            let role = community.role(&role_id);
            if op == "add" {
                member.add_role(&role).await.map_err(map_sdk_error)?;
            } else {
                member.remove_role(&role).await.map_err(map_sdk_error)?;
            }
        }
        "set" => {
            let ids = body
                .get("role_ids")
                .and_then(Value::as_array)
                .ok_or_else(|| ApiError::bad_request("role_ids is required"))?;
            let mut owned = Vec::new();
            for id in ids {
                let raw = id
                    .as_str()
                    .ok_or_else(|| ApiError::bad_request("role_ids must be strings"))?;
                owned.push(community.role(parse_hex_id(raw, "role_id")?));
            }
            let refs: Vec<&Role> = owned.iter().collect();
            member.set_roles(&refs).await.map_err(map_sdk_error)?;
        }
        _ => return Err(ApiError::bad_request("op must be add, remove, or set")),
    }
    audit("member_roles", &community_id, &format!("npub={npub} op={op}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "npub": npub, "op": op})))
}

async fn member_permissions(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, npub)): UrlPath<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npub = parse_npub(&npub)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "npub": npub, "permissions": 0})));
    };
    let permissions = bot
        .community(&community_id)
        .member(&npub)
        .permissions()
        .map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "npub": npub, "permissions": permissions.0})))
}

async fn edit_community(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let name = body.get("name").and_then(Value::as_str);
    let description = body.get("description").and_then(Value::as_str);
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id})));
    };
    bot.community(&community_id)
        .edit(name, description)
        .await
        .map_err(map_sdk_error)?;
    audit("edit_metadata", &community_id, "");
    Ok(Json(json!({"ok": true, "community_id": community_id})))
}

async fn set_image(
    state: AppState,
    community_id: String,
    body: Value,
    banner: bool,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let bytes = decode_image(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "banner": banner})));
    };
    let community = bot.community(&community_id);
    if banner {
        community.set_banner(&bytes).await.map_err(map_sdk_error)?;
    } else {
        community.set_icon(&bytes).await.map_err(map_sdk_error)?;
    }
    audit(if banner { "set_banner" } else { "set_icon" }, &community_id, &format!("bytes={}", bytes.len()));
    Ok(Json(json!({"ok": true, "community_id": community_id, "banner": banner})))
}

async fn set_icon(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    FileJsonBody(body): FileJsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    set_image(state, community_id, body, false).await
}

async fn set_banner(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    FileJsonBody(body): FileJsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    set_image(state, community_id, body, true).await
}

async fn remove_image(state: AppState, community_id: String, banner: bool) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "banner": banner})));
    };
    let community = bot.community(&community_id);
    if banner {
        community.remove_banner().await.map_err(map_sdk_error)?;
    } else {
        community.remove_icon().await.map_err(map_sdk_error)?;
    }
    audit(if banner { "remove_banner" } else { "remove_icon" }, &community_id, "");
    Ok(Json(json!({"ok": true, "community_id": community_id, "banner": banner})))
}

async fn remove_icon(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    remove_image(state, community_id, false).await
}

async fn remove_banner(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    remove_image(state, community_id, true).await
}

async fn list_pins(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "channel_id": channel_id, "pins": [], "sealed": false})));
    };
    let pins = bot
        .community(&community_id)
        .channel(&channel_id)
        .pins()
        .map_err(map_sdk_error)?;
    Ok(Json(json!({
        "community_id": community_id,
        "channel_id": channel_id,
        "pins": pins.iter().map(pin_json).collect::<Vec<_>>(),
        "sealed": false,
    })))
}

async fn pin_message(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let message_id = message_id(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "message_id": message_id})));
    };
    bot.community(&community_id)
        .channel(&channel_id)
        .pin(&message_id)
        .await
        .map_err(map_sdk_error)?;
    audit("pin", &community_id, &format!("channel_id={channel_id} message_id={message_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "message_id": message_id})))
}

async fn unpin_message(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, channel_id)): UrlPath<(String, String)>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let channel_id = parse_hex_id(&channel_id, "channel_id")?;
    let message_id = message_id(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "message_id": message_id})));
    };
    bot.community(&community_id)
        .channel(&channel_id)
        .unpin(&message_id)
        .await
        .map_err(map_sdk_error)?;
    audit("unpin", &community_id, &format!("channel_id={channel_id} message_id={message_id}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "channel_id": channel_id, "message_id": message_id})))
}

async fn list_nicknames(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "nicknames": []})));
    };
    let nicknames: Vec<Value> = bot
        .community(&community_id)
        .nicknames()
        .map_err(map_sdk_error)?
        .into_iter()
        .map(|(npub, nickname)| json!({"npub": npub, "nickname": nickname}))
        .collect();
    Ok(Json(json!({"community_id": community_id, "nicknames": nicknames})))
}

async fn set_nickname(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npub = body_npub(&body)?;
    let nickname = body
        .get("nickname")
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request("nickname is required"))?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npub": npub, "nickname": nickname})));
    };
    bot.community(&community_id)
        .member(&npub)
        .set_nickname(nickname)
        .await
        .map_err(map_sdk_error)?;
    audit("set_nickname", &community_id, &format!("npub={npub}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "npub": npub, "nickname": nickname})))
}

async fn audit_log(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "entries": [], "compacted": false})));
    };
    let log = bot
        .community(&community_id)
        .audit_log()
        .await
        .map_err(map_sdk_error)?;
    let mut body = audit_json(&log);
    if let Some(obj) = body.as_object_mut() {
        obj.insert("community_id".into(), json!(community_id));
    }
    Ok(Json(body))
}

async fn list_relays(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "relays": [], "voice_relays": []})));
    };
    let community = bot.community(&community_id);
    let relays = community.relays().map_err(map_sdk_error)?;
    let voice_relays = community.voice_relays().map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "relays": relays, "voice_relays": voice_relays})))
}

async fn check_relays(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let relays = relay_list(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "checks": []})));
    };
    let checks = bot
        .community(&community_id)
        .check_relays(&relays)
        .await
        .map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "checks": checks})))
}

async fn move_relays(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let relays = relay_list(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "relays": relays})));
    };
    let moved = bot
        .community(&community_id)
        .move_relays(&relays)
        .await
        .map_err(map_sdk_error)?;
    audit("move_relays", &community_id, &format!("relays={}", relays.join(",")));
    Ok(Json(json!({"ok": true, "community_id": community_id, "move": moved})))
}

async fn set_voice_relays(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let relays = relay_list(&body)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "voice_relays": relays})));
    };
    let stored = bot
        .community(&community_id)
        .set_voice_relays(&relays)
        .await
        .map_err(map_sdk_error)?;
    audit("voice_relays", &community_id, &format!("relays={}", stored.join(",")));
    Ok(Json(json!({"ok": true, "community_id": community_id, "voice_relays": stored})))
}

async fn list_invites(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "invites": []})));
    };
    let invites = bot
        .community(&community_id)
        .invite_links()
        .map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "invites": invites})))
}

async fn create_invite(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    if !public_invites_allowed() {
        return Err(ApiError::forbidden(
            "public invite links are off; set vector.communities.allow_public_invites: true (VECTOR_ALLOW_PUBLIC_INVITES=on). Minting one makes the community public",
        ));
    }
    let expires = body.get("expires_at_ms").and_then(Value::as_u64);
    let label = body
        .get("label")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|s| !s.is_empty());
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "url": "https://vector.example/invite/stub", "public_community": true})));
    };
    let url = if expires.is_some() || label.is_some() {
        bot.community(&community_id)
            .create_invite_with(expires, label)
            .await
    } else {
        bot.community(&community_id).create_invite().await
    }
    .map_err(map_sdk_error)?;
    audit("create_public_invite", &community_id, "public_community=true");
    Ok(Json(json!({
        "ok": true,
        "community_id": community_id,
        "url": url,
        "public_community": true,
        "warning": "This community is public while a live invite link exists",
    })))
}

fn parse_token(raw: &str) -> Result<String, ApiError> {
    let token = raw.trim();
    if token.is_empty()
        || token.len() > 256
        || !token.chars().all(|c| c.is_ascii_alphanumeric())
    {
        return Err(ApiError::bad_request("invalid invite token"));
    }
    Ok(token.to_string())
}

async fn revoke_invite(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath((community_id, token)): UrlPath<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let token = parse_token(&token)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "token": token})));
    };
    bot.community(&community_id)
        .revoke_invite(&token)
        .await
        .map_err(map_sdk_error)?;
    audit("revoke_invite", &community_id, &format!("token={token}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "token": token})))
}

async fn revoke_all_invites(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "revoked": 0, "failed": 0})));
    };
    let (revoked, failed) = bot
        .community(&community_id)
        .revoke_all_invites()
        .await
        .map_err(map_sdk_error)?;
    audit("revoke_all_invites", &community_id, &format!("revoked={revoked} failed={failed}"));
    Ok(Json(json!({"ok": true, "community_id": community_id, "revoked": revoked, "failed": failed})))
}

async fn raid_report(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "report": {}})));
    };
    let report = bot
        .community(&community_id)
        .raid_report()
        .map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "report": report})))
}

async fn contain_raid(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npubs = npub_list(
        body.get("npubs")
            .ok_or_else(|| ApiError::bad_request("npubs is required"))?,
    )?;
    if npubs.is_empty() {
        return Err(ApiError::bad_request("npubs is empty"));
    }
    let window = body
        .get("window_start_secs")
        .and_then(Value::as_u64)
        .ok_or_else(|| ApiError::bad_request("window_start_secs is required"))?;
    let cut_also = match body.get("cut_also") {
        Some(value) => npub_list(value)?,
        None => Vec::new(),
    };
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "refound_ok": false, "stub_report": true})));
    };
    let community = bot.community(&community_id);
    let report = if cut_also.is_empty() {
        community
            .contain_raid(&refs(&npubs), window)
            .await
            .map_err(map_sdk_error)?
    } else {
        community
            .contain_raid_with(&refs(&npubs), window, &refs(&cut_also))
            .await
            .map_err(map_sdk_error)?
    };
    audit(
        "contain_raid",
        &community_id,
        &format!("npubs={} refound_ok={}", npubs.join(","), report.refound_ok),
    );
    Ok(Json(json!({"ok": true, "community_id": community_id, "report": report})))
}

async fn explain_epoch(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "epoch": {}})));
    };
    let epoch = bot
        .community(&community_id)
        .explain_epoch()
        .await
        .map_err(map_sdk_error)?;
    Ok(Json(json!({"community_id": community_id, "epoch": epoch})))
}

async fn repair_roster(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "result": {}})));
    };
    let result = bot
        .community(&community_id)
        .repair_roster()
        .await
        .map_err(map_sdk_error)?;
    audit("repair_roster", &community_id, "");
    Ok(Json(json!({"ok": true, "community_id": community_id, "result": result})))
}

async fn rescue_stranded(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let npubs = npub_list(
        body.get("npubs")
            .ok_or_else(|| ApiError::bad_request("npubs is required"))?,
    )?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "npubs": npubs, "result": {}})));
    };
    let result = bot
        .community(&community_id)
        .rescue_stranded(&refs(&npubs))
        .await
        .map_err(map_sdk_error)?;
    audit("rescue_stranded", &community_id, &format!("npubs={}", npubs.join(",")));
    Ok(Json(json!({"ok": true, "community_id": community_id, "npubs": npubs, "result": result})))
}

async fn refound(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let retain = match body.get("retain") {
        Some(value) => npub_list(value)?,
        None => Vec::new(),
    };
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "retain": retain})));
    };
    bot.community(&community_id)
        .refound(&refs(&retain))
        .await
        .map_err(map_sdk_error)?;
    audit("refound", &community_id, &format!("retain={}", retain.join(",")));
    Ok(Json(json!({"ok": true, "community_id": community_id, "retain": retain})))
}

async fn list_policies(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "policies": []})));
    };
    let policies = bot.community(&community_id).policies().list().await.map_err(map_sdk_error)?;
    let rows: Vec<Value> = policies
        .into_iter()
        .map(|policy| {
            json!({
                "id": policy.id,
                "bytes": policy.bytes,
                "hash": policy.hash,
                "enabled": policy.enabled,
                "valid": policy.valid,
                "error": policy.error,
            })
        })
        .collect();
    Ok(Json(json!({"community_id": community_id, "policies": rows})))
}

async fn mutate_policy(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let action = body
        .get("action")
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request("action is required"))?;
    let Some(bot) = ready_bot(&state).await? else {
        if matches!(action, "set" | "stage" | "preview") {
            build_policy(&body)?;
        }
        if matches!(action, "set" | "stage" | "enable" | "delete") {
            policy_id(&body)?;
        }
        if action == "enable" && body.get("enabled").and_then(Value::as_bool).is_none() {
            return Err(ApiError::bad_request("enabled is required"));
        }
        if !matches!(action, "set" | "stage" | "enable" | "delete" | "preview") {
            return Err(ApiError::bad_request(
                "action must be set, stage, enable, delete, or preview",
            ));
        }
        return Ok(stub(json!({"ok": true, "community_id": community_id, "action": action, "valid": true, "flagged": []})));
    };
    let community = bot.community(&community_id);
    match action {
        "set" | "stage" => {
            let id = policy_id(&body)?;
            let policy = build_policy(&body)?;
            if action == "set" {
                community.policies().set(&id, policy).await.map_err(map_sdk_error)?;
            } else {
                community.policies().stage(&id, policy).await.map_err(map_sdk_error)?;
            }
            audit(action, &community_id, &format!("policy_id={id}"));
            Ok(Json(json!({"ok": true, "community_id": community_id, "action": action, "id": id})))
        }
        "enable" => {
            let id = policy_id(&body)?;
            let enabled = body
                .get("enabled")
                .and_then(Value::as_bool)
                .ok_or_else(|| ApiError::bad_request("enabled is required"))?;
            community
                .policies()
                .enable(&id, enabled)
                .await
                .map_err(map_sdk_error)?;
            audit("enable_policy", &community_id, &format!("policy_id={id} enabled={enabled}"));
            Ok(Json(json!({"ok": true, "community_id": community_id, "id": id, "enabled": enabled})))
        }
        "delete" => {
            let id = policy_id(&body)?;
            community.policies().delete(&id).await.map_err(map_sdk_error)?;
            audit("delete_policy", &community_id, &format!("policy_id={id}"));
            Ok(Json(json!({"ok": true, "community_id": community_id, "id": id})))
        }
        "preview" => {
            let policy = build_policy(&body)?;
            let preview = community
                .policies()
                .preview(&policy)
                .await
                .map_err(map_sdk_error)?;
            let row = |item: &vector_sdk::policy::PreviewRow| {
                json!({
                    "npub": item.npub,
                    "score": item.score,
                    "proven": item.proven,
                    "band": item.band,
                    "shield": item.shield,
                    "reasons": item.reasons,
                    "tenure_days": item.tenure_days,
                    "messages": item.messages,
                })
            };
            Ok(Json(json!({
                "ok": true,
                "community_id": community_id,
                "valid": preview.valid,
                "error": preview.error,
                "flagged": preview.flagged.iter().map(row).collect::<Vec<_>>(),
                "shielded_matches": preview.shielded_matches.iter().map(row).collect::<Vec<_>>(),
                "messages_cited": preview.messages_cited,
                "corpus": preview.corpus,
                "unevaluated": preview.unevaluated,
            })))
        }
        _ => Err(ApiError::bad_request(
            "action must be set, stage, enable, delete, or preview",
        )),
    }
}

async fn verdicts(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"community_id": community_id, "raid_detected": false, "members": []})));
    };
    let verdicts = bot
        .community(&community_id)
        .verdicts()
        .await
        .map_err(map_sdk_error)?;
    let coverage = verdicts.coverage();
    let members: Vec<Value> = verdicts
        .all()
        .map(|verdict| {
            json!({
                "npub": verdict.npub,
                "name": verdict.name(),
                "confidence": verdict.confidence(),
                "proven": verdict.proven,
                "band": verdict.band,
                "shield": verdict.shield,
                "reasons": verdict.reasons,
                "why": verdict.why(),
                "is_proven": verdict.is_proven(),
                "is_shielded": verdict.is_shielded(),
                "messages": verdict.messages,
                "tenure_secs": verdict.tenure_secs,
                "findings": verdict.findings.iter().map(|finding| json!({
                    "conviction_id": finding.conviction_id,
                    "policy_hash": finding.policy_hash,
                    "rule_id": finding.rule_id,
                    "scope": finding.scope,
                    "basis": finding.basis,
                    "severity": finding.severity,
                    "stateless": finding.stateless,
                    "rung": finding.rung,
                    "is_proven": finding.is_proven(),
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    Ok(Json(json!({
        "community_id": community_id,
        "raid_detected": verdicts.raid_detected(),
        "coverage": {
            "corpus": coverage.corpus,
            "corpus_max": coverage.corpus_max,
            "channels": coverage.channels,
            "complete": coverage.complete,
            "span_hours": coverage.span_hours(),
        },
        "members": members,
    })))
}

async fn dissolve(
    State(state): State<AppState>,
    _auth: crate::api::Auth,
    UrlPath(community_id): UrlPath<String>,
    JsonBody(body): JsonBody<Value>,
) -> Result<Json<Value>, ApiError> {
    let community_id = parse_community_id(&community_id)?;
    let confirm = parse_community_id(
        body.get("confirm_community_id")
            .and_then(Value::as_str)
            .unwrap_or(""),
    )?;
    if confirm != community_id {
        return Err(ApiError::bad_request(
            "confirm_community_id must match the community id",
        ));
    }
    let Some(bot) = ready_bot(&state).await? else {
        return Ok(stub(json!({"ok": true, "community_id": community_id, "dissolved": true})));
    };
    bot.community(&community_id)
        .dissolve()
        .await
        .map_err(map_sdk_error)?;
    audit("dissolve", &community_id, "irreversible=true");
    Ok(Json(json!({"ok": true, "community_id": community_id, "dissolved": true})))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_not_permitted_and_core_strings() {
        let denied = map_sdk_error(VectorError::NotPermitted {
            needs: Permissions(Permissions::BAN),
            action: "banning a member".into(),
        });
        assert_eq!(denied.code(), "forbidden");
        assert_eq!(denied.needs_bits(), Some(Permissions::BAN));

        let kick = map_sdk_error(VectorError::Other(
            "not authorized to kick this member".into(),
        ));
        assert_eq!(kick.code(), "forbidden");

        let missing = map_sdk_error(VectorError::NotFound("community aa".into()));
        assert_eq!(missing.code(), "not_found");

        let synced = map_sdk_error(VectorError::Other(
            "this channel follows the permissions of category abc, so a rule of its own would not apply".into(),
        ));
        assert_eq!(synced.code(), "conflict");

        let sealed = map_sdk_error(VectorError::Other(
            "the pins are sealed under a key this device doesn't hold yet".into(),
        ));
        assert_eq!(sealed.code(), "conflict");
    }

    #[test]
    fn decode_image_rejects_path_and_non_images() {
        let png = base64::engine::general_purpose::STANDARD.encode(b"\x89PNG\r\n\x1a\nrest");
        let jpeg = base64::engine::general_purpose::STANDARD.encode([0xffu8, 0xd8, 0xff, 0xe0]);
        let gif = base64::engine::general_purpose::STANDARD.encode(b"GIF89a\x01\x00");
        let webp = base64::engine::general_purpose::STANDARD.encode(b"RIFF\x24\x00\x00\x00WEBP");

        assert!(decode_image(&json!({"bytes_b64": png})).is_ok());
        assert!(decode_image(&json!({"bytes_b64": jpeg})).is_ok());
        assert!(decode_image(&json!({"bytes_b64": gif})).is_ok());
        assert!(decode_image(&json!({"bytes_b64": webp})).is_ok());

        // Path-based host file reads must be rejected even if bytes_b64 is also supplied.
        assert!(decode_image(&json!({"path": "/etc/passwd"})).is_err());
        assert!(decode_image(&json!({"path": "/etc/passwd", "bytes_b64": png})).is_err());

        // Plaintext / secrets / arbitrary bytes must fail magic-byte validation.
        let secret = base64::engine::general_purpose::STANDARD.encode(b"VECTOR_NSEC=nsec1secret");
        assert!(decode_image(&json!({"bytes_b64": secret})).is_err());

        // Missing or empty payloads fail.
        assert!(decode_image(&json!({})).is_err());
        assert!(decode_image(&json!({"bytes_b64": ""})).is_err());
    }
}
