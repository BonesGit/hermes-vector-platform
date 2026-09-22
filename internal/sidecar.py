"""Process, token, and HTTP client for the vector-bridge sidecar."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .bridge_bin import (
    _find_listener_pids,
    _pid_alive,
    _pid_is_vector_bridge,
    resolve_bridge_bin,
)
from .constants import (
    BRIDGE_TERM_WAIT,
    HEALTH_CHECK_INTERVAL,
    PLUGIN_VERSION,
    SIDECAR_TOKEN_HEADER,
)
from . import paths
from .paths import _delete_runtime_record, _write_runtime_record
from .setup import _overlay_sidecar_extra_env, _rewrite_sidecar_profile_env

logger = logging.getLogger("hermes_plugins.vector_platform.adapter")


class SidecarSession:
    """One vector-bridge process and the loopback client that talks to it."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.http_client: Optional[httpx.AsyncClient] = None
        self.token: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self.log_path: Optional[Path] = None
        self.log_fh = None

    def token_headers(self) -> Dict[str, str]:
        return {SIDECAR_TOKEN_HEADER: self.token or ""}

    def mint_token(self) -> str:
        self.token = secrets.token_hex(32)
        return self.token

    def open_http(self) -> httpx.AsyncClient:
        self.http_client = httpx.AsyncClient(timeout=30.0, trust_env=False)
        return self.http_client

    async def aclose_http(self) -> None:
        if self.http_client:
            try:
                await self.http_client.aclose()
            except Exception:
                pass
            self.http_client = None

    def publish_runtime(self) -> None:
        adapter = self.adapter
        pid = self.process.pid if self.process else 0
        _write_runtime_record(adapter.bridge_port, self.token or "", pid, adapter._npub)

    def clear_runtime(self) -> None:
        _delete_runtime_record()

    async def shutdown_transport(self) -> None:
        await self.stop()
        self.close_log()
        await self.aclose_http()
        self.clear_runtime()

    async def health_monitor(self) -> None:
        adapter = self.adapter
        while adapter._running:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if not adapter._running:
                break

            if self.process and self.process.poll() is not None:
                await adapter._handle_bridge_exit()
                break

            if not self.http_client:
                continue
            try:
                resp = await self.http_client.get(
                    f"{adapter.bridge_url}/health",
                    headers=self.token_headers(),
                    timeout=5.0,
                )
                if resp.status_code != 200:
                    logger.warning("Vector: /health returned %d", resp.status_code)
            except Exception as e:
                logger.warning("Vector: /health unreachable: %s", e)

    def spawn(self) -> subprocess.Popen:
        """Launch vector-bridge with stdin pipe; logs go to a file (not PIPE)."""
        adapter = self.adapter
        bin_path = resolve_bridge_bin()
        adapter.data_dir.mkdir(parents=True, exist_ok=True)

        try:
            home = paths.get_hermes_home()
        except Exception:
            home = Path.home() / ".hermes"
        logs_dir = Path(home) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = logs_dir / "vector-bridge.log"
        bridge_log_fh = open(self.log_path, "a", encoding="utf-8")
        self.log_fh = bridge_log_fh

        env = dict(os.environ)
        # The child has no secret scope. Under multiplex, os.environ is the
        # default profile — strip those VECTOR_* values and restore this
        # profile's before the instance overlays (port, data dir, token).
        _rewrite_sidecar_profile_env(env)
        env.update(
            {
                "VECTOR_DATA_DIR": str(adapter.data_dir),
                "VECTOR_BRIDGE_PORT": str(adapter.bridge_port),
                "VECTOR_BRIDGE_HOST": adapter.bridge_host,
                "VECTOR_SIDECAR_TOKEN": self.token or "",
                "VECTOR_SIDECAR_WATCH_STDIN": "1",
            }
        )
        env.pop("VECTOR_BOT_NAME", None)
        env.pop("VECTOR_BOT_ABOUT", None)
        env.pop("VECTOR_BOT_AVATAR", None)
        env.pop("VECTOR_BOT_BANNER", None)
        extra = getattr(adapter.config, "extra", None) or {}
        if isinstance(extra, dict):
            _overlay_sidecar_extra_env(env, extra)
        if adapter.bot_name:
            env["VECTOR_BOT_NAME"] = adapter.bot_name
        if adapter.bot_about:
            env["VECTOR_BOT_ABOUT"] = adapter.bot_about
        if adapter.bot_avatar and adapter.bot_avatar.is_file():
            env["VECTOR_BOT_AVATAR"] = str(adapter.bot_avatar.resolve())
        elif adapter.bot_avatar:
            logger.warning(
                "Vector: VECTOR_BOT_AVATAR is not a file (%s); not publishing an avatar",
                adapter.bot_avatar,
            )
        if adapter.bot_banner and adapter.bot_banner.is_file():
            env["VECTOR_BOT_BANNER"] = str(adapter.bot_banner.resolve())
        elif adapter.bot_banner:
            logger.warning(
                "Vector: VECTOR_BOT_BANNER is not a file (%s); not publishing a banner",
                adapter.bot_banner,
            )
        env.pop("VECTOR_NSEC", None)
        env.pop("VECTOR_MNEMONIC", None)
        env.pop("VECTOR_STUB", None)

        logger.info(
            "Vector plugin v%s: spawning %s (port %d, log %s)",
            PLUGIN_VERSION,
            bin_path,
            adapter.bridge_port,
            self.log_path,
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
        self.process = process
        return process

    async def wait_proc(self, proc: subprocess.Popen, timeout: float) -> None:
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

    def signal(self, proc: subprocess.Popen, sig) -> None:
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

    async def stop(self) -> None:
        proc = self.process
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
                self.signal(proc, signal.SIGTERM)
            except Exception:
                pass
            try:
                await self.wait_proc(proc, BRIDGE_TERM_WAIT)
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() is None:
                try:
                    self.signal(proc, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    await self.wait_proc(proc, 1.0)
                except (subprocess.TimeoutExpired, Exception):
                    pass
        except Exception as e:
            logger.warning("Vector: error stopping sidecar: %s", e)
        finally:
            self.process = None

    async def reap_orphans(self) -> None:
        """Kill a previous vector-bridge still listening on our port.

        Only signals PIDs whose command line contains ``vector-bridge``
        (Photon: never kill a reused pid from a stale runtime record).
        """
        adapter = self.adapter
        if sys.platform == "win32":
            return

        def _inspect():
            found = _find_listener_pids(adapter.bridge_port)
            mine = [pid for pid in found if _pid_is_vector_bridge(pid)]
            return mine, [pid for pid in found if pid not in mine]

        stale, _foreign = await asyncio.to_thread(_inspect)
        if not stale:
            return
        for pid in stale:
            logger.warning(
                "Vector: reaping orphan sidecar pid %d on port %d",
                pid,
                adapter.bridge_port,
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

    def close_log(self) -> None:
        if self.log_fh:
            try:
                self.log_fh.close()
            except Exception:
                pass
            self.log_fh = None
