"""Operator-only Concord community admin tools.

Tools are only exposed when ``check_admin_tools_available`` passes (live sidecar
and authorized caller). ``dispatch_admin_tool`` re-checks ``admin_caller_error``
on every invocation (fail-closed) and refuses anyone who is not the
``VECTOR_HOME_CHANNEL`` Vector DM, or a local CLI/TUI session with
``vector.communities.admin_cli`` enabled. Irreversible and delete actions also
require human confirmation via Hermes' ``pre_tool_call`` approval hook.
"""

from __future__ import annotations

import json
import os
import re
import weakref
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import quote

_PATH_KEYS = re.compile(r"\{([a-z_]+)\}")
_HEX_ID_RE = re.compile(r"^[0-9a-fA-F]{64}\Z")
_INVITE_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{1,256}\Z")
_HEX_PATH_KEYS = frozenset({"community_id", "channel_id", "category_id", "role_id"})
_REGISTERED_CTXS: "weakref.WeakSet[Any]" = weakref.WeakSet()

_SESSION_ENV_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_ACTOR_ID",
)

TOOL_NAMES = (
    "vector_community_inspect",
    "vector_community_moderate",
    "vector_community_channels",
    "vector_community_categories",
    "vector_community_roles",
    "vector_community_settings",
    "vector_community_invites",
    "vector_community_defense",
)

# Irreversible or destructive (tool, action) pairs that must pass Hermes'
# human-approval gate via the ``pre_tool_call`` hook.
_APPROVAL_ACTIONS: Dict[tuple[str, str], str] = {
    ("vector_community_defense", "dissolve"): "permanently dissolve Concord community",
    ("vector_community_defense", "refound"): "refound Concord community",
    ("vector_community_defense", "delete_policy"): "delete Concord moderation policy",
    ("vector_community_moderate", "ban"): "ban member(s) from Concord community",
    ("vector_community_moderate", "leave"): "leave Concord community",
    ("vector_community_roles", "grant_admin"): "grant admin privileges in Concord community",
    ("vector_community_settings", "move_relays"): "move Concord community relays",
    ("vector_community_channels", "delete"): "delete Concord channel",
    ("vector_community_categories", "delete"): "delete Concord category",
    ("vector_community_roles", "delete"): "delete Concord role",
}


def _imports():
    if __package__:
        from .bech32 import normalize_npub
        from .bridge_bin import _client_host
        from .constants import (
            DEFAULT_BRIDGE_HOST,
            DEFAULT_BRIDGE_PORT,
            SIDECAR_TOKEN_HEADER,
        )
        from .env import _scoped_env, _scoped_env_str
        from .paths import _read_runtime_record
    else:
        from bech32 import normalize_npub
        from bridge_bin import _client_host
        from constants import (
            DEFAULT_BRIDGE_HOST,
            DEFAULT_BRIDGE_PORT,
            SIDECAR_TOKEN_HEADER,
        )
        from env import _scoped_env, _scoped_env_str
        from paths import _read_runtime_record
    return {
        "normalize_npub": normalize_npub,
        "client_host": _client_host,
        "default_host": DEFAULT_BRIDGE_HOST,
        "default_port": DEFAULT_BRIDGE_PORT,
        "token_header": SIDECAR_TOKEN_HEADER,
        "scoped_env": _scoped_env,
        "scoped_env_str": _scoped_env_str,
        "read_runtime": _read_runtime_record,
    }


def _flag_on(name: str, *, default: bool) -> bool:
    raw = _imports()["scoped_env_str"](name).strip().lower()
    if not raw:
        return default
    return raw in ("on", "true", "1", "yes")


