"""Vector platform adapter for Hermes Agent.

Registers the ``vector`` platform, npub helpers, and a ``BasePlatformAdapter``
that owns a Rust ``vector-bridge`` sidecar over loopback HTTP/SSE.

Vector users are identified by a bech32 ``npub1…`` public key. Session mapping
is ``chat_id = user_id = peer npub``, ``chat_type = "dm"``.

Required env vars / config.extra keys:
    VECTOR_NPUB           Bot public key (npub1…)
    VECTOR_ALLOWED_USERS  Comma-separated allowlisted npubs
    VECTOR_HOME_CHANNEL   Operator npub for cron delivery
    VECTOR_PAIRING        on (default) = pairing codes; off = drop unauthorized
    VECTOR_BRIDGE_PORT    HTTP port (default 8096)
    VECTOR_BRIDGE_HOST    Bind address (default 127.0.0.1)
"""

from __future__ import annotations

import asyncio
import json
import logging
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
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx

from gateway.config import Platform, PlatformConfig
from hermes_constants import get_hermes_home
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger("hermes_plugins.vector_platform.adapter")

# ---------------------------------------------------------------------------
# Plugin identity / paths
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.1.0"
_PLUGIN_ROOT = Path(__file__).resolve().parent
_BRIDGE_DIR = _PLUGIN_ROOT / "bridge"
_DEFAULT_BRIDGE_BIN = _BRIDGE_DIR / "target" / "release" / "vector-bridge"

DEFAULT_BRIDGE_PORT = 8096
DEFAULT_BRIDGE_HOST = "127.0.0.1"
# Must stay under GatewayRunner._PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT (30s).
# Operators who raise VECTOR_STARTUP_TIMEOUT must also raise
# HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT above it.
DEFAULT_STARTUP_TIMEOUT = 25
MAX_MESSAGE_LENGTH = 4000
DEFAULT_BOT_NAME = "Hermes"
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
RUNTIME_RECORD_NAME = "vector-sidecar.json"


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


def _parse_npub_target(ref: str) -> Optional[tuple[str, Optional[str]]]:
    """parse_target_ref_fn: (chat_id, thread_id). DMs have no thread."""
    npub = normalize_npub(ref)
    return (npub, None) if npub else None


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


def _allow_all_users() -> bool:
    return _env_flag("VECTOR_ALLOW_ALL_USERS") in ("1", "true", "yes", "on")


def _allowed_npubs() -> set:
    """Canonical npubs from VECTOR_ALLOWED_USERS (comma-separated)."""
    found: set = set()
    raw = os.getenv("VECTOR_ALLOWED_USERS") or ""
    for part in raw.split(","):
        npub = normalize_npub(part.strip())
        if npub:
            found.add(npub)
    return found


def _sender_is_authorized(peer: str) -> bool:
    """Adapter-layer allowlist (VECTOR_ALLOW_ALL_USERS / VECTOR_ALLOWED_USERS)."""
    if _allow_all_users():
        return True
    npub = normalize_npub(peer) or (peer or "").strip()
    if not npub:
        return False
    return npub in _allowed_npubs()


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

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("vector"))

        extra = config.extra or {}
        self.bridge_port: int = int(extra.get("bridge_port", DEFAULT_BRIDGE_PORT))
        self.bridge_host: str = str(
            extra.get("bridge_host")
            or os.getenv("VECTOR_BRIDGE_HOST")
            or DEFAULT_BRIDGE_HOST
        )
        self.bot_name: str = extra.get("bot_name") or os.getenv("VECTOR_BOT_NAME") or DEFAULT_BOT_NAME
        self.startup_timeout: int = int(
            extra.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT)
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
                return SendResult(
                    success=True,
                    message_id=data.get("id") or data.get("messageId"),
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
        npub = normalize_npub(chat_id) or (chat_id or "").strip()
        name = _truncate_npub(npub)
        return {"name": name, "type": "dm", "chat_id": npub}

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return SendResult(success=False, error="not implemented in v1")

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
        return SendResult(success=False, error="not implemented in v1")

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
            return

        msg_id = str(msg_data.get("id") or "")
        if msg_id and self._is_duplicate(msg_id):
            logger.debug("Vector: dropping duplicate inbound id=%s", msg_id[:16])
            return

        text = msg_data.get("text") or ""
        if msg_data.get("is_file") and not str(text).strip():
            logger.debug("Vector: dropping empty file message id=%s", msg_id[:16])
            return
        if not str(text).strip():
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
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id or None,
            reply_to_text=reply_to_text,
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
            "VECTOR_BOT_NAME": self.bot_name,
            "VECTOR_SIDECAR_WATCH_STDIN": "1",
        }
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
        "bot_name": os.getenv("VECTOR_BOT_NAME") or DEFAULT_BOT_NAME,
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

    bot_name = io.prompt(
        "Bot display name",
        default=io.get_env_value("VECTOR_BOT_NAME") or DEFAULT_BOT_NAME,
    ) or DEFAULT_BOT_NAME

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
    io.save_env_value("VECTOR_DATA_DIR", str(data_dir))
    io.save_env_value("VECTOR_HOME_CHANNEL", operator_npub)
    io.save_env_value(
        "VECTOR_ALLOWED_USERS", _merge_allowed_users(operator_npub, existing_allowed)
    )
    io.save_env_value("VECTOR_PAIRING", "on" if pairing_on else "off")

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
        f"Back up {data_dir / 'identity.nsec'} offline — replacing it is a new bot."
    )
    io.print_success("Vector configured!")
    io.print_info("Restart the gateway: hermes gateway restart")


def interactive_setup() -> None:
    """Interactive ``hermes gateway setup`` flow for Vector."""
    _run_interactive_setup(_load_setup_io())


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
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
        parse_target_ref_fn=_parse_npub_target,
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
            "v1 supports 1:1 DMs only — not communities."
        ),
    )
