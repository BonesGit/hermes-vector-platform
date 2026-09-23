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
(port/host/bin/data dir) stays env overrides with defaults. Legacy VECTOR_*
env for those keys still wins. Every VECTOR_* read goes through the active
profile secret scope (``get_scoped_secret``), so a multiplexed secondary
profile does not inherit the default profile's ``os.environ``. The nsec
stays in ``identity.nsec`` under that profile's data dir — it is never
read from the environment at runtime.
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

def _load_internal(name: str):
    """Import ``internal.<name>`` as a package sibling or a top-level package."""
    import importlib

    pkg = __package__ or ""
    if pkg:
        try:
            return importlib.import_module(f"{pkg}.internal.{name}")
        except ImportError:
            pass
    return importlib.import_module(f"internal.{name}")


def _reexport(module, names: tuple) -> None:
    g = globals()
    for item in names:
        g[item] = getattr(module, item)


_INTERNAL_EXPORTS = {
    "bech32": (
        '_BECH32_CHARSET',
        '_bech32_polymod',
        '_bech32_hrp_expand',
        '_convertbits',
        'hex_to_npub',
        'npub_to_hex',
        'normalize_npub',
        '_CHANNEL_ID_RE',
        'normalize_channel_id',
    ),
    "constants": (
        'PLUGIN_VERSION',
        'DEFAULT_BRIDGE_PORT',
        'DEFAULT_BRIDGE_HOST',
        'DEFAULT_STARTUP_TIMEOUT',
        'HERMES_CONNECT_TIMEOUT_FLOOR',
        'MAX_MESSAGE_LENGTH',
        'AVATAR_SUFFIXES',
        'MIN_RUSTC',
        'CARGO_BUILD_TIMEOUT',
        'BRIDGE_CHECK_TIMEOUT',
        'BRIDGE_SETUP_TIMEOUT',
        'SIDECAR_TOKEN_HEADER',
        'HEALTH_POLL_INTERVAL',
        'HEALTH_CHECK_INTERVAL',
        'SSE_RETRY_DELAY_INITIAL',
        'SSE_RETRY_DELAY_MAX',
        'SSE_STALE_TIMEOUT',
        'BRIDGE_TERM_WAIT',
        'INBOUND_DEDUP_MAX',
        'LAST_INBOUND_CHATS_MAX',
        'SENT_IDS_MAX',
        'RUNTIME_RECORD_NAME',
        'NOTIFIED_CHANNELS_FILE',
        'WELCOME_SENT_FILE',
        'INBOX_NAME_MAX',
        'DOWNLOAD_TIMEOUT',
        'SEND_FILE_TIMEOUT',
        'DEFAULT_INBOUND_MEDIA_MAX_BYTES',
        'DEFAULT_RELEASE_REPO',
        'PREBUILT_MAX_BYTES',
        'PREBUILT_DOWNLOAD_TIMEOUT',
        '_RELEASE_REPO_RE',
        '_RELEASE_TAG_RE',
    ),
    "env": (
        '_scoped_env',
        '_scoped_env_str',
        '_env_flag',
        '_pairing_enabled',
        '_processing_reactions_enabled',
        '_create_community_enabled',
        '_community_download_all',
        '_group_context_enabled',
        '_env_nonneg_int',
        '_group_context_max',
        '_group_context_max_chars',
        '_group_context_max_age_secs',
    ),
    "paths": (
        'get_hermes_home',
        'logger',
        '_PLUGIN_ROOT',
        '_hermes_home',
        'resolve_data_dir',
        'resolve_files_root',
        'validate_bot_image_src',
        'install_bot_image',
        'validate_bot_avatar_src',
        'discover_bot_image',
        'install_bot_avatar',
        'install_bot_banner',
        '_sanitize_filename',
        '_unique_path',
        '_mime_for_attachment',
        '_message_type_for_mime',
        '_inbound_media_max_bytes',
        'sandbox_turn_path',
        'sandbox_breadcrumb_path',
        '_runtime_record_path',
        '_write_runtime_record',
        '_read_runtime_record',
        '_delete_runtime_record',
        '_identity_nsec_present',
    ),
    "groups": (
        'logger',
        '_send_target',
        '_pending_inbox_key',
        '_parse_npub_target',
        '_parse_target_ref',
        '_channel_ids_from_csv',
        '_channel_ids_from_env',
        '_sync_group_allowed_chats_extra',
        '_known_channel_ids',
        '_remember_channel',
        '_is_known_channel',
        '_group_allow_all_chats',
        '_VECTOR_SLASH_COMMANDS',
        '_BLOCK_COMMAND_RE',
        '_INVITE_COMMAND_RE',
        '_group_slash_command',
        '_mentions_bot',
        '_mention_remainder',
        '_GROUP_CONTEXT_HEADER',
        '_GROUP_CONTEXT_FETCH_CAP',
        '_flatten_history_text',
        '_safe_history_label',
        '_history_at_ms',
        '_history_id_key',
        '_history_row_before_trigger',
        '_format_history_line',
        '_clip_history_line',
        '_assemble_group_context',
        '_group_file_pending_path',
        '_group_file_pointer_path',
        '_reply_to_bot',
        '_home_operator_npub',
        '_format_joined_notice',
        '_format_pending_invites',
        '_load_notified_channel_ids',
        '_save_notified_channel_ids',
        '_format_operator_welcome',
        '_welcome_already_sent',
        '_save_welcome_sent',
        '_truncate_npub',
        '_profile_display_name',
        '_DEFAULT_CHANNEL_NAMES',
        '_group_chat_name',
        '_npubs_from_env',
        '_allowed_npubs',
        '_group_allowed_users',
        '_sender_is_authorized',
        '_is_superseded_replay',
        '_is_home_operator',
        '_parse_block_command',
        '_parse_invite_command',
        '_group_sender_is_authorized',
        '_merge_allowed_users',
    ),
    "yaml_config": (
        'logger',
        '_config_yaml_path',
        '_read_vector_yaml_block',
        '_yaml_on_off',
        '_VECTOR_DISPLAY_SETTINGS',
        '_YAML11_AMBIGUOUS',
        '_quote_yaml11_str',
        '_merge_vector_display_config',
        '_display_config_is_writable',
        '_ensure_mapping',
        '_apply_vector_display_settings',
        '_apply_vector_platform_settings',
        '_yaml_list_to_csv',
        '_yaml_count',
        '_set_env_if_unset',
        '_build_setup_vector_yaml',
        '_profile_scoped_config_load',
        '_apply_yaml_config',
        '_atomic_write_text',
        '_merge_display_ruamel',
        '_merge_display_pyyaml',
    ),
    "bridge_bin": (
        'logger',
        '_BRIDGE_DIR',
        '_DEFAULT_BRIDGE_BIN',
        'resolve_bridge_bin',
        'bridge_release_target',
        '_prebuilt_bin_dir',
        '_prebuilt_bridge_bin',
        '_prebuilt_version_stamp',
        '_prebuilt_yaml',
        '_release_repo',
        '_release_tag',
        '_skip_prebuilt_download',
        '_prebuilt_version_matches',
        '_github_release_url',
        '_parse_sha256sums',
        '_http_get_bytes',
        '_try_install_prebuilt_bridge',
        'bridge_port_is_listening',
        '_client_host',
        '_find_listener_pids',
        '_pid_is_vector_bridge',
        '_pid_alive',
        '_host_is_loopback',
    ),
    "setup": (
        'logger',
        '_parse_rustc_version',
        '_probe_rustc',
        '_parse_bridge_json',
        '_rewrite_sidecar_profile_env',
        '_overlay_sidecar_extra_env',
        '_bridge_cli_env',
        '_run_bridge_cli',
        '_write_temp_secret',
        '_shred_unlink',
        '_backup_identity_file',
        '_backup_identity_nsec',
        '_backup_identity',
        '_restore_identity_backup',
        '_restore_identity_nsec',
        '_discard_identity_backup',
        '_discard_identity_backups',
        '_identity_nsec_locally_unreadable',
        '_adopt_stale_identity_backup',
        '_normalize_identity_choice',
        '_ensure_bridge_binary',
        '_load_setup_io',
        '_maybe_merge_display',
        '_confirm_import_as_bot',
        '_run_interactive_setup',
        'interactive_setup',
    ),
}
for _mod_name, _mod_names in _INTERNAL_EXPORTS.items():
    _reexport(_load_internal(_mod_name), _mod_names)
