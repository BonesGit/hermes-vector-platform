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
