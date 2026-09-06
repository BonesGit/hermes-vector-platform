"""Vector platform adapter for Hermes Agent.

Registers the ``vector`` platform, npub helpers, and a ``BasePlatformAdapter``
that owns a Rust ``vector-bridge`` sidecar over loopback HTTP/SSE.

Vector users are identified by a bech32 ``npub1…`` public key. Hermes maps
DMs as ``chat_id = user_id = peer npub``, ``chat_type = "dm"``, and Concord
channels as ``chat_id = channel hex``, ``chat_type = "group"``,
``user_id = sender npub``.

Required env vars / config.extra keys:
    VECTOR_NPUB                  Bot public key (npub1…)
    VECTOR_ALLOWED_USERS         Comma-separated npubs allowed to DM (also
                                 grants community turns; pairing is DM-only)
    VECTOR_HOME_CHANNEL          Operator npub for cron + join notices

Profile, communities, reactions, pairing, and prebuilt sidecar fetch live in
config.yaml ``vector:`` (see ``_apply_yaml_config``). Sidecar plumbing
(port/host/bin/data dir) stays getenv overrides with defaults. Legacy
VECTOR_* env for those keys still wins.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import platform
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
PLUGIN_VERSION = "0.4.0"
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
DEFAULT_RELEASE_REPO = "BonesGit/hermes-vector-platform"
PREBUILT_MAX_BYTES = 80 * 1024 * 1024
PREBUILT_DOWNLOAD_TIMEOUT = 60.0
_RELEASE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RELEASE_TAG_RE = re.compile(r"^v?[A-Za-z0-9._-]+$")


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
    """Sidecar path: override, in-tree release build, then versioned prebuilt."""
    override = (os.getenv("VECTOR_BRIDGE_BIN") or "").strip()
    if override:
        return Path(override)
    if _DEFAULT_BRIDGE_BIN.is_file():
        return _DEFAULT_BRIDGE_BIN
    prebuilt = _prebuilt_bridge_bin()
    if prebuilt.is_file() and _prebuilt_version_matches():
        return prebuilt
    return _DEFAULT_BRIDGE_BIN


def bridge_release_target() -> Optional[str]:
    """Rust target triple for a GitHub Release asset, or None if unsupported."""
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        return None
    if sys.platform == "linux":
        return f"{arch}-unknown-linux-gnu"
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    return None


def _hermes_home() -> Path:
    try:
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _prebuilt_bin_dir() -> Path:
    return _hermes_home() / "plugin-data" / "vector-platform" / "bin"


def _prebuilt_bridge_bin() -> Path:
    return _prebuilt_bin_dir() / "vector-bridge"


def _prebuilt_version_stamp() -> Path:
    return _prebuilt_bin_dir() / ".version"


def _prebuilt_yaml() -> dict:
    """``vector.prebuilt`` from config.yaml. Empty when the block is absent."""
    block = _read_vector_yaml_block()
    prebuilt = block.get("prebuilt")
    return prebuilt if isinstance(prebuilt, dict) else {}


def _release_repo() -> str:
    raw = str(_prebuilt_yaml().get("repo") or "").strip()
    if raw and _RELEASE_REPO_RE.fullmatch(raw):
        return raw
    return DEFAULT_RELEASE_REPO


def _release_tag() -> str:
    raw = str(_prebuilt_yaml().get("tag") or "").strip()
    if raw and _RELEASE_TAG_RE.fullmatch(raw):
        return raw if raw.startswith("v") else f"v{raw}"
    return f"v{PLUGIN_VERSION}"


def _skip_prebuilt_download() -> bool:
    cfg = _prebuilt_yaml()
    if "download" not in cfg:
        return False
    return _yaml_on_off(cfg.get("download")) == "off"


def _prebuilt_version_matches() -> bool:
    stamp = _prebuilt_version_stamp()
    if not stamp.is_file():
        return False
    try:
        return stamp.read_text(encoding="utf-8").strip() == _release_tag()
    except OSError:
        return False


def _github_release_url(asset: str) -> str:
    return (
        f"https://github.com/{_release_repo()}/releases/download/"
        f"{_release_tag()}/{asset}"
    )


def _parse_sha256sums(text: str, asset: str) -> Optional[str]:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[-1].lstrip("*").split("/")[-1]
        if name != asset:
            continue
        digest = parts[0].lower()
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest
    return None


def _http_get_bytes(url: str, *, max_bytes: int = PREBUILT_MAX_BYTES) -> bytes:
    headers = {"User-Agent": f"hermes-vector-platform/{PLUGIN_VERSION}"}
    with httpx.Client(
        timeout=PREBUILT_DOWNLOAD_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    ) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            buf = bytearray()
            for chunk in resp.iter_bytes(chunk_size=65536):
                if len(buf) + len(chunk) > max_bytes:
                    raise ValueError(
                        f"download from {url} exceeded {max_bytes} bytes"
                    )
                buf.extend(chunk)
            return bytes(buf)


def _try_install_prebuilt_bridge(io) -> Optional[Path]:
    """Download a versioned GitHub Release binary. None if skipped or failed."""
    if _skip_prebuilt_download():
        return None
    target = bridge_release_target()
    if target is None:
        io.print_info(
            f"No prebuilt vector-bridge for {sys.platform}/{platform.machine()}; "
            "will try cargo if available."
        )
        return None

    asset = f"vector-bridge-{target}"
    dest = _prebuilt_bridge_bin()
    try:
        io.print_info(
            f"Downloading {asset} from GitHub Release {_release_tag()}..."
        )
        sums = _http_get_bytes(
            _github_release_url("SHA256SUMS"), max_bytes=64 * 1024
        ).decode("utf-8")
        expected = _parse_sha256sums(sums, asset)
        if not expected:
            io.print_info(
                f"SHA256SUMS has no entry for {asset}; will try cargo if available."
            )
            return None
        raw = _http_get_bytes(_github_release_url(asset))
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected:
            io.print_error(
                f"Checksum mismatch for {asset}: got {digest}, expected {expected}"
            )
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=".vector-bridge.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
            os.chmod(tmp_name, 0o755)
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        _prebuilt_version_stamp().write_text(
            _release_tag() + "\n", encoding="utf-8"
        )
        io.print_success(f"Installed prebuilt vector-bridge at {dest}")
        if sys.platform == "darwin":
            io.print_info(
                "If macOS blocks the binary: "
                f"xattr -d com.apple.quarantine {dest}"
            )
        return dest
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status == 404:
            io.print_info(
                f"No GitHub Release asset for {_release_tag()}/{asset}; "
                "will try cargo if available."
            )
        else:
            io.print_info(
                f"Prebuilt download failed (HTTP {status}); "
                "will try cargo if available."
            )
        return None
    except Exception as e:
        io.print_info(f"Prebuilt download failed ({e}); will try cargo if available.")
        return None


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


def discover_bot_image(data_dir: Path, stem: str) -> Optional[Path]:
    """Return ``{data_dir}/{stem}.<ext>`` if a supported image exists."""
    if stem not in ("avatar", "banner"):
        return None
    root = Path(data_dir)
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        path = root / f"{stem}{suffix}"
        if path.is_file():
            return path
    return None


def install_bot_avatar(src: str, data_dir: Path) -> Path:
    return install_bot_image(src, data_dir, "avatar")


def install_bot_banner(src: str, data_dir: Path) -> Path:
    return install_bot_image(src, data_dir, "banner")


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


def _send_target(chat_id: str) -> str:
    """Sidecar ``to``: 64-hex channel first (do not encode as npub), else npub."""
    channel = normalize_channel_id(chat_id)
    if channel:
        return channel
    return normalize_npub(chat_id) or (chat_id or "").strip()


def _pending_inbox_key(peer: str, channel_id: Optional[str] = None) -> str:
    """Pending file-only inbox: DM = peer npub; group = ``{channel}:{peer}``."""
    cid = normalize_channel_id(channel_id or "") if channel_id else None
    if cid:
        return f"{cid}:{peer}"
    return peer


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

_BLOCK_COMMAND_RE = re.compile(
    r"^/(block|unblock|blocked)(?:\s+(\S+))?\s*$",
    re.IGNORECASE,
)
_INVITE_COMMAND_RE = re.compile(
    r"^/(invites|join|decline)(?:\s+(\S+))?\s*$",
    re.IGNORECASE,
)


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


def _mention_remainder(
    text: str, bot_npub: Optional[str], bot_name: Optional[str] = None
) -> str:
    """Text with bot @mentions stripped. Empty means mention-only (no extra ask)."""
    body = text or ""
    npub = normalize_npub(bot_npub or "") if bot_npub else None
    if npub:
        body = body.replace(f"@{npub}", " ")
        body = body.replace(f"nostr:{npub}", " ")
        body = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(npub)}(?![A-Za-z0-9])", " ", body
        )
    name = (bot_name or "").strip()
    if name:
        body = re.sub(rf"@{re.escape(name)}\b", " ", body, flags=re.IGNORECASE)
    return " ".join(body.split())


def _community_download_all() -> bool:
    """VECTOR_COMMUNITY_DOWNLOAD_ALL default off. on = ingest every group file."""
    return _env_flag("VECTOR_COMMUNITY_DOWNLOAD_ALL") in ("1", "true", "yes", "on")


def _group_file_pending_path(channel_id: str, msg_id: str) -> Path:
    safe_id = _sanitize_filename(msg_id or "event")
    return resolve_files_root() / "pending" / channel_id / f"{safe_id}.json"


def _group_file_pointer_path(msg_id: str) -> Path:
    return resolve_files_root() / "by-event" / f"{_sanitize_filename(msg_id or 'event')}.json"


def _reply_to_bot(reply_to: Optional[str], sent_ids) -> bool:
    rid = (reply_to or "").strip()
    return bool(rid) and rid in sent_ids


def _home_operator_npub() -> Optional[str]:
    """VECTOR_HOME_CHANNEL as npub, or None."""
    return normalize_npub((os.getenv("VECTOR_HOME_CHANNEL") or "").strip())


def _format_joined_notice(
    community_id: str, channels: list, community_name: str = ""
) -> str:
    """Operator-facing DM/log body with copy-pasteable channel ids."""
    title = (community_name or "").strip()
    lines = [
        f"Vector: I joined {title}." if title else "Vector: I joined a community.",
        "Copy a channel_id into vector.communities.open_channels in config.yaml "
        "if you want every member to @mention me.",
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


def _format_pending_invites(rows: list) -> str:
    """Home-DM body for parked Concord invites (no unsolicited notify)."""
    if not rows:
        return "No parked invites."
    lines = [f"Parked invites ({len(rows)}):"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("community_id") or "").strip()
        name = str(row.get("name") or "").strip()
        inviter = str(row.get("inviter_npub") or "").strip()
        title = name or "(unnamed)"
        lines.append(f"- {title}")
        if cid:
            lines.append(f"  community_id: {cid}")
        if inviter:
            lines.append(f"  from: {_truncate_npub(inviter)}")
    lines.append("")
    lines.append("Join: /join <community_id>")
    lines.append("Decline: /decline <community_id>")
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


def _profile_display_name(data: Optional[Dict[str, Any]], fallback: str) -> str:
    """Kind-0 ``name``, then ``display_name``, else a truncated npub/hex."""
    if isinstance(data, dict):
        for key in ("name", "display_name"):
            label = str(data.get(key) or "").strip()
            if label:
                return label
    return _truncate_npub(fallback)


_DEFAULT_CHANNEL_NAMES = frozenset({"general"})


def _group_chat_name(
    community_name: Optional[str],
    channel_name: Optional[str],
    fallback: str = "",
) -> str:
    """Vector list title: community name. Append channel only if it isn't ``general``."""
    community = (community_name or "").strip()
    channel = (channel_name or "").strip()
    if channel and channel.lower() not in _DEFAULT_CHANNEL_NAMES:
        if community:
            return f"{community} · {channel}"
        return channel
    if community:
        return community
    return _truncate_npub(fallback)