def _session_env() -> Optional[Dict[str, str]]:
    """Read session routing variables by name. Returns None on import/read error (fail-closed)."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None
    env: Dict[str, str] = {}
    for key in _SESSION_ENV_KEYS:
        try:
            value = get_session_env(key, "")
        except Exception:
            return None
        env[key] = "" if value is None else str(value)
    return env


def admin_caller_error() -> Optional[str]:
    """None when this caller may run community admin tools."""
    env = _session_env()
    if not isinstance(env, dict):
        return "Community admin tools are refused: session context is unavailable."
    platform = str(env.get("HERMES_SESSION_PLATFORM") or "").strip().lower()
    chat_type = str(env.get("HERMES_SESSION_CHAT_TYPE") or "").strip().lower()
    chat_id = str(env.get("HERMES_SESSION_CHAT_ID") or "").strip()
    actor = str(env.get("HERMES_SESSION_ACTOR_ID") or "").strip()

    if chat_type == "group":
        return "Community admin tools are refused in group chats."
    if platform and platform != "vector":
        return "Community admin tools are refused on this platform."
    if chat_type and chat_type != "dm":
        return (
            "Community admin tools are only available in the operator's "
            "Vector DM or local CLI."
        )
    if platform == "vector" or chat_type == "dm":
        if platform != "vector" or chat_type != "dm":
            return (
                "Community admin tools are only available in the operator's "
                "Vector DM or local CLI."
            )
        if not actor:
            return "Community admin tools are refused when the turn has no trusted sender."
        normalize = _imports()["normalize_npub"]
        home = normalize(_imports()["scoped_env_str"]("VECTOR_HOME_CHANNEL").strip() or "")
        if not home or normalize(chat_id) != home or normalize(actor) != home:
            return "Community admin tools are limited to the VECTOR_HOME_CHANNEL operator DM."
        return None
    if not _flag_on("VECTOR_ADMIN_CLI", default=False):
        return (
            "CLI/TUI community admin is off. Set vector.communities.admin_cli: true "
            "in config.yaml."
        )
    return None


def _pid_alive(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 1:
        return False
    if os.name != "posix":
        return True
    try:
        os.kill(pid_int, 0)
    except OSError:
        return False
    return True


def _sidecar_running() -> bool:
    """Fast local check: runtime record exists with a token and live PID."""
    try:
        record = _imports()["read_runtime"]()
    except Exception:
        return False
    return bool(record and record.get("token") and _pid_alive(record.get("pid")))


def check_admin_tools_available() -> bool:
    """Expose admin tools only when the sidecar is running and this caller is allowed."""
    if not _sidecar_running():
        return False
    return admin_caller_error() is None


def _mark_check_fn_no_cache() -> None:
    """Prevent Hermes from TTL-caching our session-dependent check_fn across sessions."""
    try:
        from tools.registry import no_cache_check_fn
    except Exception:
        return
    if callable(no_cache_check_fn):
        no_cache_check_fn(check_admin_tools_available)


_mark_check_fn_no_cache()


def _coerce_port(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


async def sidecar_json(
    method: str,
    path: str,
    body: Optional[dict] = None,
) -> Dict[str, Any]:
    """Call the live vector-bridge using the gateway runtime record."""
    import httpx

    deps = _imports()
    record = deps["read_runtime"]()
    token = None
    port = _coerce_port(deps["scoped_env"]("VECTOR_BRIDGE_PORT"), deps["default_port"])
    host = deps["client_host"](
        str(deps["scoped_env"]("VECTOR_BRIDGE_HOST") or deps["default_host"])
    )
    stale = ""
    if record and record.get("token"):
        if _pid_alive(record.get("pid")):
            token = str(record["token"])
            port = _coerce_port(record.get("port"), port)
        else:
            stale = " The gateway sidecar is not running."
    if not token:
        return {
            "ok": False,
            "error": "Vector community admin needs a running sidecar." + stale,
        }
    url = f"http://{host}:{port}{path}"
    headers = {deps["token_header"]: token}
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.request(method, url, headers=headers, json=body)
    except Exception as exc:
        return {"ok": False, "error": f"Vector admin request failed: {exc}"}
    try:
        payload = response.json()
    except Exception:
        payload = {"error": (response.text or "")[:500]}
    if not isinstance(payload, dict):
        payload = {"result": payload}
    if response.status_code >= 400:
        payload.setdefault("ok", False)
        payload["status"] = response.status_code
    return payload


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "error": message})


def _validate_path_value(key: str, raw: str) -> str:
    if key in _HEX_PATH_KEYS:
        if not _HEX_ID_RE.match(raw):
            raise ValueError(f"{key} must be 64 hex characters")
        return quote(raw.lower(), safe="")
    if key == "npub":
        normalized = _imports()["normalize_npub"](raw)
        if not normalized:
            raise ValueError("npub is invalid")
        return quote(normalized, safe="")
    if key == "token":
        if not _INVITE_TOKEN_RE.match(raw):
            raise ValueError("token must be 1 to 256 ASCII alphanumeric characters")
        return quote(raw, safe="")
    return quote(raw, safe="")


def _path_and_body(template: str, args: Dict[str, Any], send_body: bool):
    missing = []

    def repl(match: re.Match) -> str:
        key = match.group(1)
        value = args.get(key)
        if value is None or str(value).strip() == "":
            missing.append(key)
            return ""
        return _validate_path_value(key, str(value).strip())

    path = _PATH_KEYS.sub(repl, template)
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    if not send_body:
        return path, None
    used = set(_PATH_KEYS.findall(template)) | {"action"}
    body = {key: value for key, value in args.items() if key not in used and value is not None}
    return path, body


# method, path, send JSON body
_ROUTES: Dict[str, Dict[str, tuple]] = {
    "vector_community_inspect": {
        "list": ("GET", "/communities", False),
        "capabilities": ("GET", "/communities/{community_id}/capabilities", False),
        "roles": ("GET", "/communities/{community_id}/roles", False),
        "members": ("GET", "/communities/{community_id}/members", False),
        "timeouts": ("GET", "/communities/{community_id}/members", False),
    },
    "vector_community_moderate": {
        "invite": ("POST", "/communities/invite", True),
        "kick": ("POST", "/communities/{community_id}/members/kick", True),
        "ban": ("POST", "/communities/{community_id}/members/ban", True),
        "unban": ("POST", "/communities/{community_id}/members/unban", True),
        "timeout": ("POST", "/communities/{community_id}/members/timeout", True),
        "remove_timeout": ("DELETE", "/communities/{community_id}/members/timeout", True),
        "hide_message": ("POST", "/communities/{community_id}/channels/{channel_id}/hide", True),
        "leave": ("POST", "/communities/{community_id}/leave", False),
    },
    "vector_community_channels": {
        "list": ("GET", "/communities/{community_id}/channels", False),
        "create": ("POST", "/communities/{community_id}/channels", True),
        "update": ("PATCH", "/communities/{community_id}/channels/{channel_id}", True),
        "duplicate": ("POST", "/communities/{community_id}/channels/{channel_id}/duplicate", True),
        "delete": ("DELETE", "/communities/{community_id}/channels/{channel_id}", False),
        "set_overwrite": ("PUT", "/communities/{community_id}/channels/{channel_id}/overwrites", True),
        "clear_overwrite": ("DELETE", "/communities/{community_id}/channels/{channel_id}/overwrites", True),
        "sync_permissions": ("POST", "/communities/{community_id}/channels/{channel_id}/sync", True),
        "grant_access": ("POST", "/communities/{community_id}/channels/{channel_id}/access/grant", True),
        "revoke_access": ("POST", "/communities/{community_id}/channels/{channel_id}/access/revoke", True),
        "access_list": ("GET", "/communities/{community_id}/channels/{channel_id}/access", False),
        "set_system_channel": ("PUT", "/communities/{community_id}/system-channel", True),
        "set_landing_channel": ("PUT", "/communities/{community_id}/landing-channel", True),
    },
    "vector_community_categories": {
        "list": ("GET", "/communities/{community_id}/categories", False),
        "create": ("POST", "/communities/{community_id}/categories", True),
        "update": ("PATCH", "/communities/{community_id}/categories/{category_id}", True),
        "delete": ("DELETE", "/communities/{community_id}/categories/{category_id}", False),
        "set_overwrite": ("PUT", "/communities/{community_id}/categories/{category_id}/overwrites", True),
    },
    "vector_community_roles": {
        "create": ("POST", "/communities/{community_id}/roles", True),
        "edit": ("PATCH", "/communities/{community_id}/roles/{role_id}", True),
        "delete": ("DELETE", "/communities/{community_id}/roles/{role_id}", False),
        "reorder": ("PUT", "/communities/{community_id}/roles/order", True),
        "get_everyone": ("GET", "/communities/{community_id}/roles/everyone", False),
        "set_everyone": ("PUT", "/communities/{community_id}/roles/everyone", True),
        "grant_admin": ("POST", "/communities/{community_id}/members/admin", True),
        "revoke_admin": ("POST", "/communities/{community_id}/members/admin", True),
        "add_role": ("POST", "/communities/{community_id}/members/roles", True),
        "remove_role": ("POST", "/communities/{community_id}/members/roles", True),
        "set_roles": ("POST", "/communities/{community_id}/members/roles", True),
        "member_permissions": ("GET", "/communities/{community_id}/members/{npub}/permissions", False),
    },
    "vector_community_settings": {
        "edit_metadata": ("PATCH", "/communities/{community_id}", True),
        "set_icon": ("PUT", "/communities/{community_id}/icon", True),
        "remove_icon": ("DELETE", "/communities/{community_id}/icon", False),
        "set_banner": ("PUT", "/communities/{community_id}/banner", True),
        "remove_banner": ("DELETE", "/communities/{community_id}/banner", False),
        "list_pins": ("GET", "/communities/{community_id}/channels/{channel_id}/pins", False),
        "pin": ("POST", "/communities/{community_id}/channels/{channel_id}/pins", True),
        "unpin": ("DELETE", "/communities/{community_id}/channels/{channel_id}/pins", True),
        "list_nicknames": ("GET", "/communities/{community_id}/nicknames", False),
        "set_nickname": ("PUT", "/communities/{community_id}/nicknames", True),
        "audit_log": ("GET", "/communities/{community_id}/audit-log", False),
        "list_relays": ("GET", "/communities/{community_id}/relays", False),
        "check_relays": ("POST", "/communities/{community_id}/relays/check", True),
        "move_relays": ("PUT", "/communities/{community_id}/relays", True),
        "set_voice_relays": ("PUT", "/communities/{community_id}/voice-relays", True),
    },
    "vector_community_invites": {
        "list_links": ("GET", "/communities/{community_id}/invites", False),
        "revoke_link": ("DELETE", "/communities/{community_id}/invites/{token}", False),
        "revoke_all_links": ("DELETE", "/communities/{community_id}/invites", False),
        "create_link": ("POST", "/communities/{community_id}/invites", True),
    },
    "vector_community_defense": {
        "raid_report": ("GET", "/communities/{community_id}/raid-report", False),
        "contain_raid": ("POST", "/communities/{community_id}/contain-raid", True),
        "explain_epoch": ("GET", "/communities/{community_id}/epoch", False),
        "repair_roster": ("POST", "/communities/{community_id}/repair-roster", False),
        "rescue_stranded": ("POST", "/communities/{community_id}/rescue-stranded", True),
        "refound": ("POST", "/communities/{community_id}/refound", True),
        "list_policies": ("GET", "/communities/{community_id}/policies", False),
        "preview_policy": ("POST", "/communities/{community_id}/policies", True),
        "stage_policy": ("POST", "/communities/{community_id}/policies", True),
        "set_policy": ("POST", "/communities/{community_id}/policies", True),
        "enable_policy": ("POST", "/communities/{community_id}/policies", True),
        "delete_policy": ("POST", "/communities/{community_id}/policies", True),
        "verdicts": ("GET", "/communities/{community_id}/verdicts", False),
        "dissolve": ("POST", "/communities/{community_id}/dissolve", True),
    },
}

_POLICY_ACTION = {
    "preview_policy": "preview",
    "stage_policy": "stage",
    "set_policy": "set",
    "enable_policy": "enable",
    "delete_policy": "delete",
}


def pre_tool_call_approval(
    *,
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    **_kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Escalate irreversible and delete community-admin actions to Hermes human approval."""
    if tool_name not in _ROUTES:
        return None
    payload = args if isinstance(args, dict) else {}
    action = str(payload.get("action") or "").strip()
    summary = _APPROVAL_ACTIONS.get((tool_name, action))
    if not summary:
        return None
    community_id = str(payload.get("community_id") or "").strip().lower()
    details: list[str] = []
    if community_id:
        details.append(f"community={community_id}")
    for key in ("npub", "channel_id", "category_id", "role_id", "id"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            details.append(f"{key}={val.strip()}")
    for list_key in ("npubs", "relays", "retain"):
        vals = payload.get(list_key)
        if isinstance(vals, list) and vals:
            items = [str(v).strip() for v in vals[:5] if str(v).strip()]
            if items:
                preview = ", ".join(items)
                if len(vals) > 5:
                    preview += f" (+{len(vals) - 5} more)"
                details.append(f"{list_key}=[{preview}]")
    suffix = f" ({', '.join(details)})" if details else ""
    rule_scope = community_id if _HEX_ID_RE.match(community_id) else "global"
    return {
        "action": "approve",
        "message": f"Confirm {summary}{suffix}",
        "rule_key": f"{tool_name}:{action}:{rule_scope}",
    }


def _prepare(tool: str, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
    prepared = dict(args)
    if tool == "vector_community_roles":
        if action == "grant_admin":
            prepared["grant"] = True
        elif action == "revoke_admin":
            prepared["grant"] = False
        elif action == "add_role":
            prepared["op"] = "add"
        elif action == "remove_role":
            prepared["op"] = "remove"
        elif action == "set_roles":
            prepared["op"] = "set"
    if tool == "vector_community_settings" and action in ("set_icon", "set_banner"):
        if prepared.get("path") is not None:
            raise ValueError(
                "Local file path is not supported for icon or banner; pass bytes_b64 instead."
            )
        prepared.pop("path", None)
        b64 = prepared.get("bytes_b64")
        if not isinstance(b64, str) or not b64.strip():
            raise ValueError("bytes_b64 is required for icon or banner.")
    if tool == "vector_community_defense" and action in _POLICY_ACTION:
        prepared["action"] = _POLICY_ACTION[action]
    if tool == "vector_community_invites" and action == "create_link":
        if not _flag_on("VECTOR_ALLOW_PUBLIC_INVITES", default=False):
            raise PermissionError(
                "Public invite links are off. Set vector.communities.allow_public_invites: true. "
                "Minting one makes the community public."
            )
    if tool == "vector_community_defense" and action == "dissolve":
        community_id = str(prepared.get("community_id") or "").strip().lower()
        confirm = str(prepared.get("confirm_community_id") or "").strip().lower()
        if not community_id or confirm != community_id:
            raise PermissionError(
                "Dissolving a community requires confirm_community_id equal to community_id."
            )
    return prepared


async def dispatch_admin_tool(tool: str, args: Optional[Dict[str, Any]]) -> str:
    """Run one community-admin tool action. Returns a JSON string."""
    refused = admin_caller_error()
    if refused:
        return _fail(refused)
    payload = args if isinstance(args, dict) else {}
    action = str(payload.get("action") or "").strip()
    routes = _ROUTES.get(tool) or {}
    route = routes.get(action)
    if route is None:
        return _fail(f"Unknown action {action!r}. Choose one of: {', '.join(routes)}.")
    try:
        prepared = _prepare(tool, action, payload)
        method, template, send_body = route
        path, body = _path_and_body(template, prepared, send_body)
    except PermissionError as exc:
        return _fail(str(exc))
    except ValueError as exc:
        return _fail(str(exc))
    # Policy routes need the sidecar action name, which is also a path key
    # substitute only when the template contains `{action}` — it does not.
    # `_prepare` stored `action` as the policy verb, and `_path_and_body`
    # drops the key named action. Put the policy verb back.
    if tool == "vector_community_defense" and action in _POLICY_ACTION and isinstance(body, dict):
        body["action"] = _POLICY_ACTION[action]
    result = await sidecar_json(method, path, body)
    return json.dumps(result)


def _schema(name: str, description: str, actions: Dict[str, tuple]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(actions),
                    "description": "Which admin operation to run.",
                },
                "community_id": {"type": "string", "description": "64-hex Concord community id."},
                "channel_id": {"type": "string"},
                "category_id": {"type": "string"},
                "role_id": {"type": "string"},
                "role_ids": {"type": "array", "items": {"type": "string"}},
                "npub": {"type": "string"},
                "npubs": {"type": "array", "items": {"type": "string"}},
                "message_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "topic": {"type": "string"},
                "view": {"type": "string", "description": "chat, forum, or voice."},
                "private": {"type": "boolean"},
                "read_only": {"type": "boolean"},
                "index": {"type": "integer"},
                "seconds": {"type": "integer", "description": "Timeout length, 1 to 2419200."},
                "permissions": {
                    "description": "Permission bit mask or list of permission names.",
                },
                "color": {"type": "integer"},
                "shown": {"type": "boolean"},
                "mentionable": {"type": "boolean"},
                "nickname": {"type": "string"},
                "target": {"description": '"everyone", {"role": id}, or {"member": npub}.'},
                "allow": {"description": "Permission bits or names to allow."},
                "deny": {"description": "Permission bits or names to deny."},
                "relays": {"type": "array", "items": {"type": "string"}},
                "bytes_b64": {
                    "type": "string",
                    "description": "Base64-encoded PNG, JPEG, WebP, or GIF image (up to 4 MiB) for icon or banner.",
                },
                "expires_at_ms": {"type": "integer"},
                "label": {"type": "string"},
                "token": {"type": "string"},
                "window_start_secs": {"type": "integer"},
                "cut_also": {"type": "array", "items": {"type": "string"}},
                "retain": {"type": "array", "items": {"type": "string"}},
                "preset": {"type": "string"},
                "document": {"description": "Policy JSON object or string."},
                "id": {"type": "string", "description": "Stored policy id."},
                "enabled": {"type": "boolean"},
                "confirm_community_id": {
                    "type": "string",
                    "description": "Repeat community_id to dissolve.",
                },
            },
            "required": ["action"],
            "additionalProperties": True,
        },
    }


