"""Vector platform adapter for Hermes Agent.

Registers the ``vector`` platform, npub helpers, and a ``BasePlatformAdapter``
that owns a Rust ``vector-bridge`` sidecar over loopback HTTP/SSE.

Vector users are identified by a bech32 ``npub1…`` public key. Session mapping
is ``chat_id = user_id = peer npub``, ``chat_type = "dm"`` for DMs, and
``chat_id = channel hex``, ``chat_type = "group"``, ``user_id = sender npub``
for Concord channels.

Required env vars / config.extra keys:
    VECTOR_NPUB                  Bot public key (npub1…)
    VECTOR_ALLOWED_USERS         Comma-separated npubs allowed to DM (also
                                 grants community turns; pairing is DM-only)
    VECTOR_HOME_CHANNEL          Operator npub for cron + join notices
    VECTOR_PAIRING               on (default) = pairing codes; off = drop unauthorized
    VECTOR_BRIDGE_PORT           HTTP port (default 8096)
    VECTOR_BRIDGE_HOST           Bind address (default 127.0.0.1)
    VECTOR_REACTIONS             on = 👀/✅/❌ processing acks; default off
    VECTOR_GROUP_ALLOWED_USERS    Npubs who may trigger community turns without DMs
    VECTOR_GROUP_ALLOW_ALL       Channel ids where any member may @mention / reply
    VECTOR_TRUSTED_INVITERS      Optional inviter npubs (default: ALLOWED_USERS)
    VECTOR_INVITE_POLICY         manual | whitelist (default whitelist)
    VECTOR_CREATE_COMMUNITY      on = bot-owned home community after Ready
    VECTOR_SLASH_COMMANDS        off = do not publish Vector / picker commands
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import httpx

from gateway.config import Platform, PlatformConfig
from hermes_constants import get_hermes_home
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_url,
)

logger = logging.getLogger("hermes_plugins.vector_platform.adapter")

# ---------------------------------------------------------------------------
# Plugin identity / paths
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.2.0"
_PLUGIN_ROOT = Path(__file__).resolve().parent
_BRIDGE_DIR = _PLUGIN_ROOT / "bridge"
_DEFAULT_BRIDGE_BIN = _BRIDGE_DIR / "target" / "release" / "vector-bridge"

DEFAULT_BRIDGE_PORT = 8096
DEFAULT_BRIDGE_HOST = "127.0.0.1"
# VectorBot::build is usually ~1s; slash kind-10304 used to run *before*
# Ready and take 20–40s. /health is now ready after build, but Hermes still
# wraps connect() in HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT (default 30).
# Keep VECTOR_STARTUP_TIMEOUT below that floor unless the operator raised it.
DEFAULT_STARTUP_TIMEOUT = 60
HERMES_CONNECT_TIMEOUT_FLOOR = 90
MAX_MESSAGE_LENGTH = 4000
AVATAR_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MIN_RUSTC = (1, 75)
CARGO_BUILD_TIMEOUT = 900
BRIDGE_CHECK_TIMEOUT = 30
BRIDGE_SETUP_TIMEOUT = 90

SIDECAR_TOKEN_HEADER = "X-Hermes-Sidecar-Token"
HEALTH_POLL_INTERVAL = 0.5
HEALTH_CHECK_INTERVAL = 30.0
SSE_RETRY_DELAY_INITIAL = 2.0
SSE_RETRY_DELAY_MAX = 60.0
SSE_STALE_TIMEOUT = 60.0
BRIDGE_TERM_WAIT = 2.0
INBOUND_DEDUP_MAX = 1024
LAST_INBOUND_CHATS_MAX = 200
SENT_IDS_MAX = 1000
RUNTIME_RECORD_NAME = "vector-sidecar.json"
NOTIFIED_CHANNELS_FILE = "notified-channels.json"
INBOX_NAME_MAX = 180
DOWNLOAD_TIMEOUT = 120.0
SEND_FILE_TIMEOUT = 120.0
DEFAULT_INBOUND_MEDIA_MAX_BYTES = 128 * 1024 * 1024


def _ensure_hermes_connect_timeout_floor() -> None:
    """Slash-manifest / Vector login can exceed Hermes' 30s default wrap.

    Must run from ``register()``, not only ``connect()``: Hermes reads
    ``HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT`` *before* wrapping
    ``connect()``, so setting it inside ``connect()`` misses the first
    attempt. Only fills the env when unset so an operator override wins.
    """
    if os.getenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "").strip():
        return
    os.environ["HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT"] = str(
        HERMES_CONNECT_TIMEOUT_FLOOR
    )
    logger.info(
        "Vector: HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT unset; flooring to %ss",
        HERMES_CONNECT_TIMEOUT_FLOOR,
    )


def resolve_bridge_bin() -> Path:
    """Return VECTOR_BRIDGE_BIN if set, else the in-tree release binary path."""
    override = (os.getenv("VECTOR_BRIDGE_BIN") or "").strip()
    if override:
        return Path(override)
    return _DEFAULT_BRIDGE_BIN


def resolve_data_dir() -> Path:
    """Default VECTOR_DATA_DIR: plugin-data/vector-platform/sdk."""
    override = (os.getenv("VECTOR_DATA_DIR") or "").strip()
    if override:
        return Path(override)
    try:
        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    return Path(home) / "plugin-data" / "vector-platform" / "sdk"


def resolve_files_root() -> Path:
    """Durable inbox root: plugin-data/vector-platform/files (not sdk/)."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir("vector-platform") / "files"
    except Exception:
        try:
            home = get_hermes_home()
        except Exception:
            home = Path.home() / ".hermes"
        return Path(home) / "plugin-data" / "vector-platform" / "files"


def validate_bot_image_src(src: str) -> Path:
    """Resolve a local image path; raise ValueError if it cannot be used."""
    path = Path(src).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    suffix = path.suffix.lower()
    if suffix not in AVATAR_SUFFIXES:
        raise ValueError("image must be jpg, png, webp, or gif")
    return path


def install_bot_image(src: str, data_dir: Path, stem: str) -> Path:
    """Copy a local image into VECTOR_DATA_DIR as ``{stem}.<ext>``.

    Kind-0 pictures/banners are public (Blossom, not gift-wrap). The copy is
    durable so setup can take a file from Downloads without depending on that
    path later.
    """
    if stem not in ("avatar", "banner"):
        raise ValueError("stem must be avatar or banner")
    path = validate_bot_image_src(src)
    suffix = path.suffix.lower()
    dest_suffix = ".jpg" if suffix == ".jpeg" else suffix
    dest = Path(data_dir) / f"{stem}{dest_suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != dest.resolve():
        shutil.copy2(path, dest)
    return dest


def validate_bot_avatar_src(src: str) -> Path:
    return validate_bot_image_src(src)


def install_bot_avatar(src: str, data_dir: Path) -> Path:
    return install_bot_image(src, data_dir, "avatar")


def _sanitize_filename(name: str, fallback: str = "file") -> str:
    """Keep a portable basename; empty or dotted-only names become fallback."""
    raw = (name or "").strip().replace("\x00", "")
    raw = Path(raw).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    return cleaned[:INBOX_NAME_MAX]


def _unique_path(directory: Path, filename: str) -> Path:
    """Return directory/filename, adding -2, -3, … on collision."""
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _mime_for_attachment(att: dict) -> str:
    name = str(att.get("name") or "")
    ext = str(att.get("extension") or "").lstrip(".")
    probe = name if "." in Path(name).name else (f"x.{ext}" if ext else name)
    guessed, _ = mimetypes.guess_type(probe)
    return guessed or "application/octet-stream"


def _message_type_for_mime(mime: str) -> MessageType:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return MessageType.PHOTO
    if mime.startswith("video/"):
        return MessageType.VIDEO
    if mime.startswith("audio/"):
        if mime in {"audio/ogg", "audio/opus", "audio/ogg; codecs=opus"}:
            return MessageType.VOICE
        return MessageType.AUDIO
    return MessageType.DOCUMENT


def _inbound_media_max_bytes() -> int:
    try:
        from gateway.platforms.base import get_inbound_media_max_bytes

        return int(get_inbound_media_max_bytes())
    except Exception:
        return DEFAULT_INBOUND_MEDIA_MAX_BYTES


def bridge_port_is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    """Return True if something already accepts TCP connections on host:port."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _runtime_record_path() -> Path:
    try:
        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    return Path(home) / "runtime" / RUNTIME_RECORD_NAME


def _write_runtime_record(port: int, token: str, pid: int, npub: Optional[str] = None) -> None:
    """Atomically persist ``{port, token, pid, npub}`` with owner-only perms."""
    try:
        path = _runtime_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".vector-sidecar.", suffix=".tmp"
        )
        try:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            payload: Dict[str, Any] = {"port": port, "token": token, "pid": pid}
            if npub:
                payload["npub"] = npub
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.warning("Vector: failed to write sidecar runtime record: %s", e)


def _read_runtime_record() -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(_runtime_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _delete_runtime_record() -> None:
    try:
        _runtime_record_path().unlink(missing_ok=True)
    except OSError:
        pass


def _client_host(bind_host: str) -> str:
    """HTTP client host for a sidecar bind address (bind-all → loopback)."""
    host = (bind_host or DEFAULT_BRIDGE_HOST).strip()
    if host in ("0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return host


def _identity_nsec_present(data_dir: Path) -> bool:
    path = Path(data_dir) / "identity.nsec"
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _find_listener_pids(port: int) -> List[int]:
    """PIDs listening on a local TCP port (empty if none/undeterminable)."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(tok) for tok in out.stdout.split() if tok.strip().isdigit()]


