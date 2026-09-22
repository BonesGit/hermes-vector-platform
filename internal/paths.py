"""Data directories, bot images, inbox files, and the sidecar runtime record."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.platforms.base import MessageType
from hermes_constants import get_hermes_home

from .constants import (
    AVATAR_SUFFIXES,
    DEFAULT_INBOUND_MEDIA_MAX_BYTES,
    INBOX_NAME_MAX,
    RUNTIME_RECORD_NAME,
)
from .env import _scoped_env_str

logger = logging.getLogger("hermes_plugins.vector_platform.adapter")

# Plugin root is the parent of this package (bridge/ sits beside adapter.py).
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _hermes_home() -> Path:
    try:
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"

def resolve_data_dir() -> Path:
    """Default VECTOR_DATA_DIR: plugin-data/vector-platform/sdk."""
    override = _scoped_env_str("VECTOR_DATA_DIR").strip()
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

def _identity_nsec_present(data_dir: Path) -> bool:
    path = Path(data_dir) / "identity.nsec"
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False