def _env_flag(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip().lower()


def _pairing_enabled() -> bool:
    """Pairing codes unless VECTOR_PAIRING is off or YAML says ignore.

    Default on. ``unauthorized_dm_behavior: ignore`` is bridged to
    ``VECTOR_PAIRING=off`` by ``_apply_yaml_config``.
    """
    return _env_flag("VECTOR_PAIRING", "on") not in (
        "off",
        "0",
        "false",
        "no",
        "disabled",
        "ignore",
    )


def _processing_reactions_enabled() -> bool:
    """VECTOR_REACTIONS default off. 👀/✅/❌ on the triggering DM while the agent works."""
    return _env_flag("VECTOR_REACTIONS") in ("1", "true", "yes", "on")


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
    """Adapter-layer DM allowlist (VECTOR_ALLOWED_USERS)."""
    npub = normalize_npub(peer) or (peer or "").strip()
    if not npub:
        return False
    return npub in _allowed_npubs()


def _is_superseded_replay(msg_data: dict) -> bool:
    """True when the sidecar replayed this message and a newer one followed.

    Set only on ``Last-Event-ID`` replay after a reconnect. The newest message
    per chat is never superseded, so every chat still gets exactly one turn.
    """
    if not isinstance(msg_data, dict):
        return False
    return bool(msg_data.get("replayed")) and bool(msg_data.get("superseded"))


def _is_home_operator(peer: str) -> bool:
    """True when this DM is ``VECTOR_HOME_CHANNEL``.

    Operator commands (mute, parked invites) stay here. Allowlisted users
    and pairing-approved senders do not grant this.
    """
    npub = normalize_npub(peer) or (peer or "").strip()
    home = _home_operator_npub()
    return bool(npub and home and npub == home)


def _parse_block_command(text: str) -> Optional[Tuple[str, str]]:
    """Typed ``/block`` / ``/unblock`` / ``/blocked`` in a DM. None if not a match."""
    m = _BLOCK_COMMAND_RE.match((text or "").strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


def _parse_invite_command(text: str) -> Optional[Tuple[str, str]]:
    """Typed ``/invites`` / ``/join`` / ``/decline`` in a DM. None if not a match."""
    m = _INVITE_COMMAND_RE.match((text or "").strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


def _group_sender_is_authorized(peer: str, channel_id: str) -> bool:
    """Who may trigger a community turn.

    Union: channel in ``VECTOR_GROUP_ALLOW_ALL``, DM allowlist
    (``VECTOR_ALLOWED_USERS``), or ``VECTOR_GROUP_ALLOWED_USERS``.
    Pairing is never offered in a channel.
    """
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
    SUPPORTS_MESSAGE_EDITING = True

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
        raw_banner = (
            extra.get("bot_banner") or os.getenv("VECTOR_BOT_BANNER") or ""
        ).strip()
        self.startup_timeout: int = int(
            os.getenv("VECTOR_STARTUP_TIMEOUT")
            or extra.get("startup_timeout")
            or DEFAULT_STARTUP_TIMEOUT
        )
        self._npub: Optional[str] = extra.get("npub") or (os.getenv("VECTOR_NPUB") or "").strip() or None
        self.data_dir: Path = Path(extra.get("data_dir") or resolve_data_dir())
        self.bot_avatar: Optional[Path] = (
            Path(raw_avatar).expanduser() if raw_avatar else discover_bot_image(self.data_dir, "avatar")
        )
        self.bot_banner: Optional[Path] = (
            Path(raw_banner).expanduser() if raw_banner else discover_bot_image(self.data_dir, "banner")
        )
        self.bridge_url: str = f"http://{_client_host(self.bridge_host)}:{self.bridge_port}"

        self._bridge_process: Optional[subprocess.Popen] = None
        self._bridge_log: Optional[Path] = None
        self._bridge_log_fh = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._sidecar_token: Optional[str] = None
        # Last SSE id we finished dispatching. Sent as Last-Event-ID so the
        # sidecar replays the gap instead of dropping it on reconnect.
        self._sse_last_event_id: str = ""
        self._inbound_ids: OrderedDict[str, None] = OrderedDict()
        self._last_inbound_by_chat: OrderedDict[str, str] = OrderedDict()
        self._sent_message_ids: OrderedDict[str, None] = OrderedDict()
        self._seen_reaction_ids: OrderedDict[str, None] = OrderedDict()
        self._blocked_npubs: set = set()
        self._blocked_loaded: bool = False
        # File-only saves waiting to be attached to the next gated text.
        # DMs key by peer npub; groups key by ``{channel_id}:{peer}``.
        self._pending_inbox: Dict[str, List[Tuple[str, str]]] = {}
        self._notified_channel_ids: set = _load_notified_channel_ids(self.data_dir)
        # Peer npub → kind-0 label; channel hex → Concord channel name.
        self._profile_names: Dict[str, str] = {}
        self._channel_names: Dict[str, str] = {}
        self._community_names: Dict[str, str] = {}
        self._channel_community: Dict[str, str] = {}

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
                f"{bin_path}. Run `hermes gateway setup` (downloads a prebuilt "
                "sidecar, or cargo-builds one) or set VECTOR_BRIDGE_BIN. "
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

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent Vector message (kind-16 / Concord send_edit).

        Hermes tool-progress and streaming hold one ``message_id`` for the
        whole bubble. The sidecar's kind-16 rumor has its own id; this
        method always returns the original target id. ``finalize`` is a
        no-op — Vector edits have no lifecycle state.
        """
        if not self._running or not self._http_client:
            return SendResult(success=False, error="Not connected")
        target = (message_id or "").strip()
        if not target:
            return SendResult(success=False, error="Vector edit needs a message id")
        if not content:
            return SendResult(success=False, error="Empty message")

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

        payload: Dict[str, Any] = {
            "to": chat_id,
            "message_id": target,
            "body": content,
        }
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/edit",
                json=payload,
                headers=self._token_headers(),
                timeout=30.0,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError) as e:
                    logger.warning(
                        "Vector: /edit returned 200 but JSON was unreadable: %s",
                        e,
                    )
                    return SendResult(success=True, message_id=target, retryable=False)
                if not isinstance(data, dict):
                    data = {}
                edit_id = data.get("edit_id")
                if edit_id:
                    self._record_sent_message(str(edit_id))
                    self._is_duplicate(str(edit_id))
                return SendResult(
                    success=True,
                    message_id=target,
                    raw_response=data,
                )
            error_text = resp.text[:200] if resp.text else "No error text"
            logger.warning(
                "Vector: /edit failed with status %d: %s",
                resp.status_code,
                error_text,
            )
            return SendResult(
                success=False,
                error=f"Sidecar /edit returned {resp.status_code}: {error_text}",
                retryable=resp.status_code >= 500,
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logger.error("Vector: connection error while editing: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)
        except Exception as e:
            logger.error("Vector: exception while editing: %s", e)
            return SendResult(success=False, error=str(e), retryable=False)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Retract a previously sent Vector message (NIP-09 / Concord tombstone).

        Hermes uses this for ephemeral TTL and stream-consumer preview
        cleanup. Failures are non-fatal — the caller leaves the bubble.
        """
        if not self._http_client or not (message_id or "").strip():
            return False
        payload: Dict[str, Any] = {
            "to": _send_target(chat_id),
            "message_id": message_id.strip(),
        }
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/delete",
                json=payload,
                headers=self._token_headers(),
                timeout=15.0,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug("Vector: delete_message failed: %s", e)
            return False

    async def block_user(self, npub: str, *, unblock: bool = False) -> bool:
        """Mute or unmute a DM peer at the Vector layer (not Concord kick/ban)."""
        target = normalize_npub(npub) or (npub or "").strip()
        if not self._http_client or not target:
            return False
        body: Dict[str, Any] = {"npub": target}
        if unblock:
            body["unblock"] = True
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/block",
                json=body,
                headers=self._token_headers(),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("Vector: block_user failed: %s", e)
            return False
        if resp.status_code != 200:
            return False
        if unblock:
            self._blocked_npubs.discard(target)
        else:
            self._blocked_npubs.add(target)
        self._blocked_loaded = True
        return True

    async def list_blocked(self) -> List[Dict[str, Any]]:
        """Blocked DM peers from the sidecar mute list."""
        await self._refresh_blocked()
        if not self._http_client:
            return [{"npub": n, "name": "", "display_name": ""} for n in sorted(self._blocked_npubs)]
        try:
            resp = await self._http_client.get(
                f"{self.bridge_url}/block",
                headers=self._token_headers(),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("Vector: list_blocked failed: %s", e)
            return [{"npub": n, "name": "", "display_name": ""} for n in sorted(self._blocked_npubs)]
        if resp.status_code != 200:
            return [{"npub": n, "name": "", "display_name": ""} for n in sorted(self._blocked_npubs)]
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return [{"npub": n, "name": "", "display_name": ""} for n in sorted(self._blocked_npubs)]
        rows = data.get("blocked") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        out: List[Dict[str, Any]] = []
        found: set = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            npub = normalize_npub(str(row.get("npub") or "")) or str(row.get("npub") or "").strip()
            if not npub:
                continue
            found.add(npub)
            out.append(
                {
                    "npub": npub,
                    "name": str(row.get("name") or ""),
                    "display_name": str(row.get("display_name") or ""),
                }
            )
        self._blocked_npubs = found
        self._blocked_loaded = True
        return out

    async def list_pending_invites(self) -> List[Dict[str, Any]]:
        """Parked Concord invites from the sidecar (silent until listed)."""
        if not self._http_client:
            return []
        try:
            resp = await self._http_client.get(
                f"{self.bridge_url}/invites",
                headers=self._token_headers(),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("Vector: list_pending_invites failed: %s", e)
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return []
        rows = data.get("invites") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cid = normalize_channel_id(str(row.get("community_id") or "")) or str(
                row.get("community_id") or ""
            ).strip()
            if not cid:
                continue
            out.append(
                {
                    "community_id": cid,
                    "name": str(row.get("name") or ""),
                    "inviter_npub": str(row.get("inviter_npub") or ""),
                    "version": row.get("version"),
                }
            )
        return out

    async def accept_invite(self, community_id: str) -> Optional[Dict[str, Any]]:
        """Accept a parked invite. Returns sidecar JSON or None."""
        cid = normalize_channel_id(community_id)
        if not self._http_client or not cid:
            return None
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/invites/accept",
                json={"community_id": cid},
                headers=self._token_headers(),
                timeout=60.0,
            )
        except Exception as e:
            logger.debug("Vector: accept_invite failed: %s", e)
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    async def decline_invite(self, community_id: str) -> bool:
        """Drop a parked invite without joining."""
        cid = normalize_channel_id(community_id)
        if not self._http_client or not cid:
            return False
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/invites/decline",
                json={"community_id": cid},
                headers=self._token_headers(),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("Vector: decline_invite failed: %s", e)
            return False
        return resp.status_code == 200

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

    def _forget_last_inbound(self, chat_id: Optional[str], message_id: Optional[str]) -> None:
        if not chat_id or not message_id:
            return
        key = self._chat_key(chat_id)
        if self._last_inbound_by_chat.get(key) == message_id:
            self._last_inbound_by_chat.pop(key, None)

    async def _refresh_blocked(self) -> None:
        """Load the sidecar mute list into ``_blocked_npubs``."""
        if not self._http_client:
            self._blocked_loaded = True
            return
        try:
            resp = await self._http_client.get(
                f"{self.bridge_url}/block",
                headers=self._token_headers(),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("Vector: refresh blocked list failed: %s", e)
            return
        if resp.status_code != 200:
            return
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return
        rows = data.get("blocked") if isinstance(data, dict) else None
        found: set = set()
        if isinstance(rows, list):
            for row in rows:
                raw = row.get("npub") if isinstance(row, dict) else row
                npub = normalize_npub(str(raw or "")) or str(raw or "").strip()
                if npub:
                    found.add(npub)
        self._blocked_npubs = found
        self._blocked_loaded = True

    async def _is_blocked(self, peer: str) -> bool:
        npub = normalize_npub(peer) or (peer or "").strip()
        if not npub:
            return False
        if not self._blocked_loaded:
            await self._refresh_blocked()
        return npub in self._blocked_npubs

    async def _try_block_command(self, peer: str, text: str) -> bool:
        """Handle typed ``/block`` / ``/unblock`` / ``/blocked`` from the home DM.

        Only ``VECTOR_HOME_CHANNEL`` may issue these. Returns True when the
        message is consumed (no Hermes turn).
        """
        parsed = _parse_block_command(text)
        if not parsed or not _is_home_operator(peer):
            return False
        cmd, arg = parsed
        if cmd == "blocked":
            rows = await self.list_blocked()
            if not rows:
                body = "No blocked users."
            else:
                lines = [f"Blocked ({len(rows)}):"]
                for row in rows:
                    npub = str(row.get("npub") or "")
                    label = (
                        str(row.get("name") or "").strip()
                        or str(row.get("display_name") or "").strip()
                    )
                    shown = _truncate_npub(npub) if npub else npub
                    lines.append(f"- {shown}" + (f" ({label})" if label else ""))
                body = "\n".join(lines)
            await self.send(peer, body)
            return True
        target = normalize_npub(arg)
        if not target:
            await self.send(peer, f"Usage: /{cmd} <npub>")
            return True
        bot = normalize_npub(self._npub or "") if self._npub else None
        commander = normalize_npub(peer) or peer
        if (bot and target == bot) or target == commander:
            await self.send(peer, "Cannot block that npub.")
            return True
        ok = await self.block_user(target, unblock=(cmd == "unblock"))
        if not ok:
            await self.send(peer, f"Could not {cmd} {_truncate_npub(target)}.")
            return True
        verb = "Unblocked" if cmd == "unblock" else "Blocked"
        await self.send(peer, f"{verb} {_truncate_npub(target)}.")
        return True

    async def _try_invite_command(self, peer: str, text: str) -> bool:
        """Handle typed parked-invite commands from the home DM.

        Only ``VECTOR_HOME_CHANNEL`` may issue these. Parked invites are never
        pushed to home; this is the pull path. Returns True when consumed
        (no Hermes turn).
        """
        parsed = _parse_invite_command(text)
        if not parsed or not _is_home_operator(peer):
            return False
        cmd, arg = parsed
        if cmd == "invites":
            rows = await self.list_pending_invites()
            await self.send(peer, _format_pending_invites(rows))
            return True
        cid = normalize_channel_id(arg)
        if not cid:
            await self.send(peer, f"Usage: /{cmd} <community_id>")
            return True
        if cmd == "join":
            data = await self.accept_invite(cid)
            if not data:
                await self.send(
                    peer,
                    f"Could not join {cid}. Is that invite still parked?",
                )
                return True
            community_id = str(data.get("community_id") or cid)
            name = str(data.get("name") or "").strip()
            channels = data.get("channels") if isinstance(data, dict) else None
            rows = self._ingest_joined_channels(
                community_id,
                channels if isinstance(channels, list) else [],
                community_name=name,
            )
            if rows:
                body = _format_joined_notice(
                    community_id, rows, community_name=name
                )
                self._mark_channels_notified(row["channel_id"] for row in rows)
            else:
                title = name or "the community"
                body = f"Joined {title}."
            await self.send(peer, body)
            return True
        dropped = await self.decline_invite(cid)
        if not dropped:
            await self.send(
                peer,
                f"Could not decline {cid}. Is that invite still parked?",
            )
            return True
        await self.send(peer, f"Declined invite {cid}.")
        return True

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
            name = await self._group_label(channel)
            return {
                "name": name,
                "type": "group",
                "chat_id": channel,
            }
        npub = normalize_npub(chat_id) or (chat_id or "").strip()
        name = await self._fetch_profile_name(npub)
        return {"name": name, "type": "dm", "chat_id": npub}

    def _peer_label(self, peer: str) -> str:
        return self._profile_names.get(peer) or _truncate_npub(peer)

    def _remember_group_room(
        self,
        community_id: str,
        channel_id: str,
        *,
        community_name: str = "",
        channel_name: str = "",
    ) -> None:
        cid = normalize_channel_id(channel_id) or (channel_id or "").strip()
        comm = (community_id or "").strip()
        if cid and comm:
            self._channel_community[cid] = comm
        if comm and community_name.strip():
            self._community_names[comm] = community_name.strip()
        if cid and channel_name.strip():
            self._channel_names[cid] = channel_name.strip()

    def _group_label_cached(
        self, channel_id: str, community_id: Optional[str] = None
    ) -> str:
        comm_id = (
            (community_id or "").strip()
            or self._channel_community.get(channel_id)
            or ""
        )
        return _group_chat_name(
            self._community_names.get(comm_id),
            self._channel_names.get(channel_id),
            comm_id or channel_id,
        )

    async def _group_label(self, channel_id: str, community_id: Optional[str] = None) -> str:
        label = self._group_label_cached(channel_id, community_id)
        if label and label != _truncate_npub(community_id or channel_id):
            return label
        if self._http_client:
            await self._sync_joined_channels()
            return self._group_label_cached(channel_id, community_id)
        return _truncate_npub(community_id or channel_id)

    async def _fetch_profile_name(self, npub: str) -> str:
        """Pull kind-0 via sidecar ``GET /profile``. Cache the label for inbound."""
        if not npub:
            return ""
        if not self._http_client:
            return self._peer_label(npub)
        try:
            resp = await self._http_client.get(
                f"{self.bridge_url}/profile",
                params={"npub": npub},
                headers=self._token_headers(),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("Vector: fetch profile failed: %s", e)
            return self._peer_label(npub)
        if resp.status_code != 200:
            return self._peer_label(npub)
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return self._peer_label(npub)
        label = _profile_display_name(data if isinstance(data, dict) else None, npub)
        if label and label != _truncate_npub(npub):
            self._profile_names[npub] = label
        return label

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
                [{"channel_id": channel_id, "name": ""}],
                community_name=name,
            )

    def _ingest_joined_channels(
        self,
        community_id: str,
        channels: list,
        *,
        community_name: str = "",
    ) -> list:
        """Remember channel ids for a joined community. Does not DM home."""
        comm_name = community_name.strip() or self._community_names.get(
            (community_id or "").strip(), ""
        )
        if (community_id or "").strip() and comm_name:
            self._community_names[(community_id or "").strip()] = comm_name
        rows = []
        for row in channels or []:
            if not isinstance(row, dict):
                continue
            cid = normalize_channel_id(str(row.get("channel_id") or ""))
            if not cid:
                continue
            _remember_channel(cid)
            self._remember_group_room(
                community_id,
                cid,
                community_name=comm_name,
                channel_name=str(row.get("name") or "").strip(),
            )
            rows.append(
                {
                    "channel_id": cid,
                    "name": str(row.get("name") or "").strip(),
                }
            )
        return rows

    def _mark_channels_notified(self, channel_ids) -> None:
        self._notified_channel_ids.update(channel_ids)
        _save_notified_channel_ids(self.data_dir, self._notified_channel_ids)

    async def _notify_joined_channels(
        self,
        community_id: str,
        channels: list,
        *,
        community_name: str = "",
    ) -> None:
        """Log full channel ids and DM VECTOR_HOME_CHANNEL once per channel."""
        comm_name = community_name.strip() or self._community_names.get(
            (community_id or "").strip(), ""
        )
        rows = self._ingest_joined_channels(
            community_id, channels, community_name=comm_name
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
                "Vector: joined channel_id=%s name=%s community=%s community_id=%s",
                row["channel_id"],
                row["name"] or "(unnamed)",
                comm_name or "(unnamed)",
                community,
            )
        home = _home_operator_npub()
        if home:
            if not (self._running and self._http_client):
                logger.debug(
                    "Vector: not connected; will DM channel_id to VECTOR_HOME_CHANNEL later"
                )
                return
            result = await self.send(
                home,
                _format_joined_notice(
                    community_id, new_rows, community_name=comm_name
                ),
            )
            if not result.success:
                logger.warning(
                    "Vector: could not DM VECTOR_HOME_CHANNEL the channel id: %s",
                    result.error,
                )
                return
        self._mark_channels_notified(row["channel_id"] for row in new_rows)

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
            community_name = str(community.get("name") or "").strip()
            if community_id and community_name:
                self._community_names[community_id] = community_name
            channels = community.get("channels")
            if not isinstance(channels, list):
                continue
            channel_rows = []
            for ch in channels:
                if not isinstance(ch, dict):
                    continue
                cid = normalize_channel_id(str(ch.get("channel_id") or ""))
                _remember_channel(cid)
                ch_name = str(ch.get("name") or "").strip()
                if cid:
                    self._remember_group_room(
                        community_id,
                        cid,
                        community_name=community_name,
                        channel_name=ch_name,
                    )
                    channel_rows.append(
                        {
                            "channel_id": cid,
                            "name": str(ch.get("name") or ""),
                        }
                    )
            if channel_rows:
                await self._notify_joined_channels(
                    community_id, channel_rows, community_name=community_name
                )

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
        to = _send_target(chat_id)
        try:
            resp = await self._http_client.post(
                f"{self.bridge_url}/send-file",
                json={"to": to, "path": safe},
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
                chat_id=to,
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
                headers = {
                    **self._token_headers(),
                    "Accept": "text/event-stream",
                }
                if self._sse_last_event_id:
                    headers["Last-Event-ID"] = self._sse_last_event_id
                async with self._http_client.stream(
                    "GET",
                    url,
                    headers=headers,
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
                    pending_id = ""
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
                            if line.startswith("id:"):
                                pending_id = line[3:].strip()
                                continue
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if not data_str:
                                    continue
                                try:
                                    data = json.loads(data_str)
                                    await self._dispatch_sse_event(data)
                                    # Commit the resume point only once the event
                                    # is handed off; a failure leaves it
                                    # uncommitted so the sidecar replays it.
                                    if pending_id:
                                        self._sse_last_event_id = pending_id
                                except json.JSONDecodeError:
                                    logger.debug(
                                        "Vector SSE: invalid JSON: %s",
                                        data_str[:120],
                                    )
                                except Exception:
                                    logger.exception(
                                        "Vector SSE: error handling event"
                                    )
                                finally:
                                    pending_id = ""

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
        elif event_type == "message_delete":
            await self._handle_message_delete(data)
        elif event_type == "community_joined":
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            inner = inner or {}
            channels = inner.get("channels") if isinstance(inner.get("channels"), list) else []
            community_id = str(inner.get("community_id") or "")
            community_name = str(inner.get("name") or "").strip()
            if channels:
                await self._notify_joined_channels(
                    community_id, channels, community_name=community_name
                )
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

        if await self._is_blocked(peer):
            logger.info(
                "Vector: dropping blocked sender %s",
                _truncate_npub(peer),
            )
            return

        # VECTOR_PAIRING=off: drop before handle_message so pairing codes are not sent.
        if not _pairing_enabled() and not _sender_is_authorized(peer):
            logger.info(
                "Vector: dropping unauthorized sender %s (VECTOR_PAIRING=off)",
                _truncate_npub(peer),
            )
            return

        if await self._try_block_command(peer, text):
            if msg_id:
                self._record_last_inbound(peer, msg_id)
            return

        if await self._try_invite_command(peer, text):
            if msg_id:
                self._record_last_inbound(peer, msg_id)
            return

        if msg_id:
            self._record_last_inbound(peer, msg_id)

        name = self._peer_label(peer)
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

        pending_key = _pending_inbox_key(peer)
        if is_file and not text.strip():
            if _sender_is_authorized(peer):
                await self._ack_file_only(peer, saved)
                await self._write_inbox_breadcrumb(source, saved)
                self._queue_pending_inbox(pending_key, saved)
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

        if _is_superseded_replay(msg_data):
            logger.info(
                "Vector: replayed message superseded by a newer one from %s; "
                "filing as context",
                _truncate_npub(peer),
            )
            await self._file_superseded_replay(source, text, saved)
            return

        media_urls, media_types, msg_type = self._media_for_event(
            pending_key, saved, is_file=is_file
        )

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
        """Mention-gated Concord channel message → Hermes ``chat_type=group``.

        Vector's client sends community files with empty caption (no @mention on
        the file event). Default: stash metadata only, download when someone
        replies to that file and @mentions the bot. Mention-only reply = store
        + session breadcrumb, no turn. Mention + extra text = turn with
        ``media_urls``. ``VECTOR_COMMUNITY_DOWNLOAD_ALL=on`` downloads on
        arrival (still silent; the same reply+mention starts a turn).
        """
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
        attachments = (
            msg_data.get("attachments")
            if isinstance(msg_data.get("attachments"), list)
            else []
        )
        is_file = bool(msg_data.get("is_file") or attachments)
        if not text.strip() and not is_file:
            logger.debug("Vector: skip empty group message id=%s", msg_id[:16])
            return

        raw_peer = msg_data.get("npub") or ""
        peer = normalize_npub(raw_peer) or str(raw_peer).strip()
        if not peer:
            return

        bot_npub = normalize_npub(self._npub or "") if self._npub else None
        if bot_npub and peer == bot_npub:
            return

        community_id = str(msg_data.get("community_id") or "") or None
        at_ms = msg_data.get("at_ms")

        if is_file and attachments and msg_id:
            self._stash_group_file_pending(
                channel_id,
                msg_id,
                peer=peer,
                attachments=attachments,
                community_id=community_id,
                at_ms=at_ms,
            )
            if _community_download_all():
                saved = await self._ingest_group_file_event(
                    channel_id,
                    msg_id,
                    caption=text,
                )
                if saved:
                    await self._write_inbox_breadcrumb(
                        self._group_source(
                            channel_id, peer, msg_id, community_id
                        ),
                        saved,
                    )
            if not text.strip():
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

        source = self._group_source(channel_id, peer, msg_id, community_id)
        reply_to_text = msg_data.get("reply_to_text") or None

        saved: List[Tuple[Path, dict, str]] = []
        if reply_to:
            saved = await self._ingest_group_file_event(
                channel_id, reply_to, caption=text
            )
        if is_file and attachments and msg_id:
            this_saved = await self._ingest_group_file_event(
                channel_id, msg_id, caption=text
            )
            saved.extend(this_saved)

        remainder = _mention_remainder(text, bot_npub, self.bot_name)
        mention_only = (
            saved
            and not remainder
            and not _group_slash_command(text, is_command=is_command)
            and not _reply_to_bot(reply_to, self._sent_message_ids)
        )
        if mention_only:
            await self._write_inbox_breadcrumb(source, saved)
            return

        if _is_superseded_replay(msg_data):
            logger.info(
                "Vector: replayed channel message superseded by a newer one; "
                "filing as context",
            )
            await self._file_superseded_replay(source, text, saved)
            return

        media_urls = [str(path) for path, _att, _mime in saved]
        media_types = [mime for _path, _att, mime in saved]
        msg_type = MessageType.TEXT
        if media_types:
            msg_type = _message_type_for_mime(media_types[0])
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

    async def _handle_message_delete(self, msg_data: dict) -> None:
        """Peer (or we) deleted a bubble. No Hermes turn — forget local pointers."""
        if (
            isinstance(msg_data, dict)
            and msg_data.get("type") == "message_delete"
            and "data" in msg_data
        ):
            msg_data = msg_data["data"]
        if not isinstance(msg_data, dict):
            return
        msg_id = str(msg_data.get("id") or msg_data.get("message_id") or "")
        chat_id = str(msg_data.get("chat_id") or "")
        if msg_id:
            self._is_duplicate(msg_id)
        self._forget_last_inbound(chat_id, msg_id)
        if msg_id:
            pointer = _group_file_pointer_path(msg_id)
            try:
                pointer.unlink(missing_ok=True)
            except OSError:
                pass
        channel = normalize_channel_id(chat_id)
        if channel and msg_id:
            pending = _group_file_pending_path(channel, msg_id)
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
        logger.debug(
            "Vector: message deleted id=%s chat=%s",
            (msg_id or "")[:16],
            _truncate_npub(chat_id) if chat_id else "",
        )

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
        if _is_superseded_replay(msg_data):
            # A stale reaction is not worth a turn once a newer event for the
            # same chat has already been replayed.
            return
        if bot_npub and author == bot_npub:
            return
        if not _pairing_enabled() and not _sender_is_authorized(peer):
            return
        name = self._peer_label(peer)
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

    def _queue_pending_inbox(
        self, key: str, saved: List[Tuple[Path, dict, str]]
    ) -> None:
        pending = self._pending_inbox.setdefault(key, [])
        seen = {p for p, _m in pending}
        for path, _att, mime in saved:
            path_key = str(path)
            if path_key not in seen:
                pending.append((path_key, mime))
                seen.add(path_key)

    def _media_for_event(
        self,
        key: str,
        saved: List[Tuple[Path, dict, str]],
        *,
        is_file: bool,
    ) -> Tuple[List[str], List[str], MessageType]:
        media_urls: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT
        if saved:
            media_urls = [str(path) for path, _att, _mime in saved]
            media_types = [mime for _path, _att, mime in saved]
        elif not is_file:
            pending = self._pending_inbox.pop(key, [])
            media_urls = [p for p, _m in pending]
            media_types = [m for _p, m in pending]
        if media_types:
            msg_type = _message_type_for_mime(media_types[0])
        if saved:
            self._pending_inbox.pop(key, None)
        return media_urls, media_types, msg_type

    def _group_source(self, channel_id: str, peer: str, msg_id: str, community_id: Optional[str]):
        name = self._peer_label(peer)
        chat_name = self._group_label_cached(channel_id, community_id)
        return self.build_source(
            chat_id=channel_id,
            chat_name=chat_name,
            chat_type="group",
            user_id=peer,
            user_name=name,
            message_id=msg_id or None,
            parent_chat_id=community_id,
            scope_id=community_id,
            role_authorized=True,
        )

    def _stash_group_file_pending(
        self,
        channel_id: str,
        msg_id: str,
        *,
        peer: str,
        attachments: List[dict],
        community_id: Optional[str],
        at_ms: Any,
    ) -> None:
        path = _group_file_pending_path(channel_id, msg_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "channel_id": channel_id,
                        "msg_id": msg_id,
                        "peer": peer,
                        "attachments": attachments,
                        "community_id": community_id or "",
                        "at_ms": at_ms,
                    },
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Vector: failed to stash group file metadata id=%s", msg_id[:16])

    def _load_group_file_pointer(
        self, msg_id: str
    ) -> List[Tuple[Path, dict, str]]:
        path = _group_file_pointer_path(msg_id)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        rows = data.get("files") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        saved: List[Tuple[Path, dict, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            file_path = Path(str(row.get("path") or ""))
            if not file_path.is_file():
                return []
            mime = str(row.get("mime") or "application/octet-stream")
            att = {
                "id": row.get("attachment_id") or "",
                "name": row.get("name") or file_path.name,
            }
            saved.append((file_path, att, mime))
        return saved

    def _write_group_file_pointer(
        self, msg_id: str, saved: List[Tuple[Path, dict, str]]
    ) -> None:
        if not msg_id or not saved:
            return
        path = _group_file_pointer_path(msg_id)
        payload = {
            "msg_id": msg_id,
            "files": [
                {
                    "path": str(file_path),
                    "mime": mime,
                    "name": att.get("name") or file_path.name,
                    "attachment_id": att.get("id"),
                }
                for file_path, att, mime in saved
            ],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("Vector: failed to write by-event pointer for %s", msg_id[:16])

    async def _ingest_group_file_event(
        self,
        channel_id: str,
        event_id: str,
        *,
        caption: str,
    ) -> List[Tuple[Path, dict, str]]:
        """Return already-saved bytes for ``event_id``, or download from pending metadata."""
        existing = self._load_group_file_pointer(event_id)
        if existing:
            return existing
        pending_path = _group_file_pending_path(channel_id, event_id)
        if not pending_path.is_file():
            return []
        try:
            data = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(data, dict):
            return []
        attachments = data.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return []
        peer = str(data.get("peer") or "")
        saved = await self._save_inbound_attachments(
            peer,
            attachments,
            msg_id=event_id,
            caption=caption,
            at_ms=data.get("at_ms"),
            channel_id=channel_id,
            community_id=str(data.get("community_id") or "") or None,
        )
        if saved:
            self._write_group_file_pointer(event_id, saved)
        return saved

    async def _save_inbound_attachments(
        self,
        peer: str,
        attachments: List[dict],
        *,
        msg_id: str,
        caption: str,
        at_ms: Any,
        channel_id: Optional[str] = None,
        community_id: Optional[str] = None,
    ) -> List[Tuple[Path, dict, str]]:
        """Download attachments into files/inbox/{npub|channel/npub}/{YYYY-MM-DD}/."""
        saved: List[Tuple[Path, dict, str]] = []
        max_bytes = _inbound_media_max_bytes()
        try:
            when = datetime.fromtimestamp(int(at_ms) / 1000.0) if at_ms else datetime.now()
        except (TypeError, ValueError, OSError):
            when = datetime.now()
        day = when.strftime("%Y-%m-%d")
        stamp = when.strftime("%H%M%S")
        cid = normalize_channel_id(channel_id or "") if channel_id else None
        if cid:
            inbox = resolve_files_root() / "inbox" / cid / peer / day
        else:
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
            if cid:
                meta["channel_id"] = cid
            if community_id:
                meta["community_id"] = community_id
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

    async def _ack_file_only(self, chat_id: str, saved: List[Tuple[Path, dict, str]]) -> None:
        if not saved:
            await self.send(chat_id=chat_id, content="couldn't save attachment")
            return
        names = [path.name.split("-", 1)[-1] if "-" in path.name else path.name for path, *_ in saved]
        if len(names) == 1:
            body = f"saved {names[0]}"
        else:
            body = f"saved {len(names)} files: " + ", ".join(names)
        await self.send(chat_id=chat_id, content=body)

    async def _file_superseded_replay(
        self, source, text: str, saved: List[Tuple[Path, dict, str]]
    ) -> None:
        """Record a superseded replayed message as context, with no agent turn.

        A reconnect can hand back a whole burst the peer sent while the stream
        was down. Someone who fires off five messages is waiting on an answer to
        the last one, not five answers — and five turns is five times the GPU.
        The sidecar flags every replayed message that a newer one in the same
        chat supersedes; those land in the session transcript so the agent still
        sees them, and only the newest actually runs.
        """
        lines = [
            "[Vector] Earlier message, delivered late after a reconnect "
            "(context only — answered as part of the newest message):"
        ]
        if text.strip():
            lines.append(text.strip())
        for path, att, mime in saved:
            orig = att.get("name") or path.name
            lines.append(f"- attachment {orig} ({mime}) `{path}`")
        try:
            await asyncio.to_thread(
                self._append_session_breadcrumb, source, "\n".join(lines)
            )
        except Exception:
            logger.warning(
                "Vector: failed to write superseded replay breadcrumb", exc_info=True
            )

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
        extra = getattr(self.config, "extra", None) or {}
        if isinstance(extra, dict):
            _overlay_sidecar_extra_env(env, extra)
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
    """Side-effect free: VECTOR_NPUB set and a matching vector-bridge present."""
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
            "name": "Home",
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


def _overlay_sidecar_extra_env(env: Dict[str, str], extra: dict) -> None:
    """Copy YAML-seeded extra flags into sidecar env when unset."""
    mapping = (
        ("VECTOR_INVITE_POLICY", "invite_policy"),
        ("VECTOR_TRUSTED_INVITERS", "trusted_inviters"),
        ("VECTOR_SLASH_COMMANDS", "slash_commands"),
        ("VECTOR_MISSED_REACT", "missed_react"),
        ("VECTOR_MISSED_REACT_EMOJI", "missed_react_emoji"),
        ("VECTOR_SSE_REPLAY_MAX", "replay_max"),
        ("VECTOR_SSE_REPLAY_MAX_AGE_SECS", "replay_max_age_secs"),
        ("VECTOR_COMMUNITY_NAME", "community_name"),
        ("VECTOR_CREATE_COMMUNITY", "create_community"),
        ("VECTOR_COMMUNITY_DOWNLOAD_ALL", "community_download_all"),
        ("VECTOR_REACTIONS", "reactions"),
        ("VECTOR_GROUP_ALLOWED_USERS", "group_allowed_users"),
        ("VECTOR_GROUP_ALLOW_ALL", "group_allowed_chats"),
    )
    for env_key, extra_key in mapping:
        if (env.get(env_key) or "").strip():
            continue
        val = extra.get(extra_key)
        if val is None or val == "":
            continue
        if isinstance(val, bool):
            env[env_key] = "on" if val else "off"
        elif isinstance(val, (list, tuple)):
            env[env_key] = ",".join(str(v).strip() for v in val if str(v).strip())
        else:
            env[env_key] = str(val)


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


def _backup_identity_file(data_dir: Path, name: str) -> Optional[Path]:
    """Rename ``name`` → ``name.bak``. None if missing."""
    src = Path(data_dir) / name
    if not src.is_file():
        return None
    bak = Path(data_dir) / f"{name}.bak"
    if bak.exists():
        bak.unlink()
    src.replace(bak)
    return bak


def _backup_identity_nsec(data_dir: Path) -> Optional[Path]:
    """Rename ``identity.nsec`` → ``identity.nsec.bak``. None if missing."""
    return _backup_identity_file(data_dir, "identity.nsec")


def _backup_identity(data_dir: Path) -> List[Path]:
    """Move nsec and mnemonic aside so a failed replace can put them back."""
    baks: List[Path] = []
    for name in ("identity.nsec", "identity.mnemonic"):
        bak = _backup_identity_file(data_dir, name)
        if bak is not None:
            baks.append(bak)
    return baks


def _restore_identity_backup(bak: Optional[Path]) -> None:
    """Rename ``foo.bak`` → ``foo`` next to it."""
    if bak is None or not bak.is_file():
        return
    name = bak.name
    if not name.endswith(".bak"):
        return
    src = bak.with_name(name[: -len(".bak")])
    try:
        if src.exists():
            src.unlink()
    except OSError:
        pass
    try:
        bak.replace(src)
    except OSError as e:
        logger.warning("Vector: failed to restore %s from backup: %s", src.name, e)


def _restore_identity_nsec(data_dir: Path, bak: Optional[Path]) -> None:
    """Put the backup back if ``--setup`` failed after the rename."""
    _restore_identity_backup(bak)


def _discard_identity_backup(bak: Optional[Path]) -> None:
    if bak is None:
        return
    try:
        bak.unlink(missing_ok=True)
    except OSError:
        pass


def _discard_identity_backups(baks: List[Path]) -> None:
    for bak in baks:
        _discard_identity_backup(bak)


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
    """If a previous setup left only ``*.bak`` identity files, put them back."""
    for name in ("identity.nsec", "identity.mnemonic"):
        src = Path(data_dir) / name
        bak = Path(data_dir) / f"{name}.bak"
        try:
            src_ok = src.is_file() and src.stat().st_size > 0
        except OSError:
            src_ok = False
        if src_ok or not bak.is_file():
            continue
        io.print_warning(
            f"Found {name}.bak but no {name} "
            "(previous setup may have been interrupted). Restoring the backup."
        )
        _restore_identity_backup(bak)


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


# D12: POST /edit lets Hermes accumulate tool-progress on one bubble.
# Streaming extras stay off — each token edit is another NIP-17 gift wrap.
_VECTOR_DISPLAY_SETTINGS = {
    "tool_progress": "new",
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
    "streaming": False,
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


def _merge_vector_display_config(
    config_path: Optional[Path] = None,
    platform: Optional[Dict[str, Any]] = None,
) -> bool:
    """D12: merge display.platforms.vector without clobbering other keys.

    Prefers ruamel round-trip so comments, key order, and quoting survive.
    Falls back to PyYAML (full dump) if ruamel is unavailable. Unparseable
    or non-mapping roots are refused rather than overwritten. ``platform``
    is merged into the top-level ``vector:`` block (None values delete keys).
    """
    path = Path(config_path) if config_path else _config_yaml_path()
    if not _display_config_is_writable(path):
        return False
    if _merge_display_ruamel(path, platform):
        return True
    return _merge_display_pyyaml(path, platform)


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


def _apply_vector_platform_settings(root: dict, platform: Optional[Dict[str, Any]]) -> None:
    """Replace named keys under top-level ``vector:``. None / empty pops."""
    if not platform:
        return
    vector = _ensure_mapping(root, "vector")
    for key, value in platform.items():
        if value is None or value == {} or value == []:
            vector.pop(key, None)
        else:
            vector[key] = value
    if not vector:
        root.pop("vector", None)


def _read_vector_yaml_block(config_path: Optional[Path] = None) -> dict:
    """Best-effort load of top-level ``vector:`` from config.yaml."""
    path = Path(config_path) if config_path else _config_yaml_path()
    if not path.is_file():
        return {}
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    block = loaded.get("vector")
    return block if isinstance(block, dict) else {}


def _yaml_list_to_csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _yaml_on_off(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value).strip().lower()
    if text in ("on", "true", "1", "yes"):
        return "on"
    if text in ("off", "false", "0", "no"):
        return "off"
    return None


def _yaml_count(value: Any) -> Optional[str]:
    """Non-negative integer as a string, or None when it is not one.

    ``0`` is meaningful for the replay knobs (disable / no limit), so it must
    survive. Bools are rejected on purpose: ``max_messages: false`` is a typo,
    not a count, and falling back to the documented default beats silently
    turning replay off.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return str(count) if count >= 0 else None


def _set_env_if_unset(key: str, value: Optional[str], *, skip: bool) -> None:
    if skip or value is None:
        return
    if (os.getenv(key) or "").strip():
        return
    os.environ[key] = value


def _build_setup_vector_yaml(
    *,
    bot_name: str,
    bot_about: str,
    pairing_on: bool,
    group_users: List[str],
    open_chats: List[str],
    create_home: bool,
    community_name: str,
) -> Dict[str, Any]:
    """Wizard answers as a top-level ``vector:`` mapping. Empties clear keys."""
    bot: Dict[str, Any] = {}
    if bot_name:
        bot["name"] = bot_name
    if bot_about:
        bot["about"] = bot_about
    communities: Dict[str, Any] = {}
    if group_users:
        communities["group_allowed_users"] = list(group_users)
    if open_chats:
        communities["open_channels"] = list(open_chats)
    if create_home:
        communities["create"] = True
        if community_name:
            communities["name"] = community_name
    return {
        "bot": bot or None,
        "communities": communities or None,
        "unauthorized_dm_behavior": None if pairing_on else "ignore",
    }


def _profile_scoped_config_load() -> bool:
    """True inside a multiplexed secondary profile's secret scope."""
    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active

        return bool(is_multiplex_active() and current_secret_scope() is not None)
    except Exception:
        return False


def _apply_yaml_config(yaml_cfg: dict, vector_cfg: dict) -> Optional[dict]:
    """Translate config.yaml ``vector:`` keys into env + PlatformConfig.extra.

    Env wins. Sidecar still reads VECTOR_* process env; this hook is the
    YAML→env bridge (same pattern as Discord / Mattermost).
    """
    if not isinstance(vector_cfg, dict):
        vector_cfg = {}
    skip = _profile_scoped_config_load()
    seeded: Dict[str, Any] = {}

    bot = vector_cfg.get("bot")
    if isinstance(bot, dict):
        name = str(bot.get("name") or "").strip()
        about = str(bot.get("about") or "").strip()
        avatar = str(bot.get("avatar") or "").strip()
        banner = str(bot.get("banner") or "").strip()
        if name:
            seeded["bot_name"] = name
            _set_env_if_unset("VECTOR_BOT_NAME", name, skip=skip)
        if about:
            seeded["bot_about"] = about
            _set_env_if_unset("VECTOR_BOT_ABOUT", about, skip=skip)
        if avatar:
            seeded["bot_avatar"] = avatar
            _set_env_if_unset("VECTOR_BOT_AVATAR", avatar, skip=skip)
        if banner:
            seeded["bot_banner"] = banner
            _set_env_if_unset("VECTOR_BOT_BANNER", banner, skip=skip)

    if "reactions" in vector_cfg:
        reactions = _yaml_on_off(vector_cfg.get("reactions"))
        if reactions is not None:
            seeded["reactions"] = reactions
            _set_env_if_unset("VECTOR_REACTIONS", reactions, skip=skip)
    if "missed_react" in vector_cfg:
        missed = _yaml_on_off(vector_cfg.get("missed_react"))
        if missed is not None:
            seeded["missed_react"] = missed
            _set_env_if_unset("VECTOR_MISSED_REACT", missed, skip=skip)
    emoji = str(vector_cfg.get("missed_react_emoji") or "").strip()
    if emoji:
        seeded["missed_react_emoji"] = emoji
        _set_env_if_unset("VECTOR_MISSED_REACT_EMOJI", emoji, skip=skip)
    if "slash_commands" in vector_cfg:
        slash = _yaml_on_off(vector_cfg.get("slash_commands"))
        if slash is not None:
            seeded["slash_commands"] = slash
            _set_env_if_unset("VECTOR_SLASH_COMMANDS", slash, skip=skip)

    replay = vector_cfg.get("replay")
    if isinstance(replay, dict):
        for yaml_key, extra_key, env_key in (
            ("max_messages", "replay_max", "VECTOR_SSE_REPLAY_MAX"),
            ("max_age_secs", "replay_max_age_secs", "VECTOR_SSE_REPLAY_MAX_AGE_SECS"),
        ):
            if yaml_key not in replay:
                continue
            count = _yaml_count(replay.get(yaml_key))
            if count is None:
                logger.warning(
                    "Vector: ignoring vector.replay.%s=%r (want a non-negative "
                    "integer); using the default",
                    yaml_key,
                    replay.get(yaml_key),
                )
                continue
            seeded[extra_key] = count
            _set_env_if_unset(env_key, count, skip=skip)

    prebuilt = vector_cfg.get("prebuilt")
    if isinstance(prebuilt, dict):
        if "download" in prebuilt:
            download = _yaml_on_off(prebuilt.get("download"))
            if download is not None:
                seeded["prebuilt_download"] = download
            else:
                logger.warning(
                    "Vector: ignoring vector.prebuilt.download=%r "
                    "(want true/false)",
                    prebuilt.get("download"),
                )
        repo = str(prebuilt.get("repo") or "").strip()
        if repo:
            if _RELEASE_REPO_RE.fullmatch(repo):
                seeded["prebuilt_repo"] = repo
            else:
                logger.warning(
                    "Vector: ignoring vector.prebuilt.repo=%r "
                    "(want owner/name)",
                    repo,
                )
        tag = str(prebuilt.get("tag") or "").strip()
        if tag:
            if _RELEASE_TAG_RE.fullmatch(tag):
                seeded["prebuilt_tag"] = tag if tag.startswith("v") else f"v{tag}"
            else:
                logger.warning(
                    "Vector: ignoring vector.prebuilt.tag=%r",
                    tag,
                )

    behavior = str(vector_cfg.get("unauthorized_dm_behavior") or "").strip().lower()
    if behavior == "ignore":
        _set_env_if_unset("VECTOR_PAIRING", "off", skip=skip)
    elif behavior == "pair":
        _set_env_if_unset("VECTOR_PAIRING", "on", skip=skip)

    communities = vector_cfg.get("communities")
    if isinstance(communities, dict):
        if "create" in communities:
            create = _yaml_on_off(communities.get("create"))
            if create is not None:
                seeded["create_community"] = create
                _set_env_if_unset("VECTOR_CREATE_COMMUNITY", create, skip=skip)
        cname = str(communities.get("name") or "").strip()
        if cname:
            seeded["community_name"] = cname
            _set_env_if_unset("VECTOR_COMMUNITY_NAME", cname, skip=skip)
        if "download_all" in communities:
            download = _yaml_on_off(communities.get("download_all"))
            if download is not None:
                seeded["community_download_all"] = download
                _set_env_if_unset("VECTOR_COMMUNITY_DOWNLOAD_ALL", download, skip=skip)
        policy = str(communities.get("invite_policy") or "").strip().lower()
        if policy:
            seeded["invite_policy"] = policy
            _set_env_if_unset("VECTOR_INVITE_POLICY", policy, skip=skip)
        group_users = _yaml_list_to_csv(communities.get("group_allowed_users"))
        if group_users:
            seeded["group_allowed_users"] = group_users
            _set_env_if_unset("VECTOR_GROUP_ALLOWED_USERS", group_users, skip=skip)
        open_channels = _yaml_list_to_csv(communities.get("open_channels"))
        if open_channels:
            seeded["group_allowed_chats"] = open_channels
            _set_env_if_unset("VECTOR_GROUP_ALLOW_ALL", open_channels, skip=skip)
        inviters = _yaml_list_to_csv(communities.get("trusted_inviters"))
        if inviters:
            seeded["trusted_inviters"] = inviters
            _set_env_if_unset("VECTOR_TRUSTED_INVITERS", inviters, skip=skip)

    return seeded or None


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


def _merge_display_ruamel(path: Path, platform: Optional[Dict[str, Any]] = None) -> bool:
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
        _apply_vector_platform_settings(data, platform)

        _atomic_write_text(path, lambda fh: yaml_rt.dump(data, fh))
        return True
    except Exception as e:
        logger.warning("Vector: ruamel display merge failed (%s); trying PyYAML", e)
        return False


def _merge_display_pyyaml(
    path: Path, platform: Optional[Dict[str, Any]] = None
) -> bool:
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
    _apply_vector_platform_settings(data, platform)

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
    """Return vector-bridge: existing file, GitHub prebuilt, then cargo build."""
    bin_path = resolve_bridge_bin()
    if bin_path.is_file():
        io.print_info(f"Using vector-bridge at {bin_path}")
        return bin_path

    override = (os.getenv("VECTOR_BRIDGE_BIN") or "").strip()
    if override and Path(override) != _DEFAULT_BRIDGE_BIN:
        io.print_error(f"VECTOR_BRIDGE_BIN={override} does not exist.")
        io.print_info(
            "Unset VECTOR_BRIDGE_BIN to let setup download or build "
            "vector-bridge, or point it at a built binary."
        )
        return None

    prebuilt = _try_install_prebuilt_bridge(io)
    if prebuilt is not None and prebuilt.is_file():
        return prebuilt

    cargo = shutil.which("cargo")
    if not cargo:
        io.print_error("cargo not found. Install Rust 1.75+ from https://rustup.rs")
        io.print_info(
            "Or wait for a GitHub Release with a prebuilt sidecar for this platform."
        )
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


def _maybe_merge_display(io, platform: Optional[Dict[str, Any]] = None) -> None:
    if _merge_vector_display_config(platform=platform):
        io.print_info(
            "Wrote display.platforms.vector.tool_progress: new to config.yaml"
        )
        if platform:
            io.print_info("Wrote vector: settings to config.yaml")
    else:
        io.print_warning(
            "Could not merge Vector settings into config.yaml. "
            "Add tool_progress: new under display.platforms.vector yourself "
            "or Hermes inherits the global default (all)."
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
    existing_vector = _read_vector_yaml_block()
    bot_yaml = existing_vector.get("bot")
    bot_yaml = bot_yaml if isinstance(bot_yaml, dict) else {}
    comm_yaml = existing_vector.get("communities")
    comm_yaml = comm_yaml if isinstance(comm_yaml, dict) else {}
    bot_name = (
        io.prompt(
            "Bot display name (optional, public; blank = do not publish)",
            default=(bot_yaml.get("name") or io.get_env_value("VECTOR_BOT_NAME") or None),
        )
        or ""
    ).strip()
    bot_about = (
        io.prompt(
            "Bot about text (optional, public; blank = do not publish)",
            default=(bot_yaml.get("about") or io.get_env_value("VECTOR_BOT_ABOUT") or None),
        )
        or ""
    ).strip()

    current_avatar = discover_bot_image(data_dir, "avatar")
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

    current_banner = discover_bot_image(data_dir, "banner")
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

    existing_group_users = _yaml_list_to_csv(comm_yaml.get("group_allowed_users")) or (
        io.get_env_value("VECTOR_GROUP_ALLOWED_USERS") or ""
    ).strip()
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

    existing_open = _yaml_list_to_csv(comm_yaml.get("open_channels")) or (
        io.get_env_value("VECTOR_GROUP_ALLOW_ALL") or ""
    ).strip()
    io.print_info(
        "Open channels: any member may @mention or reply to the bot "
        "(@everyone is ignored). Leave blank unless you want a whole room open."
    )
    open_raw = (
        io.prompt(
            "Open community channel ids (64-hex)",
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
    baks: List[Path] = []
    setup_ok = False
    setup_data: Optional[Dict[str, Any]] = None
    setup_code = 1
    setup_err = ""
    try:
        if wipe_identity:
            try:
                baks = _backup_identity(data_dir)
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
        # Ctrl+C / errors after the rename must put identity files back.
        if setup_ok:
            _discard_identity_backups(baks)
        else:
            for bak in baks:
                _restore_identity_backup(bak)

    bot_npub = ((setup_data or {}).get("npub") or "").strip()
    status = (setup_data or {}).get("status") or ""
    if not bot_npub:
        io.print_error("Bridge returned incomplete data (no npub).")
        return

    existing_allowed = io.get_env_value("VECTOR_ALLOWED_USERS") or ""
    io.save_env_value("VECTOR_NPUB", bot_npub)
    io.save_env_value("VECTOR_HOME_CHANNEL", operator_npub)
    io.save_env_value(
        "VECTOR_ALLOWED_USERS", _merge_allowed_users(operator_npub, existing_allowed)
    )
    if pending_avatar:
        try:
            install_bot_image(pending_avatar, data_dir, "avatar")
        except ValueError as e:
            io.print_error(f"Avatar not installed: {e}")
    if pending_banner:
        try:
            install_bot_image(pending_banner, data_dir, "banner")
        except ValueError as e:
            io.print_error(f"Banner not installed: {e}")

    if env_nsec:
        io.print_warning(
            "VECTOR_NSEC is still in .env. Delete it — the sidecar never reads it."
        )
    if env_mnemonic:
        io.print_warning(
            "VECTOR_MNEMONIC is still in .env. Delete it after you have a backup."
        )

    _maybe_merge_display(
        io,
        platform=_build_setup_vector_yaml(
            bot_name=bot_name,
            bot_about=bot_about,
            pairing_on=pairing_on,
            group_users=group_users,
            open_chats=open_chats,
            create_home=create_home,
            community_name=community_name,
        ),
    )

    if status == "created":
        io.print_success(f"Account created! Bot npub: {bot_npub}")
    elif status == "restored":
        io.print_success(f"Account restored! Bot npub: {bot_npub}")
    else:
        io.print_success(f"Existing account found! Bot npub: {bot_npub}")
    io.print_info("Share this npub with contacts.")
    io.print_info(
        "Communities: invite the bot from a trusted npub; it auto-joins and "
        "listens. @mention or reply to take a turn. Group-only npubs and "
        "open channel ids live under vector.communities in config.yaml."
    )
    if create_home:
        io.print_info(
            "Home community create is on in config.yaml — the gateway will "
            "create or reuse a private home community after Ready (no public "
            "invite link)."
        )
    backup_bits = [str(data_dir / "identity.nsec")]
    mnemonic_path = data_dir / "identity.mnemonic"
    if mnemonic_path.is_file():
        backup_bits.append(str(mnemonic_path))
    io.print_info(
        "Back up "
        + " and ".join(backup_bits)
        + " offline — replacing them is a new bot."
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
            "Run: hermes plugins enable vector-platform "
            "&& hermes gateway setup. "
            "Setup downloads vector-bridge (Linux/macOS) or cargo-builds it. "
            "hermes gateway start does not download or compile."
        ),
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="VECTOR_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        parse_target_ref_fn=_parse_target_ref,
        allowed_users_env="VECTOR_ALLOWED_USERS",
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