def _pid_is_vector_bridge(pid: int) -> bool:
    """True if ``pid``'s command line looks like vector-bridge."""
    if pid <= 1:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "vector-bridge" in (out.stdout or "")


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _host_is_loopback(host: str) -> bool:
    h = (host or "").strip().lower().strip("[]")
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# bech32 (BIP-173) helpers — copied from plugins/platforms/buzz/adapter.py
# (hex_to_npub / npub_to_hex). Charset qpzry9x8gf2tvdw0s3jn54khce6mua7l,
# 32-byte payload. No nostr pip dep.
# ---------------------------------------------------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: List[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> List[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits: int, tobits: int, pad: bool = True) -> Optional[List[int]]:
    acc = 0
    bits = 0
    ret: List[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def hex_to_npub(pubkey_hex: str) -> Optional[str]:
    """Encode a 64-char hex pubkey as an ``npub1…`` bech32 string."""
    try:
        raw = bytes.fromhex(pubkey_hex)
    except ValueError:
        return None
    if len(raw) != 32:
        return None
    data = _convertbits(raw, 8, 5)
    if data is None:
        return None
    values = _bech32_hrp_expand("npub") + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return "npub1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)


def npub_to_hex(npub: str) -> Optional[str]:
    """Decode an ``npub1…`` bech32 string to a 64-char hex pubkey."""
    npub = npub.strip().lower()
    if not npub.startswith("npub1"):
        return None
    data_part = npub[len("npub1"):]
    try:
        data = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError:
        return None
    if _bech32_polymod(_bech32_hrp_expand("npub") + data) != 1:
        return None
    decoded = _convertbits(data[:-6], 5, 8, pad=False)
    if decoded is None or len(decoded) != 32:
        return None
    return bytes(decoded).hex()


def normalize_npub(ref: str) -> Optional[str]:
    """Canonical ``npub1…`` from hex, ``npub1``, or ``nostr:npub1`` (plus whitespace)."""
    raw = (ref or "").strip()
    if raw.lower().startswith("nostr:"):
        raw = raw[6:].strip()
    if raw.lower().startswith("npub1"):
        hx = npub_to_hex(raw)
        return hex_to_npub(hx) if hx else None
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return hex_to_npub(raw.lower())
    return None


_CHANNEL_ID_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def normalize_channel_id(ref: str) -> Optional[str]:
    """64-char hex Concord channel id, lowercased."""
    raw = (ref or "").strip()
    if _CHANNEL_ID_RE.fullmatch(raw):
        return raw.lower()
    return None


def _parse_npub_target(ref: str) -> Optional[tuple[str, Optional[str]]]:
    """DM-only parse: hex / ``npub1`` / ``nostr:npub1`` → ``(npub, None)``."""
    npub = normalize_npub(ref)
    return (npub, None) if npub else None


def _parse_target_ref(ref: str) -> Optional[tuple[str, Optional[str]]]:
    """parse_target_ref_fn: DM npub or known Concord channel hex.

    64-hex that we have seen as a joined community channel (inbound, Ready
    roster, home-community create, or ``VECTOR_GROUP_ALLOW_ALL``) stays a
    channel id. Anything else that ``normalize_npub`` accepts is a DM.
    """
    channel = normalize_channel_id(ref)
    if channel and _is_known_channel(channel):
        return (channel, None)
    npub = normalize_npub(ref)
    if npub:
        return (npub, None)
    if channel:
        return (channel, None)
    return None


def _channel_ids_from_csv(raw: str) -> set:
    """Canonical 64-hex channel ids from a comma-separated string. No ``*``."""
    found: set = set()
    for part in (raw or "").split(","):
        cid = normalize_channel_id(part)
        if cid:
            found.add(cid)
    return found


def _channel_ids_from_env(name: str) -> set:
    """Canonical 64-hex channel ids from a comma-separated env var. No ``*``."""
    return _channel_ids_from_csv(os.getenv(name) or "")


def _sync_group_allowed_chats_extra(extra: dict) -> None:
    """Publish VECTOR_GROUP_ALLOW_ALL as Hermes ``extra.group_allowed_chats``.

    Gateway ``_is_user_authorized`` reads that key for any group/channel
    (even when ``VECTOR_ALLOWED_USERS`` is set). The old ``group_allow_all``
    extra key is not a Hermes hook.
    """
    if not isinstance(extra, dict):
        return
    ids = set(_group_allow_all_chats())
    ids |= _channel_ids_from_csv(str(extra.get("group_allowed_chats") or ""))
    ids |= _channel_ids_from_csv(str(extra.get("group_allow_all") or ""))
    extra.pop("group_allow_all", None)
    if ids:
        extra["group_allowed_chats"] = ",".join(sorted(ids))
    else:
        extra.pop("group_allowed_chats", None)


_known_channel_ids: set = set()


def _remember_channel(channel_id: str) -> None:
    """Record a Concord channel the bot is in (not a user-facing allowlist)."""
    cid = normalize_channel_id(channel_id)
    if cid:
        _known_channel_ids.add(cid)


def _is_known_channel(channel_id: str) -> bool:
    cid = normalize_channel_id(channel_id) or (channel_id or "").strip().lower()
    if not cid:
        return False
    return cid in _known_channel_ids or cid in _group_allow_all_chats()


def _group_allow_all_chats() -> set:
    """People-gate: VECTOR_GROUP_ALLOW_ALL channel ids (any member, mention-only)."""
    return _channel_ids_from_env("VECTOR_GROUP_ALLOW_ALL")


# Keep in sync with bridge/src/commands.rs HERMES_SLASH_COMMANDS.
_VECTOR_SLASH_COMMANDS = frozenset({"approve", "deny"})


def _group_slash_command(text: str, *, is_command: bool = False) -> bool:
    """True when a group message is a registered Hermes slash command.

    Native Vector picker invocations are forwarded with ``is_command``. Typed
    ``/approve`` / ``/deny`` also bypass the mention gate so an approval prompt
    is answerable in-channel. The people-gate still applies.
    """
    if is_command:
        return True
    token = (text or "").strip().split(None, 1)
    if not token:
        return False
    first = token[0]
    if not first.startswith("/"):
        return False
    name = first[1:].split("@", 1)[0].lower()
    if not name or "/" in name:
        return False
    return name in _VECTOR_SLASH_COMMANDS


def _mentions_bot(text: str, bot_npub: Optional[str], bot_name: Optional[str] = None) -> bool:
    """True if ``text`` @mentions the bot npub or display name. Not ``@everyone``."""
    body = text or ""
    if not body.strip():
        return False
    npub = normalize_npub(bot_npub or "") if bot_npub else None
    if npub:
        if f"@{npub}" in body or f"nostr:{npub}" in body:
            return True
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(npub)}(?![A-Za-z0-9])", body):
            return True
    name = (bot_name or "").strip()
    if name and re.search(rf"@{re.escape(name)}\b", body, re.IGNORECASE):
        return True
    return False


def _reply_to_bot(reply_to: Optional[str], sent_ids) -> bool:
    rid = (reply_to or "").strip()
    return bool(rid) and rid in sent_ids


def _home_operator_npub() -> Optional[str]:
    """VECTOR_HOME_CHANNEL as npub, or None."""
    return normalize_npub((os.getenv("VECTOR_HOME_CHANNEL") or "").strip())


def _format_joined_notice(community_id: str, channels: list) -> str:
    """Operator-facing DM/log body with copy-pasteable channel ids."""
    lines = [
        "Vector: I joined a community.",
        "Copy a channel_id into VECTOR_GROUP_ALLOW_ALL if you want every member to @mention me.",
        "",
    ]
    cid = (community_id or "").strip()
    if cid:
        lines.append(f"community_id: {cid}")
    for row in channels:
        if not isinstance(row, dict):
            continue
        channel_id = str(row.get("channel_id") or "").strip()
        if not channel_id:
            continue
        name = str(row.get("name") or "").strip()
        if name:
            lines.append(f"channel_id: {channel_id}  ({name})")
        else:
            lines.append(f"channel_id: {channel_id}")
    lines.append("")
    lines.append(
        "You can already @mention me there if you are on VECTOR_ALLOWED_USERS."
    )
    return "\n".join(lines)


def _load_notified_channel_ids(data_dir: Path) -> set:
    path = Path(data_dir) / NOTIFIED_CHANNELS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(raw, list):
        return set()
    found: set = set()
    for part in raw:
        cid = normalize_channel_id(str(part or ""))
        if cid:
            found.add(cid)
    return found


def _save_notified_channel_ids(data_dir: Path, ids: set) -> None:
    path = Path(data_dir) / NOTIFIED_CHANNELS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(ids)
        path.write_text(json.dumps(ordered) + "\n", encoding="utf-8")
        if os.name == "posix":
            os.chmod(path, 0o600)
    except OSError as e:
        logger.debug("Vector: could not persist notified channel ids: %s", e)


def _truncate_npub(npub: str) -> str:
    npub = (npub or "").strip()
    if len(npub) > 16:
        return f"{npub[:16]}..."
    return npub


