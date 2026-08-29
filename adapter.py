"""Vector platform adapter for Hermes Agent.

Registers the ``vector`` platform, npub helpers, and a ``BasePlatformAdapter``
that owns a Rust ``vector-bridge`` sidecar over loopback HTTP/SSE.

Vector users are identified by a bech32 ``npub1…`` public key. Session mapping
is ``chat_id = user_id = peer npub``, ``chat_type = "dm"``.

Required env vars / config.extra keys:
    VECTOR_NPUB           Bot public key (npub1…)
    VECTOR_ALLOWED_USERS  Comma-separated allowlisted npubs
    VECTOR_HOME_CHANNEL   Operator npub for cron delivery
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
import signal
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
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
_DEFAULT_BRIDGE_BIN = _PLUGIN_ROOT / "bridge" / "target" / "release" / "vector-bridge"

DEFAULT_BRIDGE_PORT = 8096
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_STARTUP_TIMEOUT = 30
MAX_MESSAGE_LENGTH = 4000
DEFAULT_BOT_NAME = "Hermes"

SIDECAR_TOKEN_HEADER = "X-Hermes-Sidecar-Token"
HEALTH_POLL_INTERVAL = 0.5
HEALTH_CHECK_INTERVAL = 30.0
SSE_RETRY_DELAY_INITIAL = 2.0
SSE_RETRY_DELAY_MAX = 60.0
SSE_STALE_TIMEOUT = 60.0
BRIDGE_TERM_WAIT = 2.0
INBOUND_DEDUP_MAX = 1024
RUNTIME_RECORD_NAME = "vector-sidecar.json"

_NOT_WIRED = "Vector sidecar is not wired yet"


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

        try:
            self._bridge_process = self._spawn_bridge()
        except Exception as e:
            logger.error("Vector: failed to spawn sidecar: %s", e, exc_info=True)
            self._close_bridge_log()
            self._release_platform_lock()
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
                await self._cleanup_failed_connect()
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
            await self._cleanup_failed_connect()
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
        logger.info("Vector: connected on %s:%d", self.bridge_host, self.bridge_port)
        return True

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
        if not self._http_client:
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
                data = resp.json()
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
            return SendResult(success=False, error=str(e), retryable=True)

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
            popen_kwargs["preexec_fn"] = os.setsid

        process = subprocess.Popen([str(bin_path)], **popen_kwargs)
        self._bridge_process = process
        return process

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
            pid = proc.pid
            if sys.platform == "win32":
                try:
                    proc.terminate()
                except Exception:
                    pass
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            await asyncio.sleep(BRIDGE_TERM_WAIT)
            if proc.poll() is None:
                if sys.platform == "win32":
                    try:
                        proc.kill()
                    except Exception:
                        pass
                else:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        try:
                            proc.kill()
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("Vector: error stopping sidecar: %s", e)
        finally:
            self._bridge_process = None

    async def _reap_orphan_sidecar(self) -> None:
        """If a previous vector-bridge still holds the port, SIGTERM its pid."""
        record = _read_runtime_record()
        if not record:
            return
        try:
            rec_port = int(record.get("port"))
            rec_pid = int(record.get("pid"))
        except (TypeError, ValueError):
            return
        if rec_port != self.bridge_port or rec_pid <= 0:
            return
        logger.warning(
            "Vector: reaping orphan sidecar pid %d on port %d",
            rec_pid,
            rec_port,
        )
        try:
            os.kill(rec_pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                os.kill(rec_pid, 0)
            except OSError:
                break
            await asyncio.sleep(0.1)
        else:
            try:
                os.kill(rec_pid, signal.SIGKILL)
            except OSError:
                pass
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
    """Out-of-process Vector delivery. Runtime-record sender is PR 5."""
    return {"error": _NOT_WIRED}


def interactive_setup() -> None:
    """Does not mint identity or write env; setup is not wired."""
    print("Vector setup is not wired.")
    print("Enable with: hermes plugins enable vector-platform")
    print("Identity create/import and sidecar build are not available.")


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
