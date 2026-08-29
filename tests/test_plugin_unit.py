"""Unit tests for hermes-vector-platform (no core Platform.VECTOR required)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _hermes_root() -> Path:
    if override := os.environ.get("HERMES_AGENT_ROOT"):
        return Path(override)
    return Path.home() / ".hermes" / "hermes-agent"


HERMES_ROOT = _hermes_root()
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))


def _load_adapter():
    """Load adapter.py as a free module (avoids package relative-import issues)."""
    path = PLUGIN_ROOT / "adapter.py"
    spec = importlib.util.spec_from_file_location("vector_platform_adapter", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["vector_platform_adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


vector_adapter = _load_adapter()

# fiatjaf's well-known pubkey (32-byte payload, valid bech32 checksum)
HEX_PUBKEY = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
NPUB = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"


class TestPluginVersion:
    def test_plugin_version_set(self):
        assert vector_adapter.PLUGIN_VERSION
        assert vector_adapter.PLUGIN_VERSION[0].isdigit()


class TestNormalizeNpub:
    def test_hex(self):
        assert vector_adapter.normalize_npub(HEX_PUBKEY) == NPUB

    def test_hex_uppercase(self):
        assert vector_adapter.normalize_npub(HEX_PUBKEY.upper()) == NPUB

    def test_npub1(self):
        assert vector_adapter.normalize_npub(NPUB) == NPUB

    def test_npub1_mixed_case(self):
        mixed = NPUB[:8].upper() + NPUB[8:]
        assert vector_adapter.normalize_npub(mixed) == NPUB

    def test_nostr_npub1(self):
        assert vector_adapter.normalize_npub(f"nostr:{NPUB}") == NPUB

    def test_nostr_uppercase_prefix(self):
        assert vector_adapter.normalize_npub(f"NOSTR:{NPUB}") == NPUB

    def test_whitespace_hex(self):
        assert vector_adapter.normalize_npub(f"  {HEX_PUBKEY}  \n") == NPUB

    def test_whitespace_npub(self):
        assert vector_adapter.normalize_npub(f"\t {NPUB} ") == NPUB

    def test_whitespace_nostr_npub(self):
        assert vector_adapter.normalize_npub(f"  nostr: {NPUB}  ") == NPUB

    def test_illegal_charset_b(self):
        # 'b' is not in bech32 charset qpzry9x8gf2tvdw0s3jn54khce6mua7l
        assert vector_adapter.normalize_npub(
            "npub1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ) is None

    def test_illegal_charset_i_o(self):
        assert vector_adapter.normalize_npub(
            "npub1ioioioioioioioioioioioioioioioioioioioioioioioioioio"
        ) is None

    def test_empty(self):
        assert vector_adapter.normalize_npub("") is None
        assert vector_adapter.normalize_npub("   ") is None

    def test_garbage(self):
        assert vector_adapter.normalize_npub("not-an-npub") is None

    def test_short_hex(self):
        assert vector_adapter.normalize_npub(HEX_PUBKEY[:62]) is None

    def test_none_like(self):
        assert vector_adapter.normalize_npub(None) is None  # type: ignore[arg-type]


class TestParseNpubTarget:
    def test_hex_returns_tuple_npub_none(self):
        result = vector_adapter._parse_npub_target(HEX_PUBKEY)
        assert result == (NPUB, None)
        assert isinstance(result, tuple)
        assert result[1] is None

    def test_npub_returns_tuple_npub_none(self):
        result = vector_adapter._parse_npub_target(NPUB)
        assert result == (NPUB, None)

    def test_nostr_prefix_returns_tuple(self):
        result = vector_adapter._parse_npub_target(f"nostr:{NPUB}")
        assert result == (NPUB, None)

    def test_invalid_returns_none(self):
        assert vector_adapter._parse_npub_target("garbage") is None
        assert vector_adapter._parse_npub_target("") is None
        assert vector_adapter._parse_npub_target(
            "npub1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ) is None

    def test_never_bare_string(self):
        samples = [
            HEX_PUBKEY,
            NPUB,
            f"nostr:{NPUB}",
            f"  {NPUB}  ",
            "",
            "garbage",
            "npub1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "05deadbeef",
        ]
        for ref in samples:
            result = vector_adapter._parse_npub_target(ref)
            assert not isinstance(result, str), f"{ref!r} returned a bare string"
            assert result is None or (
                isinstance(result, tuple)
                and len(result) == 2
                and isinstance(result[0], str)
                and (result[1] is None or isinstance(result[1], str))
            )


class TestEnvEnablement:
    def test_none_without_npub(self, monkeypatch):
        monkeypatch.delenv("VECTOR_NPUB", raising=False)
        assert vector_adapter._env_enablement() is None

    def test_seeds_npub(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        monkeypatch.delenv("VECTOR_HOME_CHANNEL", raising=False)
        monkeypatch.delenv("VECTOR_BOT_NAME", raising=False)
        monkeypatch.delenv("VECTOR_BRIDGE_HOST", raising=False)
        seed = vector_adapter._env_enablement()
        assert seed is not None
        assert seed["npub"] == NPUB
        assert "bridge_port" in seed
        assert seed["bridge_host"] == "127.0.0.1"
        assert "data_dir" in seed
        assert "home_channel" not in seed

    def test_home_channel_seeded(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        monkeypatch.setenv("VECTOR_HOME_CHANNEL", HEX_PUBKEY)
        monkeypatch.setenv("VECTOR_HOME_CHANNEL_NAME", "Me")
        seed = vector_adapter._env_enablement()
        assert seed["home_channel"]["chat_id"] == NPUB
        assert seed["home_channel"]["name"] == "Me"


class TestValidateConfig:
    def test_true_with_env(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        cfg = MagicMock()
        cfg.extra = {}
        assert vector_adapter.validate_config(cfg) is True

    def test_true_with_extra(self, monkeypatch):
        monkeypatch.delenv("VECTOR_NPUB", raising=False)
        cfg = MagicMock()
        cfg.extra = {"npub": NPUB}
        assert vector_adapter.validate_config(cfg) is True

    def test_false_without(self, monkeypatch):
        monkeypatch.delenv("VECTOR_NPUB", raising=False)
        cfg = MagicMock()
        cfg.extra = {}
        assert vector_adapter.validate_config(cfg) is False


class TestRequirements:
    def test_false_without_npub(self, monkeypatch):
        monkeypatch.delenv("VECTOR_NPUB", raising=False)
        assert vector_adapter.check_requirements() is False

    def test_false_without_binary(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        missing = tmp_path / "no-such-vector-bridge"
        monkeypatch.setenv("VECTOR_BRIDGE_BIN", str(missing))
        assert vector_adapter.check_requirements() is False

    def test_true_with_npub_and_binary(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        fake = tmp_path / "vector-bridge"
        fake.write_text("")
        monkeypatch.setenv("VECTOR_BRIDGE_BIN", str(fake))
        assert vector_adapter.check_requirements() is True


class TestRegister:
    def test_register_calls_ctx(self):
        ctx = MagicMock()
        vector_adapter.register(ctx)
        ctx.register_redaction_patterns.assert_called_once()
        patterns = ctx.register_redaction_patterns.call_args.args[0]
        assert any("nsec1" in p for p in patterns)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "vector"
        assert kwargs["label"] == "Vector"
        assert kwargs["cron_deliver_env_var"] == "VECTOR_HOME_CHANNEL"
        assert kwargs["allowed_users_env"] == "VECTOR_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "VECTOR_ALLOW_ALL_USERS"
        assert kwargs["parse_target_ref_fn"] is vector_adapter._parse_npub_target
        assert kwargs["parse_target_ref_fn"] is not vector_adapter.normalize_npub
        assert kwargs["check_fn"] is vector_adapter.check_requirements
        assert kwargs["validate_config"] is vector_adapter.validate_config
        assert kwargs["env_enablement_fn"] is vector_adapter._env_enablement
        assert kwargs["standalone_sender_fn"] is vector_adapter._standalone_send
        assert kwargs["setup_fn"] is vector_adapter.interactive_setup
        assert kwargs.get("ensure_deps_fn") is None
        assert kwargs["max_message_length"] == 4000
        assert "markdown" in kwargs["platform_hint"].lower()
        sample = kwargs["parse_target_ref_fn"](HEX_PUBKEY)
        assert sample == (NPUB, None)
        assert not isinstance(sample, str)


# ---------------------------------------------------------------------------
# Adapter lifecycle + DM path (mocked HTTP sidecar — no live Vector network)
# ---------------------------------------------------------------------------

import asyncio
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx


PEER_HEX = "32e1827635450cd0e04c1e6cebee8ba3c9d9da2a59088d32a7c0bb77c9c66570"
PEER_NPUB = vector_adapter.hex_to_npub(PEER_HEX)
assert PEER_NPUB


class FakeBridgeProc:
    """Stand-in for vector-bridge so tests never spawn Rust.

    Default pid is -1 so disconnect() cannot killpg a live process.
    """

    def __init__(self, pid: int = -1):
        self.pid = pid
        self.returncode = None
        self.stdin = MagicMock()
        self._alive = True

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self._alive = False
        self.returncode = 0

    def kill(self):
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        if not self._alive:
            return self.returncode
        if timeout is not None:
            raise subprocess.TimeoutExpired("vector-bridge", timeout)
        self._alive = False
        self.returncode = 0
        return self.returncode


class MockSidecar:
    """Loopback HTTP sidecar: token auth, /health, /send, /typing, /events."""

    def __init__(self, token: str, npub: str = NPUB, ready: bool = True):
        self.token = token
        self.npub = npub
        self.ready = ready
        self.sends: list = []
        self.typing: list = []
        self.health_headers: list = []
        self.send_headers: list = []
        self.typing_headers: list = []
        self.events_headers: list = []
        self.inject_queue: list = []
        self.send_raw: bytes | None = None
        self.port: int | None = None
        self._httpd = None

    def start(self) -> int:
        sidecar = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _auth(self) -> bool:
                got = self.headers.get(vector_adapter.SIDECAR_TOKEN_HEADER)
                if got != sidecar.token:
                    return self._json(
                        401, {"error": "unauthorized", "code": "unauthorized"}
                    ) or False
                return True

            def _json(self, status: int, obj: dict):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/live":
                    return self._json(200, {"ok": True})
                if not self._auth():
                    return
                if path == "/health":
                    sidecar.health_headers.append(dict(self.headers))
                    if sidecar.ready:
                        return self._json(
                            200, {"status": "ready", "npub": sidecar.npub}
                        )
                    return self._json(200, {"status": "starting"})
                if path == "/events":
                    sidecar.events_headers.append(dict(self.headers))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        for evt in list(sidecar.inject_queue):
                            payload = json.dumps(evt).encode()
                            self.wfile.write(b"data: " + payload + b"\n\n")
                            self.wfile.flush()
                        while True:
                            time.sleep(0.2)
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        return
                    return
                return self._json(404, {"error": "not found", "code": "not_found"})

            def do_POST(self):
                if not self._auth():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                data = json.loads(raw.decode() or "{}")
                path = self.path.split("?", 1)[0]
                if path == "/send":
                    sidecar.sends.append(data)
                    sidecar.send_headers.append(dict(self.headers))
                    if sidecar.send_raw is not None:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(sidecar.send_raw)))
                        self.end_headers()
                        self.wfile.write(sidecar.send_raw)
                        return
                    return self._json(200, {"id": "evt-outbound-1"})
                if path == "/typing":
                    sidecar.typing.append(data)
                    sidecar.typing_headers.append(dict(self.headers))
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "not found", "code": "not_found"})

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = httpd.server_address[1]
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _patch_platform(monkeypatch) -> MagicMock:
    """Avoid Platform('vector') hitting the registry (_missing_ rejects unknown)."""
    mock_plat = MagicMock()
    mock_plat.value = "vector"
    monkeypatch.setattr(vector_adapter, "Platform", lambda *_a, **_k: mock_plat)
    return mock_plat


def _make_adapter(monkeypatch, tmp_path, **extra):
    _patch_platform(monkeypatch)
    monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        vector_adapter.BasePlatformAdapter,
        "_acquire_platform_lock",
        lambda self, **_k: True,
    )
    monkeypatch.setattr(
        vector_adapter.BasePlatformAdapter,
        "_release_platform_lock",
        lambda self: None,
    )
    monkeypatch.setattr(
        vector_adapter.BasePlatformAdapter,
        "_write_runtime_status_safe",
        lambda self, *_a, **_k: None,
    )
    monkeypatch.setattr(vector_adapter, "BRIDGE_TERM_WAIT", 0)

    fake_bin = tmp_path / "vector-bridge"
    if not fake_bin.exists():
        fake_bin.write_text("")
    monkeypatch.setattr(vector_adapter, "resolve_bridge_bin", lambda: fake_bin)
    monkeypatch.setattr(
        vector_adapter, "bridge_port_is_listening", lambda *_a, **_k: False
    )

    data_dir = Path(extra.get("data_dir") or (tmp_path / "sdk"))
    data_dir.mkdir(parents=True, exist_ok=True)
    if not extra.get("skip_identity"):
        nsec = data_dir / "identity.nsec"
        if not nsec.exists():
            nsec.write_text("nsec1test")

    cfg = MagicMock()
    cfg.extra = {
        "npub": extra.get("npub", NPUB),
        "bridge_port": extra.get("bridge_port", 18096),
        "bridge_host": extra.get("bridge_host", "127.0.0.1"),
        "bot_name": extra.get("bot_name", "Hermes"),
        "startup_timeout": extra.get("startup_timeout", 5),
        "data_dir": str(data_dir),
    }
    return vector_adapter.VectorAdapter(cfg)


def _message_event(peer: str, text: str, *, msg_id: str = "id1", **overrides) -> dict:
    data = {
        "id": msg_id,
        "chat_id": peer,
        "npub": peer,
        "is_group": False,
        "is_mine": False,
        "is_file": False,
        "text": text,
        "reply_to": "",
        "reply_to_text": None,
        "at_ms": 1,
    }
    data.update(overrides)
    return {"type": "message", "data": data}


class TestPortHelper:
    def test_unused_high_port_not_listening(self):
        assert vector_adapter.bridge_port_is_listening(61999) is False

    def test_listening_true_for_bound_port(self):
        sidecar = MockSidecar(token="x")
        port = sidecar.start()
        try:
            assert vector_adapter.bridge_port_is_listening(port, host="127.0.0.1") is True
        finally:
            sidecar.stop()


class TestGetChatInfo:
    def test_truncated_npub_dm(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        info = asyncio.run(adapter.get_chat_info(NPUB))
        assert info["type"] == "dm"
        assert info["chat_id"] == NPUB
        assert info["name"] == f"{NPUB[:16]}..."
        assert len(info["name"]) < len(NPUB)


class TestInboundMapping:
    def test_chat_id_user_id_are_peer_npub(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, npub=NPUB)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(adapter._handle_message_event(_message_event(PEER_NPUB, "hi", msg_id="m1")))
        assert len(captured) == 1
        src = captured[0].source
        assert src.chat_id == PEER_NPUB
        assert src.user_id == PEER_NPUB
        assert src.chat_type == "dm"
        assert captured[0].text == "hi"
        assert captured[0].message_id == "m1"

    def test_hex_peer_normalized_to_npub(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(adapter._handle_message_event(_message_event(PEER_HEX, "yo", msg_id="m2")))
        assert captured[0].source.chat_id == PEER_NPUB
        assert captured[0].source.user_id == PEER_NPUB

    def test_skip_is_mine(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "echo", msg_id="mine1", is_mine=True)
            )
        )
        assert captured == []

    def test_skip_is_group(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "group", msg_id="g1", is_group=True)
            )
        )
        assert captured == []

    def test_skip_own_npub(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, npub=NPUB)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(adapter._handle_message_event(_message_event(NPUB, "self", msg_id="self1")))
        assert captured == []


class TestInboundDedup:
    def test_same_id_dispatched_once(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        evt = _message_event(PEER_NPUB, "once", msg_id="dup-1")
        asyncio.run(adapter._handle_message_event(evt))
        asyncio.run(adapter._handle_message_event(evt))
        assert len(captured) == 1

    def test_lru_evicts_oldest(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        monkeypatch.setattr(vector_adapter, "INBOUND_DEDUP_MAX", 2)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]

        async def run():
            await adapter._handle_message_event(
                _message_event(PEER_NPUB, "a", msg_id="a")
            )
            await adapter._handle_message_event(
                _message_event(PEER_NPUB, "b", msg_id="b")
            )
            await adapter._handle_message_event(
                _message_event(PEER_NPUB, "c", msg_id="c")
            )
            # "a" should have been evicted
            await adapter._handle_message_event(
                _message_event(PEER_NPUB, "a-again", msg_id="a")
            )

        asyncio.run(run())
        assert [e.message_id for e in captured] == ["a", "b", "c", "a"]


class TestSpawnEnv:
    def test_spawn_sets_token_stdin_pipe_and_strips_secrets(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._sidecar_token = "tok" + "ab" * 30
        monkeypatch.setenv("VECTOR_NSEC", "nsec1shouldneverleak")
        monkeypatch.setenv("VECTOR_MNEMONIC", "abandon abandon")
        monkeypatch.setenv("VECTOR_STUB", "1")
        captured: dict = {}

        def fake_popen(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeBridgeProc()

        monkeypatch.setattr(vector_adapter.subprocess, "Popen", fake_popen)
        proc = adapter._spawn_bridge()
        assert proc.pid == -1
        kwargs = captured["kwargs"]
        assert kwargs["stdin"] == subprocess.PIPE
        assert kwargs["stdout"] is not subprocess.PIPE
        assert kwargs["stderr"] is not subprocess.PIPE
        env = kwargs["env"]
        assert env["VECTOR_SIDECAR_TOKEN"] == adapter._sidecar_token
        assert env["VECTOR_SIDECAR_WATCH_STDIN"] == "1"
        assert env["VECTOR_BRIDGE_PORT"] == str(adapter.bridge_port)
        assert env["VECTOR_BOT_NAME"] == "Hermes"
        assert "VECTOR_NSEC" not in env
        assert "VECTOR_MNEMONIC" not in env
        assert "VECTOR_STUB" not in env
        if sys.platform != "win32":
            assert kwargs.get("start_new_session") is True
            assert "preexec_fn" not in kwargs
        adapter._close_bridge_log()


class TestConnectMissingBinary:
    def test_missing_binary_is_fatal_not_retryable(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        missing = tmp_path / "no-such-vector-bridge"
        monkeypatch.setattr(vector_adapter, "resolve_bridge_bin", lambda: missing)
        spawned = []
        monkeypatch.setattr(
            vector_adapter.VectorAdapter,
            "_spawn_bridge",
            lambda self: spawned.append(True) or FakeBridgeProc(),
        )
        ok = asyncio.run(adapter.connect())
        assert ok is False
        assert spawned == []
        assert adapter.has_fatal_error
        assert adapter.fatal_error_retryable is False
        assert adapter.fatal_error_code == "vector_bridge_missing"

    def test_missing_identity_is_fatal_not_retryable(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, skip_identity=True)
        spawned = []
        monkeypatch.setattr(
            vector_adapter.VectorAdapter,
            "_spawn_bridge",
            lambda self: spawned.append(True) or FakeBridgeProc(),
        )
        ok = asyncio.run(adapter.connect())
        assert ok is False
        assert spawned == []
        assert adapter.has_fatal_error
        assert adapter.fatal_error_retryable is False
        assert adapter.fatal_error_code == "vector_identity_missing"

    def test_spawn_oserror_is_fatal_not_retryable(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)

        def boom(*_a, **_k):
            raise PermissionError("not executable")

        monkeypatch.setattr(vector_adapter.subprocess, "Popen", boom)
        ok = asyncio.run(adapter.connect())
        assert ok is False
        assert adapter.has_fatal_error
        assert adapter.fatal_error_retryable is False
        assert adapter.fatal_error_code == "vector_bridge_spawn_failed"


class TestMockedSidecarHttp:
    def test_send_and_typing_include_token_header(self, monkeypatch, tmp_path):
        token = "a" * 64
        sidecar = MockSidecar(token=token)
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = token
            adapter._running = True

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    result = await adapter.send(PEER_NPUB, "hello", reply_to="parent-id")
                    assert result.success
                    assert result.message_id == "evt-outbound-1"
                    await adapter.send_typing(PEER_NPUB)
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.sends == [
                {"to": PEER_NPUB, "body": "hello", "reply_to": "parent-id"}
            ]
            assert sidecar.send_headers[0].get("X-Hermes-Sidecar-Token") == token
            assert sidecar.typing == [{"to": PEER_NPUB}]
            assert sidecar.typing_headers[0].get("X-Hermes-Sidecar-Token") == token
        finally:
            sidecar.stop()

    def test_send_rejects_wrong_token(self, monkeypatch, tmp_path):
        sidecar = MockSidecar(token="correct-token")
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = "wrong-token"
            adapter._running = True

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    result = await adapter.send(PEER_NPUB, "nope")
                    assert result.success is False
                    assert "401" in (result.error or "")
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.sends == []
        finally:
            sidecar.stop()

    def test_send_200_invalid_json_is_not_retryable(self, monkeypatch, tmp_path):
        token = "d" * 64
        sidecar = MockSidecar(token=token)
        sidecar.send_raw = b"not-json"
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = token
            adapter._running = True

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    result = await adapter.send(PEER_NPUB, "maybe-delivered")
                    assert result.success is True
                    assert result.message_id is None
                    assert result.retryable is False
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.sends == [{"to": PEER_NPUB, "body": "maybe-delivered"}]
        finally:
            sidecar.stop()

    def test_send_requires_running(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._http_client = MagicMock()
        adapter._running = False
        result = asyncio.run(adapter.send(PEER_NPUB, "nope"))
        assert result.success is False
        assert result.error == "Not connected"
        adapter._http_client.post.assert_not_called()

    def test_connect_polls_health_and_sends_token(self, monkeypatch, tmp_path):
        token = "b" * 64
        sidecar = MockSidecar(token=token, npub=NPUB)
        port = sidecar.start()
        try:
            adapter = _make_adapter(
                monkeypatch, tmp_path, bridge_port=port, startup_timeout=5
            )
            monkeypatch.setattr(
                vector_adapter.secrets, "token_hex", lambda n: token
            )

            async def idle_sse(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise

            async def idle_health(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise

            monkeypatch.setattr(vector_adapter.VectorAdapter, "_sse_listener", idle_sse)
            monkeypatch.setattr(
                vector_adapter.VectorAdapter, "_health_monitor", idle_health
            )
            monkeypatch.setattr(
                vector_adapter.VectorAdapter,
                "_spawn_bridge",
                lambda self: FakeBridgeProc(),
            )

            async def go():
                ok = await adapter.connect()
                assert ok is True
                assert adapter.is_connected
                result = await adapter.send(PEER_NPUB, "after-connect")
                assert result.success
                record_path = tmp_path / "runtime" / "vector-sidecar.json"
                record = json.loads(record_path.read_text())
                assert record["token"] == token
                assert record["port"] == port
                assert record["npub"] == NPUB
                await adapter.disconnect()
                assert not record_path.exists()

            asyncio.run(go())
            assert sidecar.health_headers
            assert sidecar.health_headers[0].get("X-Hermes-Sidecar-Token") == token
            assert sidecar.sends == [{"to": PEER_NPUB, "body": "after-connect"}]
        finally:
            sidecar.stop()

    def test_sse_inbound_reaches_handle_message(self, monkeypatch, tmp_path):
        token = "c" * 64
        sidecar = MockSidecar(token=token, npub=NPUB)
        sidecar.inject_queue.append(_message_event(PEER_NPUB, "from-sse", msg_id="sse-1"))
        port = sidecar.start()
        try:
            adapter = _make_adapter(
                monkeypatch, tmp_path, bridge_port=port, startup_timeout=5
            )
            monkeypatch.setattr(
                vector_adapter.secrets, "token_hex", lambda n: token
            )
            captured = []

            async def capture(event):
                captured.append(event)

            adapter.handle_message = capture  # type: ignore[method-assign]

            async def idle_health(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise

            monkeypatch.setattr(
                vector_adapter.VectorAdapter, "_health_monitor", idle_health
            )
            monkeypatch.setattr(
                vector_adapter.VectorAdapter,
                "_spawn_bridge",
                lambda self: FakeBridgeProc(),
            )

            async def go():
                ok = await adapter.connect()
                assert ok is True
                for _ in range(50):
                    if captured:
                        break
                    await asyncio.sleep(0.05)
                await adapter.disconnect()

            asyncio.run(go())
            assert sidecar.events_headers
            assert sidecar.events_headers[0].get("X-Hermes-Sidecar-Token") == token
            assert len(captured) == 1
            assert captured[0].text == "from-sse"
            assert captured[0].source.chat_id == PEER_NPUB
            assert captured[0].source.user_id == PEER_NPUB
            assert captured[0].source.chat_type == "dm"
        finally:
            sidecar.stop()

    def test_connect_does_not_set_vector_stub(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._sidecar_token = "t" * 64
        captured: dict = {}

        def fake_popen(*args, **kwargs):
            captured["env"] = kwargs["env"]
            return FakeBridgeProc()

        monkeypatch.setattr(vector_adapter.subprocess, "Popen", fake_popen)
        monkeypatch.setenv("VECTOR_STUB", "1")
        adapter._spawn_bridge()
        assert "VECTOR_STUB" not in captured["env"]
        adapter._close_bridge_log()


class TestFatalBridgeExit:
    def test_handle_bridge_exit_is_retryable(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._bridge_process = FakeBridgeProc()
        adapter._bridge_process.terminate()
        asyncio.run(adapter._handle_bridge_exit())
        assert adapter.has_fatal_error
        assert adapter.fatal_error_retryable is True
        assert adapter.fatal_error_code == "vector_bridge_exited"


class TestOrphanReap:
    def test_pid_is_vector_bridge(self, monkeypatch):
        def fake_run(*_a, **_k):
            r = MagicMock()
            r.stdout = "/opt/plugins/vector-platform/bridge/target/release/vector-bridge"
            return r

        monkeypatch.setattr(vector_adapter.subprocess, "run", fake_run)
        assert vector_adapter._pid_is_vector_bridge(99) is True

    def test_pid_is_not_vector_bridge(self, monkeypatch):
        def fake_run(*_a, **_k):
            r = MagicMock()
            r.stdout = "nginx: worker process"
            return r

        monkeypatch.setattr(vector_adapter.subprocess, "run", fake_run)
        assert vector_adapter._pid_is_vector_bridge(99) is False

    def test_reap_skips_foreign_listener(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        monkeypatch.setattr(
            vector_adapter, "_find_listener_pids", lambda _port: [9999]
        )
        monkeypatch.setattr(
            vector_adapter, "_pid_is_vector_bridge", lambda _pid: False
        )
        killed = []
        monkeypatch.setattr(vector_adapter.os, "kill", lambda pid, sig: killed.append((pid, sig)))
        asyncio.run(adapter._reap_orphan_sidecar())
        assert killed == []