_DESCRIPTIONS = {
    "vector_community_inspect": (
        "Read a Concord community the bot belongs to: list, capabilities, roles, "
        "members (includes bans and timeouts). Operator DM or local CLI only."
    ),
    "vector_community_moderate": (
        "Moderate a Concord community: direct-invite, kick, ban, unban, timeout, "
        "hide another member's message, or leave. Operator DM or local CLI only."
    ),
    "vector_community_channels": (
        "Create and edit Concord channels, categories sync, private-channel access, "
        "and the system or landing channel. Operator DM or local CLI only."
    ),
    "vector_community_categories": (
        "Create, rename, move, delete, and set permission overwrites on Concord "
        "categories. Operator DM or local CLI only."
    ),
    "vector_community_roles": (
        "Create and assign Concord roles, including @admin and @everyone permissions. "
        "Operator DM or local CLI only."
    ),
    "vector_community_settings": (
        "Edit community name, icon, banner, pins, nicknames, audit log, and relays. "
        "Operator DM or local CLI only."
    ),
    "vector_community_invites": (
        "List or revoke public invite links. create_link is refused unless "
        "vector.communities.allow_public_invites is true, because a live link "
        "makes the community public."
    ),
    "vector_community_defense": (
        "Raid report, containment, epoch repair, policies, and dissolve. "
        "dissolve requires confirm_community_id equal to community_id. "
        "Policy staging and preview do not enable enforcement; enable_policy does."
    ),
}


def register_tools(ctx) -> None:
    """Register community admin tools and pre_tool_call approval hook. Idempotent per ctx."""
    try:
        if ctx in _REGISTERED_CTXS:
            return
    except TypeError:
        pass
    _mark_check_fn_no_cache()
    for name in TOOL_NAMES:
        routes = _ROUTES[name]
        description = _DESCRIPTIONS[name]

        async def handler(args, _name=name, **_kwargs):
            return await dispatch_admin_tool(_name, args)

        ctx.register_tool(
            name=name,
            toolset="vector",
            schema=_schema(name, description, routes),
            handler=handler,
            check_fn=check_admin_tools_available,
            description=description,
            emoji="🛡️",
            is_async=True,
        )
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_tool_call", pre_tool_call_approval)
    try:
        _REGISTERED_CTXS.add(ctx)
    except TypeError:
        pass


# Keep the annotation import used if a type checker looks at the handler alias.
_Handler = Callable[..., Awaitable[str]]