del _mod_name, _mod_names

SidecarSession = _load_internal("sidecar").SidecarSession
InboundDispatcher = _load_internal("inbound").InboundDispatcher


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


# Keep in sync with bridge/src/commands.rs HERMES_SLASH_COMMANDS.


# Sidecar clamps Channel::history / history_before to this page size.


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
        self.bridge_port: int = _coerce_port(
            extra.get("bridge_port", _scoped_env("VECTOR_BRIDGE_PORT")),
            DEFAULT_BRIDGE_PORT,
        )
        self.bridge_host: str = str(
            extra.get("bridge_host")
            or _scoped_env("VECTOR_BRIDGE_HOST")
            or DEFAULT_BRIDGE_HOST
        )
        self.bot_name: str = (
            extra.get("bot_name") or _scoped_env("VECTOR_BOT_NAME") or ""
        ).strip()
        self.bot_about: str = (
            extra.get("bot_about") or _scoped_env("VECTOR_BOT_ABOUT") or ""
        ).strip()
        raw_avatar = (
            extra.get("bot_avatar") or _scoped_env("VECTOR_BOT_AVATAR") or ""
        ).strip()
        raw_banner = (
            extra.get("bot_banner") or _scoped_env("VECTOR_BOT_BANNER") or ""
        ).strip()
        self.startup_timeout: int = int(
            _scoped_env("VECTOR_STARTUP_TIMEOUT")
            or extra.get("startup_timeout")
            or DEFAULT_STARTUP_TIMEOUT
        )
        self._npub: Optional[str] = (
            extra.get("npub") or _scoped_env_str("VECTOR_NPUB").strip() or None
        )
        self.data_dir: Path = Path(extra.get("data_dir") or resolve_data_dir())
        self.bot_avatar: Optional[Path] = (
            Path(raw_avatar).expanduser() if raw_avatar else discover_bot_image(self.data_dir, "avatar")
        )
        self.bot_banner: Optional[Path] = (
            Path(raw_banner).expanduser() if raw_banner else discover_bot_image(self.data_dir, "banner")
        )
        self.bridge_url: str = f"http://{_client_host(self.bridge_host)}:{self.bridge_port}"

        self._session = SidecarSession(self)
        self._inbound = InboundDispatcher(self)
        self._sse_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
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
        # Npubs whose local kind-0 lookup missed or failed. History labels
        # stay on the truncated npub instead of asking again every turn.
        self._profile_name_misses: set[str] = set()
        # Sidecar binary predates GET /channels/{id}/history. Warn once.
        self._group_history_route_missing: bool = False
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

    @property
    def _http_client(self):
        return self._session.http_client

    @_http_client.setter
    def _http_client(self, value) -> None:
        self._session.http_client = value

    @property
    def _sidecar_token(self):
        return self._session.token

    @_sidecar_token.setter
    def _sidecar_token(self, value) -> None:
        self._session.token = value

    @property
    def _bridge_process(self):
        return self._session.process

    @_bridge_process.setter
    def _bridge_process(self, value) -> None:
        self._session.process = value

    @property
    def _bridge_log(self):
        return self._session.log_path

    @_bridge_log.setter
    def _bridge_log(self, value) -> None:
        self._session.log_path = value

    @property
    def _bridge_log_fh(self):
        return self._session.log_fh

    @_bridge_log_fh.setter
    def _bridge_log_fh(self, value) -> None:
        self._session.log_fh = value

    def _token_headers(self) -> Dict[str, str]:
        return self._session.token_headers()

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
        if (
            not _scoped_env_str("VECTOR_BRIDGE_BIN").strip()
            and bin_path == _prebuilt_bridge_bin()
            and not _prebuilt_version_matches()
        ):
            logger.warning(
                "Vector: installed sidecar stamp is not %s; "
                "run `hermes gateway setup` to refresh. Using %s for now.",
                _release_tag(),
                bin_path,
            )

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

        self._session.mint_token()
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

            self._session.open_http()

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

            self._session.publish_runtime()

            # Set _running before SSE/health tasks so their loops don't exit immediately.
            self._running = True
            self._sse_task = asyncio.create_task(self._sse_listener())
            self._health_task = asyncio.create_task(self._health_monitor())
            await self._maybe_send_operator_welcome()
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

        await self._session.shutdown_transport()
        self._release_platform_lock()
        self._mark_disconnected()
        logger.info("Vector: disconnected")

    async def _cleanup_failed_connect(self) -> None:
        await self._session.shutdown_transport()
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

    def _warn_history_route_missing(self) -> None:
        if self._group_history_route_missing:
            return
        self._group_history_route_missing = True
        logger.warning(
            "Vector: sidecar has no channel-history route; group context "
            "is off until the sidecar is rebuilt"
        )

    async def _fetch_channel_history(
        self,
        channel_id: str,
        limit: int,
        *,
        before_at_ms: Optional[int] = None,
        before_id: str = "",
    ) -> List[dict]:
        if not self._http_client or not channel_id:
            return []
        if self._group_history_route_missing:
            return []
        params: Dict[str, Any] = {"limit": limit}
        if (
            before_at_ms is not None
            and before_at_ms >= 0
            and _CHANNEL_ID_RE.fullmatch(before_id or "")
        ):
            # Page ending at the trigger. Short test ids stay on the client filter.
            params["before_at_ms"] = before_at_ms
            params["before_id"] = before_id.lower()
        try:
            resp = await self._http_client.get(
                f"{self.bridge_url}/channels/{channel_id}/history",
                params=params,
                headers=self._token_headers(),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("Vector: channel history fetch failed: %s", e)
            return []
        if resp.status_code == 404:
            self._warn_history_route_missing()
            return []
        if resp.status_code != 200:
            logger.debug(
                "Vector: channel history HTTP %s for %s",
                resp.status_code,
                channel_id[:16],
            )
            return []
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return []
        msgs = data.get("messages") if isinstance(data, dict) else None
        return msgs if isinstance(msgs, list) else []

    async def _local_profile_name(self, npub: str) -> str:
        """Kind-0 already in the sidecar. Does not ask relays.

        ``GET /profile`` without ``local`` is ``fetch_profile`` (two relay
        reads, 15s each). History can name a whole window of strangers, so
        this path stays on ``cached_profile``.
        """
        if not npub or not self._http_client:
            return ""
        try:
            resp = await self._http_client.get(
                f"{self.bridge_url}/profile",
                params={"npub": npub, "local": "true"},
                headers=self._token_headers(),
                timeout=5.0,
            )
        except Exception as e:
            logger.debug("Vector: local profile lookup failed: %s", e)
            return ""
        if resp.status_code != 200:
            return ""
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return ""
        return _profile_display_name(data if isinstance(data, dict) else None, npub)

    async def _history_speaker_name(self, npub: str, *, mine: bool) -> str:
        if mine:
            bot = (self.bot_name or "").strip()
            if bot:
                return bot
            if self._npub:
                return self._peer_label(self._npub)
            return "Hermes"
        if not npub:
            return "unknown"
        cached = self._profile_names.get(npub)
        if cached:
            return cached
        fallback = _truncate_npub(npub) or "unknown"
        if npub in self._profile_name_misses:
            return fallback
        label = await self._local_profile_name(npub)
        if label and label != fallback:
            self._profile_names[npub] = label
            return label
        self._profile_name_misses.add(npub)
        return fallback

    async def _group_channel_context(
        self,
        channel_id: str,
        *,
        trigger_id: str,
        trigger_at_ms: Any,
    ) -> Optional[str]:
        max_n = _group_context_max()
        max_chars = _group_context_max_chars()
        max_age = _group_context_max_age_secs()
        trigger_id = str(trigger_id or "")
        try:
            trigger_at = int(trigger_at_ms) if trigger_at_ms is not None else None
        except (TypeError, ValueError):
            trigger_at = None
        use_before = bool(
            trigger_at is not None
            and trigger_at >= 0
            and _CHANNEL_ID_RE.fullmatch(trigger_id)
        )
        if max_n <= 0:
            fetch_n = _GROUP_CONTEXT_FETCH_CAP
        elif use_before:
            fetch_n = min(_GROUP_CONTEXT_FETCH_CAP, max_n)
        else:
            # Room to drop the trigger when the page still includes it.
            fetch_n = min(_GROUP_CONTEXT_FETCH_CAP, max_n + 1)
        raw = await self._fetch_channel_history(
            channel_id,
            fetch_n,
            before_at_ms=trigger_at if use_before else None,
            before_id=trigger_id if use_before else "",
        )
        now_ms = int(time.time() * 1000)
        horizon_ms = None
        if max_age > 0:
            anchor = trigger_at if trigger_at is not None else now_ms
            horizon_ms = anchor - (max_age * 1000)

        rows: List[dict] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            msg_id = str(item.get("id") or "")
            if msg_id and msg_id in seen:
                continue
            at_ms = _history_at_ms(item)
            if not _history_row_before_trigger(at_ms, msg_id, trigger_at, trigger_id):
                continue
            if horizon_ms is not None and at_ms < horizon_ms:
                continue
            if msg_id:
                seen.add(msg_id)
            rows.append(item)
        rows.sort(key=lambda item: (_history_at_ms(item), str(item.get("id") or "")))
        if max_n > 0 and len(rows) > max_n:
            rows = rows[-max_n:]

        lines: List[str] = []
        for item in rows:
            mine = bool(item.get("mine"))
            npub = normalize_npub(str(item.get("npub") or "")) or str(
                item.get("npub") or ""
            )
            is_file = bool(item.get("is_file"))
            name = await self._history_speaker_name(npub, mine=mine)
            unverified = (not mine) and not _group_sender_is_authorized(
                npub, channel_id
            )
            line = _format_history_line(
                name=name,
                text=str(item.get("text") or ""),
                is_file=is_file,
                mine=mine,
                unverified=unverified,
            )
            if line:
                lines.append(line)
        block, injected = _assemble_group_context(lines, max_chars)
        if block:
            logger.debug(
                "Vector: group context channel=%s lines=%s chars=%s",
                channel_id[:16],
                injected,
                len(block),
            )
        return block

    async def _ensure_home_community(self) -> None:
        """Slice 2: create-or-reuse a bot-owned Concord community (no public link)."""
        if not self._http_client:
            return
        name = _scoped_env_str("VECTOR_COMMUNITY_NAME").strip() or "Hermes"
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

    async def _maybe_send_operator_welcome(self) -> None:
        """Once per bot+operator pair, DM VECTOR_HOME_CHANNEL a hello.

        That npub is the account entered during ``hermes gateway setup``. The
        Vector app will not show the bot until someone sends a gift-wrapped
        DM; this outbound hello opens the thread so first-run is not silent.
        Restarts skip it. A failed send leaves the marker unset so the next
        connect retries. Does not start a Hermes turn.
        """
        home = _home_operator_npub()
        bot = normalize_npub(self._npub or "") or (self._npub or "").strip()
        if not home or not bot or home == bot:
            return
        if _welcome_already_sent(self.data_dir, bot, home):
            return
        if not (self._running and self._http_client):
            return
        result = await self.send(home, _format_operator_welcome(bot))
        if not result.success:
            logger.warning(
                "Vector: first-run hello to VECTOR_HOME_CHANNEL failed: %s",
                result.error,
            )
            return
        _save_welcome_sent(self.data_dir, bot, home)
        logger.info(
            "Vector: sent first-run hello to %s",
            _truncate_npub(home),
        )

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

    async def _sse_listener(self, *args, **kwargs):
        return await self._inbound._sse_listener(*args, **kwargs)


    async def _dispatch_sse_event(self, *args, **kwargs):
        return await self._inbound._dispatch_sse_event(*args, **kwargs)


    async def _handle_message_event(self, *args, **kwargs):
        return await self._inbound._handle_message_event(*args, **kwargs)


    async def _handle_group_message(self, *args, **kwargs):
        return await self._inbound._handle_group_message(*args, **kwargs)


    async def _handle_message_delete(self, *args, **kwargs):
        return await self._inbound._handle_message_delete(*args, **kwargs)


    async def _handle_message_update(self, *args, **kwargs):
        return await self._inbound._handle_message_update(*args, **kwargs)


    def _is_duplicate(self, *args, **kwargs):
        return self._inbound._is_duplicate(*args, **kwargs)


    def _queue_pending_inbox(self, *args, **kwargs):
        return self._inbound._queue_pending_inbox(*args, **kwargs)


    def _media_for_event(self, *args, **kwargs):
        return self._inbound._media_for_event(*args, **kwargs)


    def _group_source(self, *args, **kwargs):
        return self._inbound._group_source(*args, **kwargs)


    def _stash_group_file_pending(self, *args, **kwargs):
        return self._inbound._stash_group_file_pending(*args, **kwargs)


    def _load_group_file_pointer(self, *args, **kwargs):
        return self._inbound._load_group_file_pointer(*args, **kwargs)


    def _write_group_file_pointer(self, *args, **kwargs):
        return self._inbound._write_group_file_pointer(*args, **kwargs)


    async def _ingest_group_file_event(self, *args, **kwargs):
        return await self._inbound._ingest_group_file_event(*args, **kwargs)


    async def _save_inbound_attachments(self, *args, **kwargs):
        return await self._inbound._save_inbound_attachments(*args, **kwargs)


    async def _download_attachment(self, *args, **kwargs):
        return await self._inbound._download_attachment(*args, **kwargs)


    def _append_inbox_index(self, *args, **kwargs):
        return self._inbound._append_inbox_index(*args, **kwargs)


    async def _ack_file_only(self, *args, **kwargs):
        return await self._inbound._ack_file_only(*args, **kwargs)


    async def _file_superseded_replay(self, *args, **kwargs):
        return await self._inbound._file_superseded_replay(*args, **kwargs)


    async def _write_inbox_breadcrumb(self, *args, **kwargs):
        return await self._inbound._write_inbox_breadcrumb(*args, **kwargs)


    def _append_session_breadcrumb(self, *args, **kwargs):
        return self._inbound._append_session_breadcrumb(*args, **kwargs)


    # ------------------------------------------------------------------
    # Health monitor / process death
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        await self._session.health_monitor()

    def _spawn_bridge(self) -> subprocess.Popen:
        return self._session.spawn()

    async def _wait_proc(self, proc: subprocess.Popen, timeout: float) -> None:
        await self._session.wait_proc(proc, timeout)

    def _signal_bridge(self, proc: subprocess.Popen, sig) -> None:
        self._session.signal(proc, sig)

    async def _stop_bridge_process(self) -> None:
        await self._session.stop()

    async def _reap_orphan_sidecar(self) -> None:
        await self._session.reap_orphans()

    def _close_bridge_log(self) -> None:
        self._session.close_log()

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

# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Side-effect free: VECTOR_NPUB set and a matching vector-bridge present."""
    npub = _scoped_env_str("VECTOR_NPUB").strip()
    if not npub:
        return False
    return resolve_bridge_bin().is_file()


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    npub = _scoped_env("VECTOR_NPUB") or extra.get("npub") or ""
    return bool(str(npub).strip())


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement():
    """Seed PlatformConfig.extra from the active profile's env before construction."""
    npub = _scoped_env_str("VECTOR_NPUB").strip()
    if not npub:
        return None
    seed = {
        "npub": npub,
        "data_dir": str(resolve_data_dir()),
        "bridge_port": _scoped_env("VECTOR_BRIDGE_PORT") or str(DEFAULT_BRIDGE_PORT),
        "bridge_host": _scoped_env("VECTOR_BRIDGE_HOST") or DEFAULT_BRIDGE_HOST,
        "startup_timeout": _scoped_env("VECTOR_STARTUP_TIMEOUT") or str(DEFAULT_STARTUP_TIMEOUT),
    }
    home = _scoped_env_str("VECTOR_HOME_CHANNEL").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": normalize_npub(home) or home,
            "name": "Home",
        }
    group_users = _scoped_env_str("VECTOR_GROUP_ALLOWED_USERS").strip()
    if group_users:
        seed["group_allowed_users"] = group_users
    open_chats = ",".join(sorted(_group_allow_all_chats()))
    if open_chats:
        seed["group_allowed_chats"] = open_chats
    bot_name = _scoped_env_str("VECTOR_BOT_NAME").strip()
    if bot_name:
        seed["bot_name"] = bot_name
    bot_about = _scoped_env_str("VECTOR_BOT_ABOUT").strip()
    if bot_about:
        seed["bot_about"] = bot_about
    avatar = _scoped_env_str("VECTOR_BOT_AVATAR").strip()
    if avatar:
        seed["bot_avatar"] = avatar
    banner = _scoped_env_str("VECTOR_BOT_BANNER").strip()
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


# D12: POST /edit lets Hermes accumulate tool-progress on one bubble.
# Streaming extras stay off — each token edit is another NIP-17 gift wrap.


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
        extra.get("bridge_port") or _scoped_env("VECTOR_BRIDGE_PORT"),
        DEFAULT_BRIDGE_PORT,
    )
    host = _client_host(
        str(extra.get("bridge_host") or _scoped_env("VECTOR_BRIDGE_HOST") or DEFAULT_BRIDGE_HOST)
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
