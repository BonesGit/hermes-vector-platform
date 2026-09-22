"""Find, download, and probe the vector-bridge sidecar binary."""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import httpx

from .constants import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_RELEASE_REPO,
    PLUGIN_VERSION,
    PREBUILT_DOWNLOAD_TIMEOUT,
    PREBUILT_MAX_BYTES,
    _RELEASE_REPO_RE,
    _RELEASE_TAG_RE,
)
from .env import _scoped_env_str
from .paths import _PLUGIN_ROOT, _hermes_home
from .yaml_config import _read_vector_yaml_block, _yaml_on_off

logger = logging.getLogger("hermes_plugins.vector_platform.adapter")

_BRIDGE_DIR = _PLUGIN_ROOT / "bridge"
_DEFAULT_BRIDGE_BIN = _BRIDGE_DIR / "target" / "release" / "vector-bridge"


def resolve_bridge_bin(*, require_current: bool = False) -> Path:
    """Sidecar path: override, in-tree release build, then installed prebuilt.

    Runtime (``require_current=False``) uses a prebuilt even when ``.version``
    lags the plugin, so a Python-only ``hermes plugins update`` does not
    disable Vector. Setup passes ``require_current=True`` and re-downloads
    when the stamp does not match ``v{plugin version}``.
    """
    override = _scoped_env_str("VECTOR_BRIDGE_BIN").strip()
    if override:
        return Path(override)
    if _DEFAULT_BRIDGE_BIN.is_file():
        return _DEFAULT_BRIDGE_BIN
    prebuilt = _prebuilt_bridge_bin()
    if prebuilt.is_file() and (
        not require_current or _prebuilt_version_matches()
    ):
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

def bridge_port_is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    """Return True if something already accepts TCP connections on host:port."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False

def _client_host(bind_host: str) -> str:
    """HTTP client host for a sidecar bind address (bind-all → loopback)."""
    host = (bind_host or DEFAULT_BRIDGE_HOST).strip()
    if host in ("0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return host

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