def _env_flag(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip().lower()


def _pairing_enabled() -> bool:
    """VECTOR_PAIRING default on. off/0/false/no drop unauthorized senders."""
    return _env_flag("VECTOR_PAIRING", "on") not in (
        "off",
        "0",
        "false",
        "no",
        "disabled",
    )


def _processing_reactions_enabled() -> bool:
    """VECTOR_REACTIONS default off. 👀/✅/❌ on the triggering DM while the agent works."""
    return _env_flag("VECTOR_REACTIONS") in ("1", "true", "yes", "on")


def _allow_all_users() -> bool:
    return _env_flag("VECTOR_ALLOW_ALL_USERS") in ("1", "true", "yes", "on")


def _create_community_enabled() -> bool:
    return _env_flag("VECTOR_CREATE_COMMUNITY") in ("1", "true", "yes", "on")


def _npubs_from_env(name: str) -> set:
    """Canonical npubs from a comma-separated env var."""
    found: set = set()
    raw = os.getenv(name) or ""
    for part in raw.split(","):
        npub = normalize_npub(part.strip())
        if npub:
            found.add(npub)
    return found


def _allowed_npubs() -> set:
    """Canonical npubs from VECTOR_ALLOWED_USERS (comma-separated)."""
    return _npubs_from_env("VECTOR_ALLOWED_USERS")


def _group_allowed_users() -> set:
    """Group-only senders (VECTOR_GROUP_ALLOWED_USERS). Does not grant DMs."""
    return _npubs_from_env("VECTOR_GROUP_ALLOWED_USERS")


def _sender_is_authorized(peer: str) -> bool:
    """Adapter-layer DM allowlist (VECTOR_ALLOW_ALL_USERS / VECTOR_ALLOWED_USERS)."""
    if _allow_all_users():
        return True
    npub = normalize_npub(peer) or (peer or "").strip()
    if not npub:
        return False
    return npub in _allowed_npubs()


def _group_sender_is_authorized(peer: str, channel_id: str) -> bool:
    """Who may trigger a community turn.

    Union: ``VECTOR_ALLOW_ALL_USERS``, channel in ``VECTOR_GROUP_ALLOW_ALL``,
    DM allowlist (``VECTOR_ALLOWED_USERS``), or ``VECTOR_GROUP_ALLOWED_USERS``.
    Pairing is never offered in a channel.
    """
    if _allow_all_users():
        return True
    cid = normalize_channel_id(channel_id) or (channel_id or "").strip().lower()
    if cid and cid in _group_allow_all_chats():
        return True
    if _sender_is_authorized(peer):
        return True
    npub = normalize_npub(peer) or (peer or "").strip()
    if not npub:
        return False
    return npub in _group_allowed_users()


def _merge_allowed_users(operator_npub: str, existing: str) -> str:
    """Operator npub first, then other already-allowlisted npubs."""
    seen = [operator_npub]
    for part in (existing or "").split(","):
        npub = normalize_npub(part.strip())
        if npub and npub not in seen:
            seen.append(npub)
    return ",".join(seen)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class VectorAdapter(BasePlatformAdapter):
    """Vector ``BasePlatformAdapter``. Owns a local ``vector-bridge`` sidecar."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    supports_code_blocks = True

    # Shared reaction-ack flow (base.on_processing_complete): swap 👀 for
    # ✅/❌. Gated by VECTOR_REACTIONS (default off) via _reactions_enabled.
    _OK_EMOJI = "✅"
    _FAIL_EMOJI = "❌"

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("vector"))

        extra = config.extra if isinstance(getattr(config, "extra", None), dict) else {}
        if getattr(config, "extra", None) is not extra:
            config.extra = extra
        _sync_group_allowed_chats_extra(extra)
        self.bridge_port: int = int(extra.get("bridge_port", DEFAULT_BRIDGE_PORT))
        self.bridge_host: str = str(
            extra.get("bridge_host")
            or os.getenv("VECTOR_BRIDGE_HOST")
            or DEFAULT_BRIDGE_HOST
        )
        self.bot_name: str = (
            extra.get("bot_name") or os.getenv("VECTOR_BOT_NAME") or ""
        ).strip()
        self.bot_about: str = (
            extra.get("bot_about") or os.getenv("VECTOR_BOT_ABOUT") or ""
        ).strip()
        raw_avatar = (
            extra.get("bot_avatar") or os.getenv("VECTOR_BOT_AVATAR") or ""
        ).strip()
        self.bot_avatar: Optional[Path] = Path(raw_avatar).expanduser() if raw_avatar else None
        raw_banner = (
            extra.get("bot_banner") or os.getenv("VECTOR_BOT_BANNER") or ""
        ).strip()
        self.bot_banner: Optional[Path] = Path(raw_banner).expanduser() if raw_banner else None
        self.startup_timeout: int = int(
            os.getenv("VECTOR_STARTUP_TIMEOUT")
            or extra.get("startup_timeout")
            or DEFAULT_STARTUP_TIMEOUT
        )
        self._npub: Optional[str] = extra.get("npub") or (os.getenv("VECTOR_NPUB") or "").strip() or None
        self.data_dir: Path = Path(extra.get("data_dir") or resolve_data_dir())
        self.bridge_url: str = f"http://{_client_host(self.bridge_host)}:{self.bridge_port}"

        self._bridge_process: Optional[subprocess.Popen] = None
        self._bridge_log: Optional[Path] = None
        self._bridge_log_fh = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._sidecar_token: Optional[str] = None
        self._inbound_ids: OrderedDict[str, None] = OrderedDict()
        self._last_inbound_by_chat: OrderedDict[str, str] = OrderedDict()
        self._sent_message_ids: OrderedDict[str, None] = OrderedDict()
        self._seen_reaction_ids: OrderedDict[str, None] = OrderedDict()
        # File-only saves waiting to be attached to the next text from that peer.
        # Sequential Vector DMs (one file per event) append here until that text.
        self._pending_inbox: Dict[str, List[Tuple[str, str]]] = {}
        self._notified_channel_ids: set = _load_notified_channel_ids(self.data_dir)

        logger.info(
            "Vector plugin v%s initialized: port=%d host=%s bot=%s",
            PLUGIN_VERSION,
            self.bridge_port,
            self.bridge_host,
            self.bot_name,
        )

    def _token_headers(self) -> Dict[str, str]:
        return {SIDECAR_TOKEN_HEADER: self._sidecar_token or ""}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        _ensure_hermes_connect_timeout_floor()
        npub = self._npub or "unknown"
        if not self._acquire_platform_lock(
            scope="vector-npub",
            identity=npub,
            resource_desc="Vector bot identity (npub)",
        ):
            return False

        bin_path = resolve_bridge_bin()
        if not bin_path.is_file():
            msg = (
                "vector-bridge binary not found at "
                f"{bin_path}. Run `hermes gateway setup` (builds the sidecar) "
                "or set VECTOR_BRIDGE_BIN. "
                "Do not expect hermes gateway start to compile Rust."
            )
            logger.error("Vector: %s", msg)
            self._set_fatal_error("vector_bridge_missing", msg, retryable=False)
            self._release_platform_lock()
            return False

        if not _identity_nsec_present(self.data_dir):
            nsec_path = Path(self.data_dir) / "identity.nsec"
            msg = (
                f"Vector identity not found at {nsec_path}. "
                "Run `hermes gateway setup` to create or import an nsec. "
                "Do not expect hermes gateway start to mint identity."
            )
            logger.error("Vector: %s", msg)
            self._set_fatal_error("vector_identity_missing", msg, retryable=False)
            self._release_platform_lock()
            return False

        if not _host_is_loopback(self.bridge_host):
            logger.warning(
                "VECTOR_BRIDGE_HOST=%s is not loopback; "
                "X-Hermes-Sidecar-Token is still required on every route",
                self.bridge_host,
            )

        probe_host = _client_host(self.bridge_host)
        if bridge_port_is_listening(self.bridge_port, host=probe_host):
            await self._reap_orphan_sidecar()
            freed = False
            for _ in range(10):
                await asyncio.sleep(0.2)
                if not bridge_port_is_listening(self.bridge_port, host=probe_host):
                    freed = True
                    break
            if not freed:
                msg = (
                    f"Vector: port {self.bridge_port} already in use on {probe_host}. "
                    f"Stop the other process or set VECTOR_BRIDGE_PORT to a free port. "
                    f"(plugin v{PLUGIN_VERSION})"
                )
                logger.error(msg)
                self._set_fatal_error("vector_bridge_port_in_use", msg, retryable=True)
                self._release_platform_lock()
                return False

        self._sidecar_token = secrets.token_hex(32)
        connected = False
        try:
            try:
                self._bridge_process = self._spawn_bridge()
            except (FileNotFoundError, PermissionError, OSError) as e:
                msg = (
                    f"failed to spawn vector-bridge ({e}). "
                    "Check that the binary is executable, or run `hermes gateway setup`."
                )
                logger.error("Vector: %s", msg, exc_info=True)
                self._set_fatal_error("vector_bridge_spawn_failed", msg, retryable=False)
                return False
            except Exception as e:
                logger.error("Vector: failed to spawn sidecar: %s", e, exc_info=True)
                self._set_fatal_error("vector_bridge_spawn_failed", str(e), retryable=False)
                return False

            self._http_client = httpx.AsyncClient(timeout=30.0, trust_env=False)

            logger.info(
                "Vector: waiting up to %ds for sidecar /health status=ready...",
                self.startup_timeout,
            )
            ready = False
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if self._bridge_process.poll() is not None:
                    msg = (
                        f"vector-bridge exited during startup "
                        f"(code {self._bridge_process.returncode}). "
                        f"Check log: {self._bridge_log}"
                    )
                    logger.error("Vector: %s", msg)
                    self._set_fatal_error("vector_bridge_exited", msg, retryable=True)
                    return False

                try:
                    resp = await self._http_client.get(
                        f"{self.bridge_url}/health",
                        headers=self._token_headers(),
                        timeout=2.0,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "ready":
                            ready_npub = data.get("npub")
                            if ready_npub:
                                self._npub = ready_npub
                            ready = True
                            break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(HEALTH_POLL_INTERVAL)

            if not ready:
                msg = (
                    f"Vector sidecar did not become ready in {self.startup_timeout}s. "
                    f"Check log: {self._bridge_log}"
                )
                logger.error("Vector: %s", msg)
                self._set_fatal_error("vector_bridge_startup_timeout", msg, retryable=True)
                return False

            logger.info(
                "Vector: bot npub = %s",
                _truncate_npub(self._npub or ""),
            )

            pid = self._bridge_process.pid if self._bridge_process else 0
            _write_runtime_record(self.bridge_port, self._sidecar_token or "", pid, self._npub)

            # Set _running before SSE/health tasks so their loops don't exit immediately.
            self._running = True
            self._sse_task = asyncio.create_task(self._sse_listener())
            self._health_task = asyncio.create_task(self._health_monitor())
            if _create_community_enabled():
                await self._ensure_home_community()
            await self._sync_joined_channels()
            self._mark_connected()
            connected = True
            logger.info("Vector: connected on %s:%d", self.bridge_host, self.bridge_port)
            return True
        except asyncio.CancelledError:
            raise
        finally:
            if not connected:
                try:
                    await asyncio.shield(self._cleanup_failed_connect())
                except Exception:
                    logger.warning(
                        "Vector: cleanup after failed connect raised",
                        exc_info=True,
                    )

    async def disconnect(self) -> None:
        self._running = False

        for task_attr in ("_sse_task", "_health_task"):
            task = getattr(self, task_attr, None)
            if task:
                task.cancel()
                if task is not asyncio.current_task():
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                setattr(self, task_attr, None)

        await self._stop_bridge_process()
        self._close_bridge_log()

        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

        _delete_runtime_record()
        self._release_platform_lock()
        self._mark_disconnected()
        logger.info("Vector: disconnected")

    async def _cleanup_failed_connect(self) -> None:
        await self._stop_bridge_process()
        self._close_bridge_log()
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        _delete_runtime_record()
        self._release_platform_lock()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._running or not self._http_client:
            return SendResult(success=False, error="Not connected")

        if self._bridge_process and self._bridge_process.poll() is not None:
            msg = (
                f"vector-bridge exited unexpectedly "
                f"(code {self._bridge_process.returncode})."
            )
            if not self.has_fatal_error:
                logger.error("Vector: %s", msg)
                self._set_fatal_error("vector_bridge_exited", msg, retryable=True)
                self._close_bridge_log()
                asyncio.create_task(self._notify_fatal_error())
            return SendResult(success=False, error=self.fatal_error_message or msg)

        payload: Dict[str, Any] = {"to": chat_id, "body": content}
        if reply_to:
            payload["reply_to"] = reply_to

        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/send",
                json=payload,
                headers=self._token_headers(),
                timeout=30.0,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError) as e:
                    logger.warning(
                        "Vector: /send returned 200 but JSON was unreadable: %s",
                        e,
                    )
                    # Sidecar may already have delivered; do not retry.
                    return SendResult(success=True, message_id=None, retryable=False)
                if not isinstance(data, dict):
                    return SendResult(
                        success=True, message_id=None, raw_response=data, retryable=False
                    )
                message_id = data.get("id") or data.get("messageId")
                self._record_sent_message(message_id)
                return SendResult(
                    success=True,
                    message_id=message_id,
                    raw_response=data,
                )
            error_text = resp.text[:200] if resp.text else "No error text"
            logger.warning(
                "Vector: /send failed with status %d: %s",
                resp.status_code,
                error_text,
            )
            return SendResult(
                success=False,
                error=f"Sidecar /send returned {resp.status_code}: {error_text}",
                retryable=resp.status_code >= 500,
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logger.error("Vector: connection error while sending: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)
        except Exception as e:
            logger.error("Vector: exception while sending: %s", e)
            return SendResult(success=False, error=str(e), retryable=False)

    async def send_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
        *,
        emoji_url: Optional[str] = None,
    ) -> bool:
        """React on a Vector DM. Used by the Hermes react tool and optional acks."""
        if not self._http_client or not (message_id or "").strip() or not (emoji or "").strip():
            return False
        body: Dict[str, Any] = {
            "to": chat_id,
            "message_id": message_id,
            "emoji": emoji,
        }
        if emoji_url:
            body["emoji_url"] = emoji_url
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/react",
                json=body,
                headers=self._token_headers(),
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug("Vector: send_reaction failed: %s", e)
            return False

    async def _add_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Lifecycle primitive: tapback ``emoji``. Soft-fails, never raises."""
        return await self.send_reaction(chat_id, message_id, emoji)

    async def _remove_reaction(self, chat_id: str, message_id: str) -> bool:
        """Retract our tapback(s) on ``message_id``. Soft-fails, never raises."""
        if not self._http_client or not (message_id or "").strip():
            return False
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/react",
                json={"to": chat_id, "message_id": message_id, "remove": True},
                headers=self._token_headers(),
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug("Vector: remove_reaction failed: %s", e)
            return False

    def _chat_key(self, chat_id: Optional[str]) -> str:
        raw = (chat_id or "").strip()
        channel = normalize_channel_id(raw)
        if channel and _is_known_channel(channel):
            return channel
        return normalize_npub(raw) or raw

    def _record_last_inbound(
        self, chat_id: Optional[str], message_id: Optional[str]
    ) -> None:
        if not chat_id or not message_id:
            return
        key = self._chat_key(chat_id)
        if not key:
            return
        last = self._last_inbound_by_chat
        if key in last:
            del last[key]
        last[key] = message_id
        while len(last) > LAST_INBOUND_CHATS_MAX:
            last.popitem(last=False)

    def _record_sent_message(self, message_id: Optional[str]) -> None:
        if not message_id:
            return
        sent = self._sent_message_ids
        if message_id in sent:
            del sent[message_id]
        sent[message_id] = None
        while len(sent) > SENT_IDS_MAX:
            sent.popitem(last=False)

    def _remember_reaction_id(self, reaction_id: str) -> None:
        seen = self._seen_reaction_ids
        if reaction_id in seen:
            del seen[reaction_id]
        seen[reaction_id] = None
        while len(seen) > INBOUND_DEDUP_MAX:
            seen.popitem(last=False)

    def _reactions_enabled(self) -> bool:
        """Processing-lifecycle 👀/✅/❌. Agent ``send_message(action=react)`` is not gated."""
        return _processing_reactions_enabled()

    async def add_reaction(
        self,
        chat_id: str,
        emoji: str,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Hermes ``send_message(action="react")`` contract.

        Without ``message_id``, targets the chat's most recent inbound Vector
        event (typically the DM the agent is answering).
        """
        target = (message_id or "").strip() or self._last_inbound_by_chat.get(
            self._chat_key(chat_id)
        )
        if not target:
            return {
                "success": False,
                "error": "no message to react to — pass message_id (no "
                "inbound message seen in this chat since the gateway started)",
            }
        ok = await self._add_reaction(chat_id, target, emoji)
        if not ok:
            return {
                "success": False,
                "error": "reaction failed (see gateway debug log)",
            }
        return {"success": True, "message_id": target}

    async def remove_reaction(
        self, chat_id: str, message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Hermes ``send_message(action="unreact")`` contract."""
        target = (message_id or "").strip() or self._last_inbound_by_chat.get(
            self._chat_key(chat_id)
        )
        if not target:
            return {
                "success": False,
                "error": "no message to unreact — pass message_id",
            }
        ok = await self._remove_reaction(chat_id, target)
        if not ok:
            return {
                "success": False,
                "error": "unreact failed (see gateway debug log)",
            }
        return {"success": True, "message_id": target}

    async def on_processing_start(self, event: MessageEvent) -> None:
        """React 👀 on the triggering DM while the agent works (VECTOR_REACTIONS)."""
        if not self._reactions_enabled():
            return
        if getattr(event.source, "chat_type", "dm") == "group":
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id:
            await self._add_reaction(chat_id, message_id, "\U0001f440")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if not self._http_client:
            return
        try:
            await self._http_client.post(
                f"{self.bridge_url}/typing",
                json={"to": chat_id},
                headers=self._token_headers(),
                timeout=5.0,
            )
        except Exception as e:
            logger.debug("Vector: send_typing failed: %s", e)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        channel = normalize_channel_id(chat_id)
        if channel:
            return {
                "name": _truncate_npub(channel),
                "type": "group",
                "chat_id": channel,
            }
        npub = normalize_npub(chat_id) or (chat_id or "").strip()
        name = _truncate_npub(npub)
        return {"name": name, "type": "dm", "chat_id": npub}

    async def _ensure_home_community(self) -> None:
        """Slice 2: create-or-reuse a bot-owned Concord community (no public link)."""
        if not self._http_client:
            return
        name = (os.getenv("VECTOR_COMMUNITY_NAME") or "").strip() or "Hermes"
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/communities",
                json={"name": name},
                headers=self._token_headers(),
                timeout=30.0,
            )
        except Exception as e:
            logger.warning("Vector: create home community failed: %s", e)
            return
        if resp.status_code != 200:
            logger.warning(
                "Vector: /communities returned %s: %s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        channel_id = str(data.get("channel_id") or "")
        community_id = str(data.get("community_id") or "")
        created = bool(data.get("created"))
        if channel_id:
            _remember_channel(channel_id)
        if created:
            logger.info(
                "Vector: created home community %s channel %s",
                _truncate_npub(community_id),
                _truncate_npub(channel_id),
            )
        else:
            logger.info(
                "Vector: reusing home community %s channel %s",
                _truncate_npub(community_id),
                _truncate_npub(channel_id),
            )
        if channel_id:
            await self._notify_joined_channels(
                community_id,
                [{"channel_id": channel_id, "name": name}],
            )

    async def _notify_joined_channels(self, community_id: str, channels: list) -> None:
        """Log full channel ids and DM VECTOR_HOME_CHANNEL once per channel."""
        rows = []
        for row in channels or []:
            if not isinstance(row, dict):
                continue
            cid = normalize_channel_id(str(row.get("channel_id") or ""))
            if not cid:
                continue
            _remember_channel(cid)
            rows.append(
                {
                    "channel_id": cid,
                    "name": str(row.get("name") or "").strip(),
                }
            )
        if not rows:
            return
        new_rows = [
            row for row in rows if row["channel_id"] not in self._notified_channel_ids
        ]
        if not new_rows:
            return
        community = (community_id or "").strip() or "(unknown)"
        for row in new_rows:
            logger.info(
                "Vector: joined channel_id=%s name=%s community_id=%s",
                row["channel_id"],
                row["name"] or "(unnamed)",
                community,
            )
        home = _home_operator_npub()
        if home:
            if not (self._running and self._http_client):
                logger.debug(
                    "Vector: not connected; will DM channel_id to VECTOR_HOME_CHANNEL later"
                )
                return
            result = await self.send(home, _format_joined_notice(community_id, new_rows))
            if not result.success:
                logger.warning(
                    "Vector: could not DM VECTOR_HOME_CHANNEL the channel id: %s",
                    result.error,
                )
                return
        self._notified_channel_ids.update(row["channel_id"] for row in new_rows)
        _save_notified_channel_ids(self.data_dir, self._notified_channel_ids)

    async def _sync_joined_channels(self) -> None:
        """Remember channel ids for communities the bot already belongs to."""
        if not self._http_client:
            return
        try:
            resp = await self._http_client.get(
                f"{self.bridge_url}/communities",
                headers=self._token_headers(),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("Vector: list communities failed: %s", e)
            return
        if resp.status_code != 200:
            return
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        rows = data.get("communities")
        if not isinstance(rows, list):
            return
        for community in rows:
            if not isinstance(community, dict):
                continue
            community_id = str(community.get("community_id") or "")
            channels = community.get("channels")
            if not isinstance(channels, list):
                continue
            channel_rows = []
            for ch in channels:
                if not isinstance(ch, dict):
                    continue
                cid = str(ch.get("channel_id") or "")
                _remember_channel(cid)
                if cid:
                    channel_rows.append(
                        {
                            "channel_id": cid,
                            "name": str(ch.get("name") or ""),
                        }
                    )
            if channel_rows:
                await self._notify_joined_channels(community_id, channel_rows)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        path = image_url
        if str(image_url).startswith(("http://", "https://")):
            try:
                path = await cache_image_from_url(image_url)
            except Exception as e:
                logger.warning("Vector: failed to cache outbound image URL: %s", e)
                return SendResult(success=False, error=str(e), retryable=True)
        return await self._send_local_file(
            chat_id, path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_local_file(
            chat_id, image_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_local_file(
            chat_id, file_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_local_file(
            chat_id, video_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_local_file(
            chat_id, audio_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self.send_image(
            chat_id, animation_url, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def _send_local_file(
        self,
        chat_id: str,
        file_path: str,
        *,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._running or not self._http_client:
            return SendResult(success=False, error="Not connected")
        session_key = ""
        try:
            session_key = str((metadata or {}).get("session_id") or "")
        except Exception:
            session_key = ""
        safe = self.validate_media_delivery_path(str(file_path), session_key=session_key)
        if not safe:
            logger.warning("Vector: refusing unsafe outbound path")
            return SendResult(success=False, error="unsafe media path", retryable=False)
        npub = normalize_npub(chat_id) or (chat_id or "").strip()
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/send-file",
                json={"to": npub, "path": safe},
                headers=self._token_headers(),
                timeout=SEND_FILE_TIMEOUT,
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:
            return SendResult(success=False, error=str(e), retryable=True)
        if resp.status_code != 200:
            error_text = (resp.text or "")[:200]
            return SendResult(
                success=False,
                error=f"Sidecar /send-file returned {resp.status_code}: {error_text}",
                retryable=resp.status_code >= 500,
            )
        try:
            data = resp.json() if resp.content else {}
        except (ValueError, json.JSONDecodeError):
            data = {}
        file_id = None
        if isinstance(data, dict):
            file_id = data.get("id") or data.get("messageId")
        if caption and str(caption).strip():
            cap = await self.send(
                chat_id=npub,
                content=str(caption).strip(),
                reply_to=reply_to,
                metadata=metadata,
            )
            if not cap.success:
                logger.warning("Vector: file sent but caption failed: %s", cap.error)
        self._record_sent_message(file_id)
        return SendResult(success=True, message_id=file_id, raw_response=data)

    # ------------------------------------------------------------------
    # SSE listener (inbound messages)
    # ------------------------------------------------------------------

    async def _sse_listener(self) -> None:
        url = f"{self.bridge_url}/events"
        backoff = SSE_RETRY_DELAY_INITIAL

        while self._running:
            if self._bridge_process and self._bridge_process.poll() is not None:
                await self._handle_bridge_exit()
                break

            try:
                logger.debug("Vector SSE: connecting to %s", url)
                async with self._http_client.stream(
                    "GET",
                    url,
                    headers={
                        **self._token_headers(),
                        "Accept": "text/event-stream",
                    },
                    timeout=None,
                ) as response:
                    if response.status_code != 200:
                        raise httpx.HTTPStatusError(
                            f"/events returned {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    backoff = SSE_RETRY_DELAY_INITIAL
                    logger.info("Vector SSE: connected")

                    buffer = ""
                    aiter = response.aiter_text().__aiter__()
                    while self._running:
                        try:
                            chunk = await asyncio.wait_for(
                                aiter.__anext__(), timeout=SSE_STALE_TIMEOUT
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Vector SSE: no data in %.0fs, reconnecting",
                                SSE_STALE_TIMEOUT,
                            )
                            break
                        except StopAsyncIteration:
                            break

                        if (
                            self._bridge_process
                            and self._bridge_process.poll() is not None
                        ):
                            await self._handle_bridge_exit()
                            return

                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.rstrip("\r")
                            if not line or line.startswith(":"):
                                continue
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if not data_str:
                                    continue
                                try:
                                    data = json.loads(data_str)
                                    await self._dispatch_sse_event(data)
                                except json.JSONDecodeError:
                                    logger.debug(
                                        "Vector SSE: invalid JSON: %s",
                                        data_str[:120],
                                    )
                                except Exception:
                                    logger.exception(
                                        "Vector SSE: error handling event"
                                    )

            except asyncio.CancelledError:
                break
            except httpx.HTTPError as e:
                if self._running:
                    logger.warning(
                        "Vector SSE: HTTP error: %s (reconnecting in %.0fs)",
                        e,
                        backoff,
                    )
            except Exception as e:
                if self._running:
                    logger.warning(
                        "Vector SSE: error: %s (reconnecting in %.0fs)",
                        e,
                        backoff,
                    )

            if self._running:
                if (
                    self._bridge_process
                    and self._bridge_process.poll() is not None
                ):
                    await self._handle_bridge_exit()
                    break
                jitter = backoff * 0.2 * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, SSE_RETRY_DELAY_MAX)

    async def _dispatch_sse_event(self, data: dict) -> None:
        event_type = data.get("type", "")
        if event_type == "ready":
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            npub = (inner or {}).get("npub")
            if npub:
                self._npub = npub
            logger.info("Vector SSE: sidecar emitted 'ready'")
        elif event_type == "message":
            await self._handle_message_event(data)
        elif event_type == "message_update":
            await self._handle_message_update(data)
        elif event_type == "community_joined":
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            inner = inner or {}
            channels = inner.get("channels") if isinstance(inner.get("channels"), list) else []
            community_id = str(inner.get("community_id") or "")
            if channels:
                await self._notify_joined_channels(community_id, channels)
            elif community_id:
                await self._sync_joined_channels()
        else:
            logger.debug("Vector SSE: unhandled event type '%s'", event_type)

    async def _handle_message_event(self, msg_data: dict) -> None:
        if isinstance(msg_data, dict) and msg_data.get("type") == "message" and "data" in msg_data:
            msg_data = msg_data["data"]
        if not isinstance(msg_data, dict):
            return

        if msg_data.get("is_mine"):
            return
        if msg_data.get("is_group"):
            await self._handle_group_message(msg_data)
            return

        msg_id = str(msg_data.get("id") or "")
        if msg_id and self._is_duplicate(msg_id):
            logger.debug("Vector: dropping duplicate inbound id=%s", msg_id[:16])
            return

        text = str(msg_data.get("text") or "")
        attachments = msg_data.get("attachments") if isinstance(msg_data.get("attachments"), list) else []
        is_file = bool(msg_data.get("is_file") or attachments)
        if not text.strip() and not is_file:
            return

        raw_peer = msg_data.get("npub") or msg_data.get("chat_id") or ""
        peer = normalize_npub(raw_peer) or str(raw_peer).strip()
        if not peer:
            return

        bot_npub = normalize_npub(self._npub or "") if self._npub else None
        if bot_npub and peer == bot_npub:
            return

        # VECTOR_PAIRING=off: drop before handle_message so pairing codes are not sent.
        if not _pairing_enabled() and not _sender_is_authorized(peer):
            logger.info(
                "Vector: dropping unauthorized sender %s (VECTOR_PAIRING=off)",
                _truncate_npub(peer),
            )
            return

        if msg_id:
            self._record_last_inbound(peer, msg_id)

        name = _truncate_npub(peer)
        source = self.build_source(
            chat_id=peer,
            chat_name=name,
            chat_type="dm",
            user_id=peer,
            user_name=name,
            message_id=msg_id or None,
        )
        reply_to_text = msg_data.get("reply_to_text") or None

        saved: List[Tuple[Path, dict, str]] = []
        if is_file and attachments and _sender_is_authorized(peer):
            saved = await self._save_inbound_attachments(
                peer, attachments, msg_id=msg_id, caption=text, at_ms=msg_data.get("at_ms")
            )

        if is_file and not text.strip():
            if _sender_is_authorized(peer):
                await self._ack_file_only(peer, saved)
                await self._write_inbox_breadcrumb(source, saved)
                pending = self._pending_inbox.setdefault(peer, [])
                seen = {p for p, _m in pending}
                for path, _att, mime in saved:
                    key = str(path)
                    if key not in seen:
                        pending.append((key, mime))
                        seen.add(key)
            elif _pairing_enabled():
                event = MessageEvent(
                    text="(file attachment)",
                    message_type=MessageType.DOCUMENT,
                    source=source,
                    message_id=msg_id or None,
                    reply_to_text=reply_to_text,
                )
                await self.handle_message(event)
            return

        media_urls: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT
        if saved:
            media_urls = [str(path) for path, _att, _mime in saved]
            media_types = [mime for _path, _att, mime in saved]
        elif not is_file:
            pending = self._pending_inbox.pop(peer, [])
            media_urls = [p for p, _m in pending]
            media_types = [m for _p, m in pending]
        if media_types:
            msg_type = _message_type_for_mime(media_types[0])
        if saved:
            self._pending_inbox.pop(peer, None)

        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=msg_id or None,
            reply_to_text=reply_to_text,
            media_urls=media_urls,
            media_types=media_types,
        )
        await self.handle_message(event)

    async def _handle_group_message(self, msg_data: dict) -> None:
        """Mention-gated Concord channel message → Hermes ``chat_type=group``."""
        raw_chat = str(msg_data.get("chat_id") or "")
        channel_id = normalize_channel_id(raw_chat)
        if not channel_id:
            return
        _remember_channel(channel_id)
        await self._notify_joined_channels(
            str(msg_data.get("community_id") or ""),
            [{"channel_id": channel_id, "name": ""}],
        )

        msg_id = str(msg_data.get("id") or "")
        if msg_id and self._is_duplicate(msg_id):
            logger.debug("Vector: dropping duplicate inbound id=%s", msg_id[:16])
            return

        text = str(msg_data.get("text") or "")
        if not text.strip():
            logger.debug("Vector: skip empty/file-only group message id=%s", msg_id[:16])
            return

        raw_peer = msg_data.get("npub") or ""
        peer = normalize_npub(raw_peer) or str(raw_peer).strip()
        if not peer:
            return

        bot_npub = normalize_npub(self._npub or "") if self._npub else None
        if bot_npub and peer == bot_npub:
            return

        reply_to = str(msg_data.get("reply_to") or "")
        is_command = bool(msg_data.get("is_command"))
        if (
            not _mentions_bot(text, bot_npub, self.bot_name)
            and not _reply_to_bot(reply_to, self._sent_message_ids)
            and not _group_slash_command(text, is_command=is_command)
        ):
            logger.debug("Vector: drop group message (no mention) id=%s", msg_id[:16])
            return

        if not _group_sender_is_authorized(peer, channel_id):
            logger.debug(
                "Vector: drop group message from unauthorized sender %s",
                _truncate_npub(peer),
            )
            return

        if msg_id:
            self._record_last_inbound(channel_id, msg_id)

        name = _truncate_npub(peer)
        community_id = str(msg_data.get("community_id") or "") or None
        chat_name = _truncate_npub(community_id or channel_id)
        source = self.build_source(
            chat_id=channel_id,
            chat_name=chat_name,
            chat_type="group",
            user_id=peer,
            user_name=name,
            message_id=msg_id or None,
            parent_chat_id=community_id,
            scope_id=community_id,
            # Adapter already applied the group people-gate. Hermes has no
            # group_allowed_users_env hook; this is the Discord-style
            # "adapter admitted this sender" bit so group-only npubs and
            # VECTOR_GROUP_ALLOW_ALL members are not dropped at the gateway.
            role_authorized=True,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id or None,
            reply_to_text=msg_data.get("reply_to_text") or None,
        )
        await self.handle_message(event)

    async def _handle_message_update(self, msg_data: dict) -> None:
        """Peer reaction on a message we sent → ``reaction:added:<emoji>``."""
        if (
            isinstance(msg_data, dict)
            and msg_data.get("type") == "message_update"
            and "data" in msg_data
        ):
            msg_data = msg_data["data"]
        if not isinstance(msg_data, dict):
            return

        reactions = msg_data.get("reactions")
        if not isinstance(reactions, list) or not reactions:
            return

        target_id = str(msg_data.get("id") or "")
        raw_peer = msg_data.get("npub") or msg_data.get("chat_id") or ""
        peer = normalize_npub(raw_peer) or str(raw_peer).strip()
        if not peer or not target_id:
            return

        bot_npub = normalize_npub(self._npub or "") if self._npub else None
        ours = bool(msg_data.get("mine")) or target_id in self._sent_message_ids
        last = reactions[-1] if isinstance(reactions[-1], dict) else None
        if not isinstance(last, dict):
            return
        react_id = str(last.get("id") or "")
        author = normalize_npub(str(last.get("author_id") or "")) or str(
            last.get("author_id") or ""
        ).strip()
        emoji = str(last.get("emoji") or "")
        already_seen = bool(react_id) and react_id in self._seen_reaction_ids
        for row in reactions:
            if isinstance(row, dict) and row.get("id"):
                self._remember_reaction_id(str(row["id"]))
        if already_seen or not ours or not emoji:
            return
        if bot_npub and author == bot_npub:
            return
        if not _pairing_enabled() and not _sender_is_authorized(peer):
            return
        name = _truncate_npub(peer)
        source = self.build_source(
            chat_id=peer,
            chat_name=name,
            chat_type="dm",
            user_id=peer,
            user_name=name,
            message_id=react_id or None,
        )
        event = MessageEvent(
            text=f"reaction:added:{emoji}",
            message_type=MessageType.TEXT,
            source=source,
            message_id=react_id or None,
            reply_to_message_id=target_id,
            reply_to_text=str(msg_data.get("text") or "") or None,
            reply_to_is_own_message=True,
            raw_message=msg_data,
        )
        await self.handle_message(event)

    def _is_duplicate(self, msg_id: str) -> bool:
        """Return True if this inbound id was already seen (LRU ~1024)."""
        if not msg_id:
            return False
        seen = self._inbound_ids
        if msg_id in seen:
            seen.move_to_end(msg_id)
            return True
        seen[msg_id] = None
        while len(seen) > INBOUND_DEDUP_MAX:
            seen.popitem(last=False)
        return False

    async def _save_inbound_attachments(
        self,
        peer: str,
        attachments: List[dict],
        *,
        msg_id: str,
        caption: str,
        at_ms: Any,
    ) -> List[Tuple[Path, dict, str]]:
        """Download attachments into files/inbox/{npub}/{YYYY-MM-DD}/."""
        saved: List[Tuple[Path, dict, str]] = []
        max_bytes = _inbound_media_max_bytes()
        try:
            when = datetime.fromtimestamp(int(at_ms) / 1000.0) if at_ms else datetime.now()
        except (TypeError, ValueError, OSError):
            when = datetime.now()
        day = when.strftime("%Y-%m-%d")
        stamp = when.strftime("%H%M%S")
        inbox = resolve_files_root() / "inbox" / peer / day
        author = peer
        for i, att in enumerate(attachments):
            if not isinstance(att, dict):
                continue
            try:
                size = int(att.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if max_bytes and size > max_bytes:
                logger.warning(
                    "Vector: skip inbound attachment over cap (%s bytes)", size
                )
                continue
            orig = str(att.get("name") or "").strip() or f"file-{att.get('id') or i}"
            ext = str(att.get("extension") or "").lstrip(".")
            if ext and not orig.lower().endswith(f".{ext.lower()}"):
                orig = f"{orig}.{ext}"
            filename = _sanitize_filename(f"{stamp}-{orig}")
            dest = _unique_path(inbox, filename)
            path = await self._download_attachment(att, dest, author_npub=author)
            if path is None:
                continue
            mime = _mime_for_attachment(att)
            sha = ""
            try:
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                pass
            meta = {
                "original_name": orig,
                "saved_as": path.name,
                "size": path.stat().st_size if path.exists() else size,
                "mime": mime,
                "sha256": sha,
                "vector_event_id": msg_id,
                "attachment_id": att.get("id"),
                "peer": peer,
                "caption": caption or "",
                "saved_at": time.time(),
            }
            try:
                path.with_name(path.name + ".meta.json").write_text(
                    json.dumps(meta, indent=2) + "\n", encoding="utf-8"
                )
            except OSError:
                logger.warning("Vector: failed to write inbox meta for %s", path.name)
            self._append_inbox_index(meta | {"path": str(path)})
            saved.append((path, att, mime))
        return saved

    async def _download_attachment(
        self, att: dict, dest: Path, *, author_npub: str
    ) -> Optional[Path]:
        if not self._http_client:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/download-attachment",
                json={
                    "attachment": att,
                    "dest": str(dest),
                    "author_npub": author_npub,
                },
                headers=self._token_headers(),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logger.warning("Vector: download-attachment failed: %s", e)
            return None
        if resp.status_code != 200:
            logger.warning(
                "Vector: /download-attachment returned %s: %s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return None
        if dest.is_file():
            return dest
        try:
            data = resp.json()
            p = Path(data.get("path") or "")
            if p.is_file():
                return p
        except (ValueError, json.JSONDecodeError, TypeError):
            pass
        return None

    def _append_inbox_index(self, row: dict) -> None:
        index = resolve_files_root() / "index.jsonl"
        try:
            index.parent.mkdir(parents=True, exist_ok=True)
            with index.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            logger.warning("Vector: failed to append files/index.jsonl")

    async def _ack_file_only(self, peer: str, saved: List[Tuple[Path, dict, str]]) -> None:
        if not saved:
            await self.send(chat_id=peer, content="couldn't save attachment")
            return
        names = [path.name.split("-", 1)[-1] if "-" in path.name else path.name for path, *_ in saved]
        if len(names) == 1:
            body = f"saved {names[0]}"
        else:
            body = f"saved {len(names)} files: " + ", ".join(names)
        await self.send(chat_id=peer, content=body)

    async def _write_inbox_breadcrumb(
        self, source, saved: List[Tuple[Path, dict, str]]
    ) -> None:
        if not saved:
            return
        lines = [
            "[Vector inbox] Saved file(s) with no caption (not processed). "
            "Paths for later reference:"
        ]
        for path, att, mime in saved:
            orig = att.get("name") or path.name
            lines.append(f"- {orig} ({mime}) `{path}`")
        content = "\n".join(lines)
        try:
            await asyncio.to_thread(self._append_session_breadcrumb, source, content)
        except Exception:
            logger.warning("Vector: failed to write inbox session breadcrumb", exc_info=True)

    def _append_session_breadcrumb(self, source, content: str) -> None:
        """Write into the gateway SessionStore's real session_id, not the routing key."""
        store = getattr(self, "_session_store", None)
        if store is None or not hasattr(store, "get_or_create_session"):
            logger.warning("Vector: session store unavailable for inbox breadcrumb")
            return
        entry = store.get_or_create_session(source, touch_activity=False)
        session_id = getattr(entry, "session_id", None)
        if not session_id:
            logger.warning("Vector: session store returned no session_id for breadcrumb")
            return
        runner = getattr(self, "gateway_runner", None)
        db = getattr(runner, "_session_db", None) if runner is not None else None
        inner = getattr(db, "_db", db) if db is not None else None
        if inner is None or not hasattr(inner, "append_message"):
            logger.warning("Vector: session db unavailable for inbox breadcrumb")
            return
        ensure = getattr(inner, "ensure_session", None)
        if callable(ensure):
            ensure(session_id, source="vector")
        inner.append_message(
            session_id,
            "user",
            content,
            display_kind="internal_notification",
            platform_message_id=getattr(source, "message_id", None),
        )
        logger.info("Vector: inbox breadcrumb written to session %s", session_id)

    # ------------------------------------------------------------------
    # Health monitor / process death
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        while self._running:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break

            if self._bridge_process and self._bridge_process.poll() is not None:
                await self._handle_bridge_exit()
                break

            if not self._http_client:
                continue
            try:
                resp = await self._http_client.get(
                    f"{self.bridge_url}/health",
                    headers=self._token_headers(),
                    timeout=5.0,
                )
                if resp.status_code != 200:
                    logger.warning("Vector: /health returned %d", resp.status_code)
            except Exception as e:
                logger.warning("Vector: /health unreachable: %s", e)

    async def _handle_bridge_exit(self) -> None:
        returncode = (
            self._bridge_process.returncode if self._bridge_process else "?"
        )
        msg = (
            f"vector-bridge exited unexpectedly (code {returncode}). "
            f"Check log: {self._bridge_log}"
        )
        if not self.has_fatal_error:
            logger.error("Vector: %s", msg)
            self._set_fatal_error("vector_bridge_exited", msg, retryable=True)
            self._close_bridge_log()
            await self._notify_fatal_error()

    # ------------------------------------------------------------------
    # Sidecar process
    # ------------------------------------------------------------------

    def _spawn_bridge(self) -> subprocess.Popen:
        """Launch vector-bridge with stdin pipe; logs go to a file (not PIPE)."""
        bin_path = resolve_bridge_bin()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        try:
            home = get_hermes_home()
        except Exception:
            home = Path.home() / ".hermes"
        logs_dir = Path(home) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._bridge_log = logs_dir / "vector-bridge.log"
        bridge_log_fh = open(self._bridge_log, "a", encoding="utf-8")
        self._bridge_log_fh = bridge_log_fh

        env = {
            **os.environ,
            "VECTOR_DATA_DIR": str(self.data_dir),
            "VECTOR_BRIDGE_PORT": str(self.bridge_port),
            "VECTOR_BRIDGE_HOST": self.bridge_host,
            "VECTOR_SIDECAR_TOKEN": self._sidecar_token or "",
            "VECTOR_SIDECAR_WATCH_STDIN": "1",
        }
        env.pop("VECTOR_BOT_NAME", None)
        env.pop("VECTOR_BOT_ABOUT", None)
        env.pop("VECTOR_BOT_AVATAR", None)
        env.pop("VECTOR_BOT_BANNER", None)
        if self.bot_name:
            env["VECTOR_BOT_NAME"] = self.bot_name
        if self.bot_about:
            env["VECTOR_BOT_ABOUT"] = self.bot_about
        if self.bot_avatar and self.bot_avatar.is_file():
            env["VECTOR_BOT_AVATAR"] = str(self.bot_avatar.resolve())
        elif self.bot_avatar:
            logger.warning(
                "Vector: VECTOR_BOT_AVATAR is not a file (%s); not publishing an avatar",
                self.bot_avatar,
            )
        if self.bot_banner and self.bot_banner.is_file():
            env["VECTOR_BOT_BANNER"] = str(self.bot_banner.resolve())
        elif self.bot_banner:
            logger.warning(
                "Vector: VECTOR_BOT_BANNER is not a file (%s); not publishing a banner",
                self.bot_banner,
            )
        env.pop("VECTOR_NSEC", None)
        env.pop("VECTOR_MNEMONIC", None)
        env.pop("VECTOR_STUB", None)

        logger.info(
            "Vector plugin v%s: spawning %s (port %d, log %s)",
            PLUGIN_VERSION,
            bin_path,
            self.bridge_port,
            self._bridge_log,
        )

        popen_kwargs: Dict[str, Any] = {
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": bridge_log_fh,
            "stderr": bridge_log_fh,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # Photon: start_new_session, not preexec_fn=os.setsid (unsafe in threads).
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen([str(bin_path)], **popen_kwargs)
        self._bridge_process = process
        return process

    async def _wait_proc(self, proc: subprocess.Popen, timeout: float) -> None:
        """Wait for ``proc`` up to ``timeout`` seconds; TimeoutExpired if still alive."""
        try:
            await asyncio.to_thread(proc.wait, timeout)
        except subprocess.TimeoutExpired:
            raise
        except Exception:
            deadline = time.monotonic() + max(float(timeout), 0.0)
            while proc.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if proc.poll() is None:
                raise subprocess.TimeoutExpired("vector-bridge", timeout)

    def _signal_bridge(self, proc: subprocess.Popen, sig) -> None:
        pid = getattr(proc, "pid", -1) or -1
        if sys.platform == "win32" or pid <= 1:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
            return
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()

    async def _stop_bridge_process(self) -> None:
        proc = self._bridge_process
        if not proc:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if proc.poll() is not None:
                return
            try:
                self._signal_bridge(proc, signal.SIGTERM)
            except Exception:
                pass
            try:
                await self._wait_proc(proc, BRIDGE_TERM_WAIT)
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() is None:
                try:
                    self._signal_bridge(proc, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    await self._wait_proc(proc, 1.0)
                except (subprocess.TimeoutExpired, Exception):
                    pass
        except Exception as e:
            logger.warning("Vector: error stopping sidecar: %s", e)
        finally:
            self._bridge_process = None

    async def _reap_orphan_sidecar(self) -> None:
        """Kill a previous vector-bridge still listening on our port.

        Only signals PIDs whose command line contains ``vector-bridge``
        (Photon: never kill a reused pid from a stale runtime record).
        """
        if sys.platform == "win32":
            return

        def _inspect():
            found = _find_listener_pids(self.bridge_port)
            mine = [pid for pid in found if _pid_is_vector_bridge(pid)]
            return mine, [pid for pid in found if pid not in mine]

        stale, _foreign = await asyncio.to_thread(_inspect)
        if not stale:
            return
        for pid in stale:
            logger.warning(
                "Vector: reaping orphan sidecar pid %d on port %d",
                pid,
                self.bridge_port,
            )
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 2.0
        while time.time() < deadline and any(_pid_alive(p) for p in stale):
            await asyncio.sleep(0.1)
        for pid in stale:
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        # Give the OS a beat to release the listening socket (Photon).
        await asyncio.sleep(0.2)
        _delete_runtime_record()

    def _close_bridge_log(self) -> None:
        if self._bridge_log_fh:
            try:
                self._bridge_log_fh.close()
            except Exception:
                pass
            self._bridge_log_fh = None


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Side-effect free: VECTOR_NPUB set and vector-bridge binary present."""
    npub = (os.getenv("VECTOR_NPUB") or "").strip()
    if not npub:
        return False
    return resolve_bridge_bin().is_file()


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    npub = os.getenv("VECTOR_NPUB") or extra.get("npub") or ""
    return bool(str(npub).strip())


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement():
    """Seed PlatformConfig.extra from env vars before adapter construction."""
    npub = (os.getenv("VECTOR_NPUB") or "").strip()
    if not npub:
        return None
    seed = {
        "npub": npub,
        "data_dir": str(resolve_data_dir()),
        "bridge_port": os.getenv("VECTOR_BRIDGE_PORT") or str(DEFAULT_BRIDGE_PORT),
        "bridge_host": os.getenv("VECTOR_BRIDGE_HOST") or DEFAULT_BRIDGE_HOST,
        "startup_timeout": os.getenv("VECTOR_STARTUP_TIMEOUT") or str(DEFAULT_STARTUP_TIMEOUT),
    }
    home = (os.getenv("VECTOR_HOME_CHANNEL") or "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": normalize_npub(home) or home,
            "name": os.getenv("VECTOR_HOME_CHANNEL_NAME") or "Home",
        }
    group_users = (os.getenv("VECTOR_GROUP_ALLOWED_USERS") or "").strip()
    if group_users:
        seed["group_allowed_users"] = group_users
    open_chats = ",".join(sorted(_group_allow_all_chats()))
    if open_chats:
        seed["group_allowed_chats"] = open_chats
    bot_name = (os.getenv("VECTOR_BOT_NAME") or "").strip()
    if bot_name:
        seed["bot_name"] = bot_name
    bot_about = (os.getenv("VECTOR_BOT_ABOUT") or "").strip()
    if bot_about:
        seed["bot_about"] = bot_about
    avatar = (os.getenv("VECTOR_BOT_AVATAR") or "").strip()
    if avatar:
        seed["bot_avatar"] = avatar
    banner = (os.getenv("VECTOR_BOT_BANNER") or "").strip()
    if banner:
        seed["bot_banner"] = banner
    return seed


def _coerce_port(value: Any, default: int = DEFAULT_BRIDGE_PORT) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _sidecar_pid_alive(pid: Any) -> bool:
    """Best-effort liveness for the runtime-record sidecar pid."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 1:
        return False
    if os.name != "posix":
        return True
    return _pid_alive(pid_int)


def _parse_rustc_version(text: str) -> Optional[tuple]:
    """Parse ``rustc 1.75.0 (...)`` → ``(1, 75)``."""
    match = re.search(r"rustc\s+(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _probe_rustc() -> Optional[tuple]:
    try:
        result = subprocess.run(
            ["rustc", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_rustc_version(result.stdout or result.stderr or "")


def _parse_bridge_json(text: str) -> Optional[Dict[str, Any]]:
    """First JSON object that carries ``status``, ``code``, or ``error``."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and (
            "status" in data or "code" in data or "error" in data
        ):
            return data
    return None


def _bridge_cli_env(data_dir: Path) -> Dict[str, str]:
    """Env for --check/--setup: data dir set, secrets never inherited."""
    env = {**os.environ, "VECTOR_DATA_DIR": str(data_dir)}
    env.pop("VECTOR_NSEC", None)
    env.pop("VECTOR_MNEMONIC", None)
    env.pop("VECTOR_STUB", None)
    env.pop("VECTOR_SIDECAR_TOKEN", None)
    return env


def _run_bridge_cli(
    bin_path: Path,
    data_dir: Path,
    args: List[str],
    *,
    timeout: float = 60.0,
) -> tuple:
    """Run vector-bridge identity CLI. Returns ``(parsed_json, returncode, stderr)``."""
    try:
        result = subprocess.run(
            [str(bin_path), *args],
            env=_bridge_cli_env(data_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, 124, f"timed out after {timeout:.0f}s"
    except OSError as e:
        return None, 127, str(e)
    data = _parse_bridge_json(result.stdout or "")
    err = (result.stderr or "").strip()
    if data is None and err:
        data = _parse_bridge_json(err)
    return data, result.returncode, err


def _write_temp_secret(contents: str, directory: Optional[Path] = None) -> Path:
    """Write a one-shot 0600 file for --nsec-file / --mnemonic-file.

    Prefer ``VECTOR_DATA_DIR`` so a SIGKILL leftover sits next to identity
    material, not in ``/tmp``.
    """
    parent = Path(directory) if directory else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=str(parent), prefix=".vector-import.", suffix=".tmp"
    )
    try:
        try:
            os.chmod(name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write((contents or "").strip() + "\n")
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)


def _shred_unlink(path: Path) -> None:
    """Overwrite then unlink a one-shot secret file."""
    try:
        if path.is_file():
            size = max(path.stat().st_size, 1)
            with open(path, "r+b") as fh:
                fh.write(b"\0" * size)
                fh.flush()
                os.fsync(fh.fileno())
        path.unlink(missing_ok=True)
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _backup_identity_nsec(data_dir: Path) -> Optional[Path]:
    """Rename ``identity.nsec`` → ``identity.nsec.bak``. None if missing."""
    src = Path(data_dir) / "identity.nsec"
    if not src.is_file():
        return None
    bak = Path(data_dir) / "identity.nsec.bak"
    if bak.exists():
        bak.unlink()
    src.replace(bak)
    return bak


def _restore_identity_nsec(data_dir: Path, bak: Optional[Path]) -> None:
    """Put the backup back if ``--setup`` failed after the rename."""
    if bak is None or not bak.is_file():
        return
    src = Path(data_dir) / "identity.nsec"
    try:
        if src.exists():
            src.unlink()
    except OSError:
        pass
    try:
        bak.replace(src)
    except OSError as e:
        logger.warning("Vector: failed to restore identity.nsec from backup: %s", e)


def _discard_identity_backup(bak: Optional[Path]) -> None:
    if bak is None:
        return
    try:
        bak.unlink(missing_ok=True)
    except OSError:
        pass


def _identity_nsec_locally_unreadable(data_dir: Path) -> bool:
    """True when identity.nsec exists but cannot be read (or is empty)."""
    path = Path(data_dir) / "identity.nsec"
    try:
        if not path.is_file():
            return False
        if path.stat().st_size == 0:
            return True
        path.read_bytes()
        return False
    except OSError:
        return True


def _adopt_stale_identity_backup(data_dir: Path, io) -> None:
    """If a previous setup left only ``identity.nsec.bak``, put it back."""
    src = Path(data_dir) / "identity.nsec"
    bak = Path(data_dir) / "identity.nsec.bak"
    try:
        src_ok = src.is_file() and src.stat().st_size > 0
    except OSError:
        src_ok = False
    if src_ok or not bak.is_file():
        return
    io.print_warning(
        "Found identity.nsec.bak but no identity.nsec "
        "(previous setup may have been interrupted). Restoring the backup."
    )
    _restore_identity_nsec(data_dir, bak)


def _normalize_identity_choice(raw: str) -> Optional[str]:
    value = (raw or "").strip().lower()
    if value in ("c", "create", "new"):
        return "create"
    if value in ("n", "nsec", "import", "import nsec"):
        return "nsec"
    if value in ("m", "mnemonic", "seed", "import mnemonic"):
        return "mnemonic"
    return None


def _config_yaml_path() -> Path:
    try:
        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    return Path(home) / "config.yaml"


# D12 + Signal/Photon _TIER_LOW extras until /edit exists.
_VECTOR_DISPLAY_SETTINGS = {
    "tool_progress": "off",
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
}
_YAML11_AMBIGUOUS = {
    "y",
    "n",
    "yes",
    "no",
    "true",
    "false",
    "on",
    "off",
    "null",
    "~",
}


def _quote_yaml11_str(value: Any) -> Any:
    """Quote YAML 1.1 bool-like strings so ``off`` does not load as False."""
    if not (isinstance(value, str) and value.lower() in _YAML11_AMBIGUOUS):
        return value
    try:
        from ruamel.yaml.scalarstring import DoubleQuotedScalarString

        return DoubleQuotedScalarString(value)
    except ImportError:
        return value


def _merge_vector_display_config(config_path: Optional[Path] = None) -> bool:
    """D12: merge display.platforms.vector without clobbering other keys.

    Prefers ruamel round-trip so comments, key order, and quoting survive.
    Falls back to PyYAML (full dump) if ruamel is unavailable. Unparseable
    or non-mapping roots are refused rather than overwritten.
    """
    path = Path(config_path) if config_path else _config_yaml_path()
    if not _display_config_is_writable(path):
        return False
    if _merge_display_ruamel(path):
        return True
    return _merge_display_pyyaml(path)


def _display_config_is_writable(path: Path) -> bool:
    """False when an existing config.yaml must not be replaced."""
    if not path.exists():
        return True
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Vector: failed to read %s: %s", path, e)
        return False
    if not raw.strip():
        return True
    try:
        import yaml

        loaded = yaml.safe_load(raw)
    except Exception as e:
        logger.warning(
            "Vector: %s is unparseable (%s); refusing to overwrite", path, e
        )
        return False
    if loaded is not None and not isinstance(loaded, dict):
        logger.warning(
            "Vector: %s root is not a mapping; skipping display merge", path
        )
        return False
    return True


def _ensure_mapping(parent: dict, key: str) -> dict:
    current = parent.get(key)
    if not isinstance(current, dict):
        current = {}
        parent[key] = current
    return current


def _apply_vector_display_settings(root: dict) -> None:
    display = _ensure_mapping(root, "display")
    platforms = _ensure_mapping(display, "platforms")
    vector = _ensure_mapping(platforms, "vector")
    for key, value in _VECTOR_DISPLAY_SETTINGS.items():
        vector[key] = value


def _atomic_write_text(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".vector-config.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            writer(fh)
            fh.flush()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge_display_ruamel(path: Path) -> bool:
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap
    except ImportError:
        return False
    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    yaml_rt.allow_unicode = True
    yaml_rt.default_flow_style = False
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    try:
        data: Any = CommentedMap()
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            if raw.strip():
                with path.open("r", encoding="utf-8") as fh:
                    loaded = yaml_rt.load(fh)
                if loaded is None:
                    data = CommentedMap()
                elif not isinstance(loaded, dict):
                    return False
                else:
                    data = loaded
        if not isinstance(data, CommentedMap):
            data = CommentedMap(data)

        def _cm(parent, key):
            cur = parent.get(key)
            if isinstance(cur, CommentedMap):
                return cur
            nxt = CommentedMap(cur) if isinstance(cur, dict) else CommentedMap()
            parent[key] = nxt
            return nxt

        vector = _cm(_cm(_cm(data, "display"), "platforms"), "vector")
        for key, value in _VECTOR_DISPLAY_SETTINGS.items():
            vector[key] = _quote_yaml11_str(value)

        _atomic_write_text(path, lambda fh: yaml_rt.dump(data, fh))
        return True
    except Exception as e:
        logger.warning("Vector: ruamel display merge failed (%s); trying PyYAML", e)
        return False


def _merge_display_pyyaml(path: Path) -> bool:
    try:
        import yaml
    except ImportError:
        logger.warning("Vector: PyYAML not available; skipping display YAML merge")
        return False

    data: Dict[str, Any] = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Vector: failed to read %s: %s", path, e)
            return False
        if raw.strip():
            loaded = yaml.safe_load(raw)
            if loaded is None:
                data = {}
            elif not isinstance(loaded, dict):
                return False
            else:
                data = loaded

    _apply_vector_display_settings(data)

    class _Dumper(yaml.SafeDumper):
        pass

    def _represent_str(dumper, value):
        style = (
            '"'
            if isinstance(value, str) and value.lower() in _YAML11_AMBIGUOUS
            else None
        )
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _Dumper.add_representer(str, _represent_str)

    try:
        def _write(fh):
            yaml.dump(
                data,
                fh,
                Dumper=_Dumper,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )

        _atomic_write_text(path, _write)
    except Exception as e:
        logger.warning("Vector: failed to write display YAML to %s: %s", path, e)
        return False
    return True


def _ensure_bridge_binary(io) -> Optional[Path]:
    """Return vector-bridge path, cargo-building in bridge/ if the default is missing."""
    bin_path = resolve_bridge_bin()
    if bin_path.is_file():
        io.print_info(f"Using vector-bridge at {bin_path}")
        return bin_path

    override = (os.getenv("VECTOR_BRIDGE_BIN") or "").strip()
    if override and Path(override) != _DEFAULT_BRIDGE_BIN:
        io.print_error(f"VECTOR_BRIDGE_BIN={override} does not exist.")
        io.print_info(
            "Unset VECTOR_BRIDGE_BIN to let setup build "
            "bridge/target/release/vector-bridge, or point it at a built binary."
        )
        return None

    cargo = shutil.which("cargo")
    if not cargo:
        io.print_error("cargo not found. Install Rust 1.75+ from https://rustup.rs")
        io.print_info("  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh")
        io.print_info("Then re-run: hermes gateway setup")
        return None

    rustc = _probe_rustc()
    if rustc is None:
        io.print_error("rustc not found. Install Rust 1.75+ from https://rustup.rs")
        return None
    if rustc < MIN_RUSTC:
        io.print_error(
            f"rustc {rustc[0]}.{rustc[1]} is too old; vector-bridge needs >= 1.75"
        )
        return None

    cargo_toml = _BRIDGE_DIR / "Cargo.toml"
    if not cargo_toml.is_file():
        io.print_error(f"Bridge crate not found at {cargo_toml}")
        io.print_info("Reinstall the vector-platform plugin so bridge/ is present.")
        return None

    io.print_info(
        "Building vector-bridge (cargo build --release --locked; "
        "may take several minutes)..."
    )
    try:
        result = subprocess.run(
            [cargo, "build", "--release", "--locked"],
            cwd=str(_BRIDGE_DIR),
            timeout=CARGO_BUILD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        io.print_error(
            f"cargo build timed out after {CARGO_BUILD_TIMEOUT}s. Retry or build "
            "manually: cd bridge && cargo build --release --locked"
        )
        return None
    except OSError as e:
        io.print_error(f"cargo build failed to start: {e}")
        return None

    if result.returncode != 0:
        io.print_error(
            "cargo build --release --locked failed (see compiler output above)."
        )
        return None

    built = _DEFAULT_BRIDGE_BIN
    if not built.is_file():
        io.print_error(f"cargo build succeeded but {built} is missing")
        return None
    io.print_success(f"Built vector-bridge at {built}")
    return built


def _load_setup_io():
    """CLI printers/prompts. Lazy so the plugin stays importable in tests."""
    try:
        from hermes_cli.setup import (
            prompt,
            prompt_yes_no,
            save_env_value,
            get_env_value,
            print_header,
            print_info,
            print_warning,
            print_success,
            print_error,
        )
    except ImportError:
        from hermes_cli.config import get_env_value, save_env_value
        from hermes_cli.cli_output import (
            prompt,
            prompt_yes_no,
            print_header,
            print_info,
            print_warning,
            print_success,
            print_error,
        )
    return SimpleNamespace(
        prompt=prompt,
        prompt_yes_no=prompt_yes_no,
        save_env_value=save_env_value,
        get_env_value=get_env_value,
        print_header=print_header,
        print_info=print_info,
        print_warning=print_warning,
        print_success=print_success,
        print_error=print_error,
    )


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
    caption=None,
):
    """Out-of-process Vector delivery via the live sidecar HTTP API.

    Cron ``deliver=vector`` works when the gateway is up: this reads
    ``~/.hermes/runtime/vector-sidecar.json`` (0600) for port + token and
    POSTs ``/send`` with ``X-Hermes-Sidecar-Token``.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    port = _coerce_port(
        extra.get("bridge_port") or os.getenv("VECTOR_BRIDGE_PORT"),
        DEFAULT_BRIDGE_PORT,
    )
    host = _client_host(
        str(extra.get("bridge_host") or os.getenv("VECTOR_BRIDGE_HOST") or DEFAULT_BRIDGE_HOST)
    )

    token = None
    stale_hint = ""
    record = _read_runtime_record()
    if record and record.get("token"):
        if _sidecar_pid_alive(record.get("pid")):
            token = str(record["token"])
            port = _coerce_port(record.get("port"), port)
        else:
            stale_hint = (
                " A stale sidecar runtime record was found (pid "
                f"{record.get('pid')} is not running) — the gateway "
                "appears to be down."
            )

    if not token:
        return {
            "error": (
                "Vector standalone send requires a running sidecar. "
                "Start the Hermes gateway (which spawns vector-bridge and "
                "records its address under <hermes-home>/runtime/"
                f"{RUNTIME_RECORD_NAME})." + stale_hint
            )
        }

    url = f"http://{host}:{port}/send"
    headers = {SIDECAR_TOKEN_HEADER: token}
    text = message or ""
    has_media = bool(media_files)
    if not str(text).strip():
        if has_media:
            return {"error": "Vector media is not implemented in v1"}
        return {"error": "Vector send requires a message body"}
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(
                url,
                json={"to": chat_id, "body": text},
                headers=headers,
            )
            if resp.status_code != 200:
                err = (resp.text or "")[:200]
                return {
                    "error": f"Vector /send returned {resp.status_code}: {err}"
                }
            result: Dict[str, Any] = {
                "success": True,
                "platform": "vector",
                "chat_id": chat_id,
            }
            if has_media:
                result["warning"] = (
                    "Vector media is not implemented in v1; attachments were ignored"
                )
            return result
    except Exception as e:
        return {"error": f"Vector send failed: {e}"}


def _maybe_merge_display(io) -> None:
    if _merge_vector_display_config():
        io.print_info(
            "Wrote display.platforms.vector.tool_progress: off to config.yaml"
        )
    else:
        io.print_warning(
            "Could not merge display.platforms.vector into config.yaml. "
            "Add tool_progress: off under display.platforms.vector yourself "
            "or Hermes will post a new Vector DM per tool event."
        )


def _confirm_import_as_bot(io) -> bool:
    io.print_warning(
        "This identity will be tagged as a bot and will receive agent replies. "
        "Do not import your personal daily-driver nsec unless you intend that."
    )
    return io.prompt_yes_no("Import this identity as the Hermes bot?", False)


def _run_interactive_setup(io) -> None:
    """Wizard body (testable with a mocked io + subprocess)."""
    io.print_header("Vector")
    data_dir = Path(io.get_env_value("VECTOR_DATA_DIR") or resolve_data_dir())
    # Adopt before the already-configured early return: an interrupted
    # reconfigure leaves VECTOR_NPUB set and only identity.nsec.bak on disk.
    _adopt_stale_identity_backup(data_dir, io)
    existing_npub = (io.get_env_value("VECTOR_NPUB") or "").strip()
    if existing_npub:
        io.print_info(
            f"Vector: already configured (npub: {_truncate_npub(existing_npub)})"
        )
        if not io.prompt_yes_no("Reconfigure Vector?", False):
            _maybe_merge_display(io)
            return

    bin_path = _ensure_bridge_binary(io)
    if not bin_path:
        return

    io.print_info(f"Data dir: {data_dir}")
    _adopt_stale_identity_backup(data_dir, io)

    check_data, check_code, check_err = _run_bridge_cli(
        bin_path, data_dir, ["--check"], timeout=BRIDGE_CHECK_TIMEOUT
    )

    existing_identity_npub = None
    if check_data and check_data.get("status") == "existing":
        existing_identity_npub = (check_data.get("npub") or "").strip() or None

    identity_choice: Optional[str] = "create"
    import_secret = None
    import_kind = None  # "nsec" | "mnemonic"
    wipe_identity = False
    env_nsec = (io.get_env_value("VECTOR_NSEC") or "").strip()
    env_mnemonic = (io.get_env_value("VECTOR_MNEMONIC") or "").strip()

    if existing_identity_npub:
        io.print_warning(
            f"An identity already exists (npub: {existing_identity_npub})."
        )
        io.print_warning(
            "Replacing identity.nsec creates a NEW bot. Contacts will not recognize it."
        )
        if io.prompt_yes_no("Reconfigure identity anyway?", False):
            wipe_identity = True
        else:
            identity_choice = None
    elif check_code not in (0, None):
        check_code_name = (
            (check_data or {}).get("code") if isinstance(check_data, dict) else None
        )
        io.print_error(
            f"vector-bridge --check failed (exit {check_code}): {check_err or 'no output'}"
        )
        # Only offer replace for a corrupt nsec. Timeout/crash/wrong-arch
        # must not be described as an unreadable identity.
        if check_code_name == "invalid_nsec" or _identity_nsec_locally_unreadable(
            data_dir
        ):
            io.print_warning(
                "identity.nsec is unreadable (corrupt or invalid nsec). "
                "Replacing it creates a NEW bot."
            )
            if not io.prompt_yes_no("Replace the unreadable identity.nsec?", False):
                return
            wipe_identity = True
        else:
            return

    if identity_choice is not None:
        default_mode = "create"
        if env_nsec:
            default_mode = "nsec"
            io.print_info(
                "VECTOR_NSEC is set in .env; choosing 'nsec' will copy it into "
                "identity.nsec (then delete the env var)."
            )
        elif env_mnemonic:
            default_mode = "mnemonic"
            io.print_info(
                "VECTOR_MNEMONIC is set in .env; choosing 'mnemonic' will import it."
            )
        io.print_info(
            "Create a new Vector identity, or import an nsec / 12-word mnemonic."
        )
        raw_choice = io.prompt(
            "Identity [create / nsec / mnemonic]", default=default_mode
        )
        identity_choice = _normalize_identity_choice(raw_choice or default_mode)
        if identity_choice is None:
            io.print_error("Choose create, nsec, or mnemonic.")
            return
        if identity_choice in ("nsec", "mnemonic"):
            if not _confirm_import_as_bot(io):
                io.print_info("Import cancelled.")
                return
        if identity_choice == "nsec":
            import_secret = env_nsec or io.prompt("nsec (nsec1…; input hidden)", password=True)
            if not (import_secret or "").strip():
                io.print_error("nsec is required for import.")
                return
            import_kind = "nsec"
        elif identity_choice == "mnemonic":
            import_secret = env_mnemonic or io.prompt(
                "12-word mnemonic (input hidden)", password=True
            )
            words = (import_secret or "").split()
            if len(words) != 12:
                io.print_error("Invalid mnemonic — must be exactly 12 words.")
                return
            import_kind = "mnemonic"

    io.print_info(
        "Name, about, avatar, and banner are public Nostr kind-0 metadata: "
        "anyone who has the bot npub can fetch them from relays. Leave them "
        "blank to publish no profile card."
    )
    bot_name = (
        io.prompt(
            "Bot display name (optional, public; blank = do not publish)",
            default=io.get_env_value("VECTOR_BOT_NAME") or None,
        )
        or ""
    ).strip()
    bot_about = (
        io.prompt(
            "Bot about text (optional, public; blank = do not publish)",
            default=io.get_env_value("VECTOR_BOT_ABOUT") or None,
        )
        or ""
    ).strip()

    current_avatar = (io.get_env_value("VECTOR_BOT_AVATAR") or "").strip()
    if current_avatar:
        io.print_info(f"Current bot avatar: {current_avatar}")
    avatar_raw = (
        io.prompt(
            "Bot avatar image path (jpg/png/webp/gif; blank keeps current)",
            default=None,
        )
        or ""
    ).strip()
    pending_avatar: Optional[str] = None
    if avatar_raw:
        try:
            pending_avatar = str(validate_bot_image_src(avatar_raw))
        except ValueError as e:
            io.print_error(f"Avatar not installed: {e}")
            io.print_info("Continuing without changing the avatar.")

    current_banner = (io.get_env_value("VECTOR_BOT_BANNER") or "").strip()
    if current_banner:
        io.print_info(f"Current bot banner: {current_banner}")
    banner_raw = (
        io.prompt(
            "Bot banner image path (jpg/png/webp/gif; blank keeps current)",
            default=None,
        )
        or ""
    ).strip()
    pending_banner: Optional[str] = None
    if banner_raw:
        try:
            pending_banner = str(validate_bot_image_src(banner_raw))
        except ValueError as e:
            io.print_error(f"Banner not installed: {e}")
            io.print_info("Continuing without changing the banner.")

    io.print_info("Enter YOUR Vector npub (hex / npub1 / nostr:npub1).")
    io.print_info("This is who the bot will DM and who is allowed to message it.")
    existing_home = (io.get_env_value("VECTOR_HOME_CHANNEL") or "").strip()
    operator_raw = io.prompt(
        "Your Vector npub", default=existing_home or None
    )
    operator_npub = normalize_npub(operator_raw or "")
    if not operator_npub:
        io.print_error(
            "A valid Vector npub is required (hex, npub1…, or nostr:npub1)."
        )
        operator_raw = io.prompt("Your Vector npub")
        operator_npub = normalize_npub(operator_raw or "")
    if not operator_npub:
        io.print_error("Operator npub is required — aborting Vector setup.")
        return

    pairing_on = io.prompt_yes_no(
        "Enable pairing codes for unknown npubs?", True
    )

    existing_group_users = (io.get_env_value("VECTOR_GROUP_ALLOWED_USERS") or "").strip()
    io.print_info(
        "Group-only npubs can @mention the bot in those rooms without DM access. "
        "People already in VECTOR_ALLOWED_USERS can talk in groups automatically. "
        "Leave blank to skip."
    )
    group_users_raw = (
        io.prompt(
            "Group-only npubs (comma-separated)",
            default=existing_group_users or None,
        )
        or ""
    ).strip()
    group_users: List[str] = []
    for part in group_users_raw.split(","):
        npub = normalize_npub(part.strip())
        if npub and npub not in group_users:
            group_users.append(npub)
        elif part.strip() and not npub:
            io.print_warning(f"Skipping invalid group-only npub: {part.strip()[:20]}")

    existing_open = (io.get_env_value("VECTOR_GROUP_ALLOW_ALL") or "").strip()
    io.print_info(
        "Open channels: any member may @mention or reply to the bot "
        "(@everyone is ignored). Leave blank unless you want a whole room open."
    )
    open_raw = (
        io.prompt(
            "Open community channel ids (VECTOR_GROUP_ALLOW_ALL)",
            default=existing_open or None,
        )
        or ""
    ).strip()
    open_chats: List[str] = []
    for part in open_raw.split(","):
        cid = normalize_channel_id(part)
        if not part.strip():
            continue
        if not cid:
            io.print_warning(f"Skipping invalid open channel id: {part.strip()[:20]}")
            continue
        if cid not in open_chats:
            open_chats.append(cid)

    create_home = io.prompt_yes_no(
        "Also create a private home community owned by the bot?", False
    )
    community_name = ""
    if create_home:
        community_name = (
            io.prompt("Home community name", default="Hermes") or "Hermes"
        ).strip() or "Hermes"

    extra_args: List[str] = []
    temp_secret: Optional[Path] = None
    bak: Optional[Path] = None
    setup_ok = False
    setup_data: Optional[Dict[str, Any]] = None
    setup_code = 1
    setup_err = ""
    try:
        if wipe_identity:
            try:
                bak = _backup_identity_nsec(data_dir)
            except OSError as e:
                io.print_error(f"Could not replace identity.nsec: {e}")
                return
        if import_kind and import_secret:
            temp_secret = _write_temp_secret(import_secret, data_dir)
            flag = "--nsec-file" if import_kind == "nsec" else "--mnemonic-file"
            extra_args = [flag, str(temp_secret)]

        io.print_info("Running vector-bridge --setup...")
        setup_data, setup_code, setup_err = _run_bridge_cli(
            bin_path,
            data_dir,
            ["--setup", *extra_args],
            timeout=BRIDGE_SETUP_TIMEOUT,
        )
        bot_npub = ((setup_data or {}).get("npub") or "").strip()
        if setup_data and setup_code == 0 and bot_npub:
            setup_ok = True
        else:
            io.print_error(
                f"vector-bridge --setup failed (exit {setup_code}): "
                f"{setup_err or 'could not parse output'}"
            )
            return
    finally:
        if temp_secret is not None:
            _shred_unlink(temp_secret)
        # Ctrl+C / errors after the rename must put identity.nsec back.
        if setup_ok:
            _discard_identity_backup(bak)
        else:
            _restore_identity_nsec(data_dir, bak)

    bot_npub = ((setup_data or {}).get("npub") or "").strip()
    status = (setup_data or {}).get("status") or ""
    if not bot_npub:
        io.print_error("Bridge returned incomplete data (no npub).")
        return

    existing_allowed = io.get_env_value("VECTOR_ALLOWED_USERS") or ""
    io.save_env_value("VECTOR_NPUB", bot_npub)
    io.save_env_value("VECTOR_BOT_NAME", bot_name)
    io.save_env_value("VECTOR_BOT_ABOUT", bot_about)
    if pending_avatar:
        try:
            installed = install_bot_image(pending_avatar, data_dir, "avatar")
            io.save_env_value("VECTOR_BOT_AVATAR", str(installed))
        except ValueError as e:
            io.print_error(f"Avatar not installed: {e}")
    if pending_banner:
        try:
            installed = install_bot_image(pending_banner, data_dir, "banner")
            io.save_env_value("VECTOR_BOT_BANNER", str(installed))
        except ValueError as e:
            io.print_error(f"Banner not installed: {e}")
    io.save_env_value("VECTOR_DATA_DIR", str(data_dir))
    io.save_env_value("VECTOR_HOME_CHANNEL", operator_npub)
    io.save_env_value(
        "VECTOR_ALLOWED_USERS", _merge_allowed_users(operator_npub, existing_allowed)
    )
    io.save_env_value("VECTOR_PAIRING", "on" if pairing_on else "off")
    if group_users:
        io.save_env_value("VECTOR_GROUP_ALLOWED_USERS", ",".join(group_users))
    if open_chats:
        io.save_env_value("VECTOR_GROUP_ALLOW_ALL", ",".join(open_chats))
    io.save_env_value("VECTOR_CREATE_COMMUNITY", "on" if create_home else "off")
    if create_home:
        io.save_env_value("VECTOR_COMMUNITY_NAME", community_name)

    if env_nsec:
        io.print_warning(
            "VECTOR_NSEC is still in .env. Delete it — the sidecar never reads it."
        )
    if env_mnemonic:
        io.print_warning(
            "VECTOR_MNEMONIC is still in .env. Delete it after you have a backup."
        )

    _maybe_merge_display(io)

    if status == "created":
        io.print_success(f"Account created! Bot npub: {bot_npub}")
    elif status == "restored":
        io.print_success(f"Account restored! Bot npub: {bot_npub}")
    else:
        io.print_success(f"Existing account found! Bot npub: {bot_npub}")
    io.print_info("Share this npub with contacts.")
    io.print_info(
        "Communities: invite the bot from a trusted npub; it auto-joins and "
        "listens. @mention or reply to take a turn. "
        "VECTOR_GROUP_ALLOWED_USERS is group-only; VECTOR_GROUP_ALLOW_ALL "
        "opens a listed channel to every member (mention/reply only)."
    )
    if create_home:
        io.print_info(
            "VECTOR_CREATE_COMMUNITY=on — the gateway will create or reuse a "
            "private home community after Ready (no public invite link)."
        )
    io.print_info(
        f"Back up {data_dir / 'identity.nsec'} offline — replacing it is a new bot."
    )
    io.print_success("Vector configured!")
    io.print_info("Restart the gateway: hermes gateway restart")


def interactive_setup() -> None:
    """Interactive ``hermes gateway setup`` flow for Vector."""
    _run_interactive_setup(_load_setup_io())


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    _ensure_hermes_connect_timeout_floor()
    ctx.register_redaction_patterns([r"nsec1[a-z0-9]{20,}"])
    ctx.register_platform(
        name="vector",
        label="Vector",
        adapter_factory=lambda cfg: VectorAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["VECTOR_NPUB"],
        install_hint=(
            "Requires a built vector-bridge (Rust 1.75+). "
            "Run: hermes plugins enable vector-platform "
            "&& hermes gateway setup  (builds the sidecar). "
            "Do not expect hermes gateway start to compile Rust."
        ),
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="VECTOR_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        parse_target_ref_fn=_parse_target_ref,
        allowed_users_env="VECTOR_ALLOWED_USERS",
        allow_all_env="VECTOR_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🛡️",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are on Vector, a private encrypted messenger built on Nostr. "
            "You are a bot account (your profile is tagged bot: true) with your "
            "own npub. Peers are identified by npub1… bech32 keys. "
            "Markdown is rendered. Keep replies concise. "
            "DMs are 1:1. Community channels are mention-gated: only reply when "
            "someone @mentions you (npub or display name), replies to your "
            "message, or sends a Vector slash command (/approve, /deny). "
            "@everyone is not a mention. Group chat_id is a 64-char "
            "hex channel id."
        ),
    )
