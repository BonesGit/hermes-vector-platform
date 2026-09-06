"""Unit tests for hermes-vector-platform (no core Platform.VECTOR required)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import signal
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

    def test_declared_versions_match(self):
        """plugin.yaml, pyproject, Cargo.toml, and PLUGIN_VERSION stay in lockstep."""
        version = vector_adapter.PLUGIN_VERSION
        plugin_yaml = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        pyproject = (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        cargo = (PLUGIN_ROOT / "bridge" / "Cargo.toml").read_text(encoding="utf-8")
        cargo_lock = (PLUGIN_ROOT / "bridge" / "Cargo.lock").read_text(
            encoding="utf-8"
        )
        assert f"version: {version}" in plugin_yaml
        assert f'version = "{version}"' in pyproject
        assert f'version = "{version}"' in cargo.split("[dependencies]", 1)[0]
        assert f'name = "vector-bridge"\nversion = "{version}"' in cargo_lock


class TestInstallManifest:
    def test_bot_npub_is_not_requires_env(self):
        """VECTOR_NPUB is written by gateway setup, not asked at plugin install."""
        plugin_yaml = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        requires, _, _ = plugin_yaml.partition("optional_env:")
        assert "requires_env:" not in requires
        assert "VECTOR_NPUB" in plugin_yaml
        after = PLUGIN_ROOT / "after-install.md"
        assert after.is_file()
        assert "hermes gateway setup" in after.read_text(encoding="utf-8")
        # Current Hermes installers hard-fail on manifest_version > 1.
        assert "manifest_version:" not in plugin_yaml


class TestPackaging:
    """The wheel must ship sidecar sources, not a bare `adapter` module."""

    def test_pyproject_is_a_namespaced_package(self):
        text = (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'vector-platform = "hermes_vector_platform:register"' in text
        assert 'py-modules = [' not in text
        assert 'packages = ["hermes_vector_platform"]' in text
        assert 'hermes_vector_platform = "."' in text
        assert '"plugin.yaml"' in text
        assert '"bridge/src/*.rs"' in text
        assert '"bridge/Cargo.toml"' in text
        assert '"bridge/Cargo.lock"' in text

    def test_manifest_includes_bridge_sources(self):
        text = (PLUGIN_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert "graft bridge/src" in text
        assert "include plugin.yaml" in text
        assert "include after-install.md" in text
        assert "prune bridge/target" in text


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


CHANNEL_ID = "ab" * 32
assert len(CHANNEL_ID) == 64


class TestParseTargetRef:
    def test_npub_is_dm(self):
        assert vector_adapter._parse_target_ref(NPUB) == (NPUB, None)

    def test_hex_pubkey_is_dm_when_unknown_channel(self, monkeypatch):
        vector_adapter._known_channel_ids.clear()
        monkeypatch.delenv("VECTOR_GROUP_ALLOW_ALL", raising=False)
        assert vector_adapter._parse_target_ref(HEX_PUBKEY) == (NPUB, None)

    def test_remembered_channel_stays_hex(self):
        vector_adapter._known_channel_ids.clear()
        vector_adapter._remember_channel(CHANNEL_ID.upper())
        assert vector_adapter._parse_target_ref(CHANNEL_ID) == (CHANNEL_ID, None)
        vector_adapter._known_channel_ids.clear()

    def test_unknown_channel_hex_encodes_as_npub(self, monkeypatch):
        vector_adapter._known_channel_ids.clear()
        monkeypatch.delenv("VECTOR_GROUP_ALLOW_ALL", raising=False)
        # CHANNEL_ID is valid 64-hex so normalize_npub encodes it as npub.
        result = vector_adapter._parse_target_ref(CHANNEL_ID)
        assert result is not None
        assert result[1] is None
        assert result[0].startswith("npub1")

    def test_garbage(self):
        assert vector_adapter._parse_target_ref("garbage") is None


class TestSendTarget:
    def test_channel_hex_not_encoded_as_npub(self):
        assert vector_adapter._send_target(CHANNEL_ID) == CHANNEL_ID
        assert vector_adapter._send_target(CHANNEL_ID.upper()) == CHANNEL_ID

    def test_npub_canonical(self):
        assert vector_adapter._send_target(NPUB) == NPUB

    def test_pending_key_group_vs_dm(self):
        assert vector_adapter._pending_inbox_key(PEER_NPUB) == PEER_NPUB
        assert (
            vector_adapter._pending_inbox_key(PEER_NPUB, CHANNEL_ID)
            == f"{CHANNEL_ID}:{PEER_NPUB}"
        )


class TestMentionsBot:
    def test_at_npub(self):
        assert vector_adapter._mentions_bot(f"hey @{NPUB}", NPUB)

    def test_nostr_npub(self):
        assert vector_adapter._mentions_bot(f"nostr:{NPUB} ping", NPUB)

    def test_bare_npub(self):
        assert vector_adapter._mentions_bot(f"see {NPUB} please", NPUB)

    def test_display_name(self):
        assert vector_adapter._mentions_bot("hi @Hermes are you there", NPUB, "Hermes")

    def test_everyone_is_not_a_mention(self):
        assert not vector_adapter._mentions_bot("@everyone hello", NPUB, "Hermes")

    def test_unrelated(self):
        assert not vector_adapter._mentions_bot("hello there", NPUB, "Hermes")


class TestMentionRemainder:
    def test_mention_only_npub(self):
        assert vector_adapter._mention_remainder(f"@{NPUB}", NPUB, "Hermes") == ""

    def test_mention_plus_ask(self):
        assert (
            vector_adapter._mention_remainder(f"@{NPUB} what's this", NPUB, "Hermes")
            == "what's this"
        )

    def test_display_name_only(self):
        assert vector_adapter._mention_remainder("@Hermes", NPUB, "Hermes") == ""


class TestGroupSlashCommand:
    def test_approve_variants(self):
        assert vector_adapter._group_slash_command("/approve")
        assert vector_adapter._group_slash_command("/approve session")
        assert vector_adapter._group_slash_command("/approve all always")
        assert vector_adapter._group_slash_command("/APPROVE")
        assert vector_adapter._group_slash_command("/deny")
        assert vector_adapter._group_slash_command("/deny all too risky")

    def test_non_approval_commands_are_not_bypasses(self):
        assert not vector_adapter._group_slash_command("/yolo")
        assert not vector_adapter._group_slash_command("/memory approve")
        assert not vector_adapter._group_slash_command("/skills pending")
        assert not vector_adapter._group_slash_command("/help")
        assert not vector_adapter._group_slash_command("/reset now")
        assert not vector_adapter._group_slash_command("/approve-session")
        assert not vector_adapter._group_slash_command("/deny-all")

    def test_is_command_flag_bypasses_name_check(self):
        assert vector_adapter._group_slash_command("/unknown", is_command=True)
        assert vector_adapter._group_slash_command("not a slash", is_command=True)

    def test_unknown_and_paths_are_not_commands(self):
        assert not vector_adapter._group_slash_command("just chatting")
        assert not vector_adapter._group_slash_command("/not-a-hermes-cmd")
        assert not vector_adapter._group_slash_command("/path/to/file")
        assert not vector_adapter._group_slash_command("")


class TestBlockCommandParse:
    def test_parse_block_variants(self):
        assert vector_adapter._parse_block_command("/block npub1abc") == (
            "block",
            "npub1abc",
        )
        assert vector_adapter._parse_block_command("/BLOCK  npub1abc") == (
            "block",
            "npub1abc",
        )
        assert vector_adapter._parse_block_command("/unblock npub1abc") == (
            "unblock",
            "npub1abc",
        )
        assert vector_adapter._parse_block_command("/blocked") == ("blocked", "")
        assert vector_adapter._parse_block_command("/block") == ("block", "")
        assert vector_adapter._parse_block_command("block npub1abc") is None
        assert vector_adapter._parse_block_command("/approve") is None

    def test_home_operator_only(self, monkeypatch):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_NPUB)
        monkeypatch.delenv("VECTOR_HOME_CHANNEL", raising=False)
        assert not vector_adapter._is_home_operator(PEER_NPUB)
        monkeypatch.setenv("VECTOR_HOME_CHANNEL", NPUB)
        assert vector_adapter._is_home_operator(NPUB)
        assert not vector_adapter._is_home_operator(PEER_NPUB)


class TestInviteCommandParse:
    def test_parse_invite_variants(self):
        cid = "aa" * 32
        assert vector_adapter._parse_invite_command("/invites") == ("invites", "")
        assert vector_adapter._parse_invite_command("/INVITES") == ("invites", "")
        assert vector_adapter._parse_invite_command(f"/join {cid}") == ("join", cid)
        assert vector_adapter._parse_invite_command(f"/decline {cid}") == (
            "decline",
            cid,
        )
        assert vector_adapter._parse_invite_command("/join") == ("join", "")
        assert vector_adapter._parse_invite_command("join " + cid) is None
        assert vector_adapter._parse_invite_command("/approve") is None
        assert vector_adapter._parse_invite_command("/block npub1abc") is None

    def test_format_pending_invites_empty_and_rows(self):
        assert vector_adapter._format_pending_invites([]) == "No parked invites."
        body = vector_adapter._format_pending_invites(
            [
                {
                    "community_id": "aa" * 32,
                    "name": "Ada's house",
                    "inviter_npub": PEER_NPUB,
                }
            ]
        )
        assert "Parked invites (1)" in body
        assert "Ada's house" in body
        assert "aa" * 32 in body
        assert "/join" in body
        assert "/decline" in body


class TestKnownChannels:
    def test_remember_is_required(self):
        vector_adapter._known_channel_ids.clear()
        assert vector_adapter._is_known_channel(CHANNEL_ID) is False
        vector_adapter._remember_channel(f" {CHANNEL_ID.upper()} ")
        assert vector_adapter._is_known_channel(CHANNEL_ID) is True
        vector_adapter._known_channel_ids.clear()

    def test_allow_all_env_counts_as_known(self, monkeypatch):
        vector_adapter._known_channel_ids.clear()
        monkeypatch.setenv("VECTOR_GROUP_ALLOW_ALL", CHANNEL_ID)
        assert vector_adapter._is_known_channel(CHANNEL_ID) is True


class TestGroupSenderAuth:
    def test_dm_allowlist_unions_into_group(self, monkeypatch):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_NPUB)
        monkeypatch.delenv("VECTOR_GROUP_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("VECTOR_GROUP_ALLOW_ALL", raising=False)
        assert vector_adapter._group_sender_is_authorized(PEER_NPUB, CHANNEL_ID) is True
        assert vector_adapter._group_sender_is_authorized(NPUB, CHANNEL_ID) is False

    def test_group_only_users(self, monkeypatch):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", NPUB)
        monkeypatch.setenv("VECTOR_GROUP_ALLOWED_USERS", PEER_HEX)
        monkeypatch.delenv("VECTOR_GROUP_ALLOW_ALL", raising=False)
        assert vector_adapter._group_sender_is_authorized(PEER_NPUB, CHANNEL_ID) is True
        assert vector_adapter._sender_is_authorized(PEER_NPUB) is False

    def test_allow_all_channel(self, monkeypatch):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", NPUB)
        monkeypatch.delenv("VECTOR_GROUP_ALLOWED_USERS", raising=False)
        monkeypatch.setenv("VECTOR_GROUP_ALLOW_ALL", CHANNEL_ID.upper())
        assert vector_adapter._group_sender_is_authorized(PEER_NPUB, CHANNEL_ID) is True
        other = "cd" * 32
        assert vector_adapter._group_sender_is_authorized(PEER_NPUB, other) is False

    def test_allow_all_star_not_wildcard(self, monkeypatch):
        monkeypatch.delenv("VECTOR_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("VECTOR_GROUP_ALLOWED_USERS", raising=False)
        monkeypatch.setenv("VECTOR_GROUP_ALLOW_ALL", "*")
        assert vector_adapter._group_sender_is_authorized(PEER_NPUB, CHANNEL_ID) is False

    def test_no_open_override_without_allowlist(self, monkeypatch):
        monkeypatch.delenv("VECTOR_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("VECTOR_GROUP_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("VECTOR_GROUP_ALLOW_ALL", raising=False)
        assert vector_adapter._group_sender_is_authorized(PEER_NPUB, CHANNEL_ID) is False


class TestTruncateNpub:
    def test_long_npub_matches_session_16_prefix(self):
        out = vector_adapter._truncate_npub(NPUB)
        assert out == f"{NPUB[:16]}..."
        assert len(out) < len(NPUB)
        assert NPUB not in out

    def test_short_or_empty(self):
        assert vector_adapter._truncate_npub("npub1short") == "npub1short"
        assert vector_adapter._truncate_npub("") == ""
        assert vector_adapter._truncate_npub("   ") == ""
        assert vector_adapter._truncate_npub(None) == ""  # type: ignore[arg-type]


class TestGroupChatName:
    def test_community_hides_general(self):
        assert (
            vector_adapter._group_chat_name("Ada's house", "general", CHANNEL_ID)
            == "Ada's house"
        )
        assert (
            vector_adapter._group_chat_name("Ada's house", "GENERAL", CHANNEL_ID)
            == "Ada's house"
        )
        assert (
            vector_adapter._group_chat_name("Ada's house", "", CHANNEL_ID)
            == "Ada's house"
        )

    def test_extra_channel_is_appended(self):
        assert (
            vector_adapter._group_chat_name("Ada's house", "random", CHANNEL_ID)
            == "Ada's house · random"
        )

    def test_no_community_skips_bare_general(self):
        assert vector_adapter._group_chat_name("", "general", CHANNEL_ID) == (
            f"{CHANNEL_ID[:16]}..."
        )
        assert vector_adapter._group_chat_name("", "random", CHANNEL_ID) == "random"


class TestProfileDisplayName:
    def test_name_then_display_name_then_truncate(self):
        assert (
            vector_adapter._profile_display_name(
                {"name": "Ada", "display_name": "A."}, NPUB
            )
            == "Ada"
        )
        assert (
            vector_adapter._profile_display_name(
                {"name": "  ", "display_name": "Ada Lovelace"}, NPUB
            )
            == "Ada Lovelace"
        )
        assert vector_adapter._profile_display_name({}, NPUB) == f"{NPUB[:16]}..."
        assert vector_adapter._profile_display_name(None, NPUB) == f"{NPUB[:16]}..."


class TestRuntimeRecord:
    def test_write_is_0600_payload_and_delete_unlinks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        token = "tok" + "ab" * 30
        vector_adapter._write_runtime_record(8096, token, 1234, NPUB)
        path = tmp_path / "runtime" / "vector-sidecar.json"
        assert path.is_file()
        if os.name == "posix":
            assert (path.stat().st_mode & 0o777) == 0o600
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"port": 8096, "token": token, "pid": 1234, "npub": NPUB}
        vector_adapter._delete_runtime_record()
        assert not path.exists()
        vector_adapter._delete_runtime_record()  # missing_ok

    def test_replace_of_world_readable_file_is_still_0600(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        path = tmp_path / "runtime" / "vector-sidecar.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale", encoding="utf-8")
        if os.name == "posix":
            os.chmod(path, 0o644)
        vector_adapter._write_runtime_record(8096, "t" * 64, 1, NPUB)
        if os.name == "posix":
            assert (path.stat().st_mode & 0o777) == 0o600


class TestEnvEnablement:
    def test_none_without_npub(self, monkeypatch):
        monkeypatch.delenv("VECTOR_NPUB", raising=False)
        assert vector_adapter._env_enablement() is None

    def test_seeds_npub(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        monkeypatch.delenv("VECTOR_HOME_CHANNEL", raising=False)
        monkeypatch.delenv("VECTOR_BOT_NAME", raising=False)
        monkeypatch.delenv("VECTOR_BRIDGE_HOST", raising=False)
        monkeypatch.delenv("VECTOR_BOT_AVATAR", raising=False)
        monkeypatch.delenv("VECTOR_BOT_ABOUT", raising=False)
        monkeypatch.delenv("VECTOR_BOT_BANNER", raising=False)
        monkeypatch.delenv("VECTOR_GROUP_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("VECTOR_GROUP_ALLOW_ALL", raising=False)
        seed = vector_adapter._env_enablement()
        assert seed is not None
        assert seed["npub"] == NPUB
        assert "bridge_port" in seed
        assert seed["bridge_host"] == "127.0.0.1"
        assert "data_dir" in seed
        assert "home_channel" not in seed
        assert "bot_avatar" not in seed
        assert "bot_about" not in seed
        assert "bot_banner" not in seed

    def test_home_channel_seeded(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        monkeypatch.setenv("VECTOR_HOME_CHANNEL", HEX_PUBKEY)
        seed = vector_adapter._env_enablement()
        assert seed["home_channel"]["chat_id"] == NPUB
        assert seed["home_channel"]["name"] == "Home"

    def test_seeds_bot_avatar(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        monkeypatch.setenv("VECTOR_BOT_AVATAR", "/abs/avatar.png")
        seed = vector_adapter._env_enablement()
        assert seed["bot_avatar"] == "/abs/avatar.png"

    def test_seeds_bot_about(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        monkeypatch.setenv("VECTOR_BOT_ABOUT", "A Vector bot")
        seed = vector_adapter._env_enablement()
        assert seed["bot_about"] == "A Vector bot"

    def test_seeds_bot_banner(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        monkeypatch.setenv("VECTOR_BOT_BANNER", "/abs/banner.png")
        seed = vector_adapter._env_enablement()
        assert seed["bot_banner"] == "/abs/banner.png"

    def test_seeds_group_gates(self, monkeypatch):
        monkeypatch.setenv("VECTOR_NPUB", NPUB)
        monkeypatch.setenv("VECTOR_GROUP_ALLOWED_USERS", PEER_NPUB)
        monkeypatch.setenv("VECTOR_GROUP_ALLOW_ALL", CHANNEL_ID.upper())
        seed = vector_adapter._env_enablement()
        assert seed["group_allowed_chats"] == CHANNEL_ID
        assert seed["group_allowed_users"] == PEER_NPUB
        assert "group_allow_all" not in seed


class TestHermesConnectTimeoutFloor:
    def test_sets_when_unset(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", raising=False)
        vector_adapter._ensure_hermes_connect_timeout_floor()
        assert os.environ["HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT"] == str(
            vector_adapter.HERMES_CONNECT_TIMEOUT_FLOOR
        )

    def test_does_not_override_operator(self, monkeypatch):
        monkeypatch.setenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "15")
        vector_adapter._ensure_hermes_connect_timeout_floor()
        assert os.environ["HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT"] == "15"


class TestInstallBotAvatar:
    def test_copies_png_to_data_dir(self, tmp_path):
        src = tmp_path / "face.PNG"
        src.write_bytes(b"png-bytes")
        dest = vector_adapter.install_bot_avatar(str(src), tmp_path / "sdk")
        assert dest == tmp_path / "sdk" / "avatar.png"
        assert dest.read_bytes() == b"png-bytes"

    def test_copies_banner(self, tmp_path):
        src = tmp_path / "wide.png"
        src.write_bytes(b"banner")
        dest = vector_adapter.install_bot_image(str(src), tmp_path / "sdk", "banner")
        assert dest == tmp_path / "sdk" / "banner.png"
        assert dest.read_bytes() == b"banner"

    def test_rejects_non_image(self, tmp_path):
        src = tmp_path / "notes.txt"
        src.write_text("nope")
        try:
            vector_adapter.install_bot_avatar(str(src), tmp_path / "sdk")
        except ValueError as e:
            assert "jpg" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_rejects_missing_file(self, tmp_path):
        try:
            vector_adapter.validate_bot_avatar_src(str(tmp_path / "missing.png"))
        except ValueError as e:
            assert "not a file" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_discover_finds_copied_image(self, tmp_path):
        src = tmp_path / "face.png"
        src.write_bytes(b"png")
        dest = vector_adapter.install_bot_avatar(str(src), tmp_path / "sdk")
        found = vector_adapter.discover_bot_image(tmp_path / "sdk", "avatar")
        assert found == dest
        assert vector_adapter.discover_bot_image(tmp_path / "sdk", "banner") is None


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


class TestBridgeReleaseTarget:
    def test_linux_x86_64(self, monkeypatch):
        monkeypatch.setattr(vector_adapter.sys, "platform", "linux")
        monkeypatch.setattr(vector_adapter.platform, "machine", lambda: "x86_64")
        assert (
            vector_adapter.bridge_release_target() == "x86_64-unknown-linux-gnu"
        )

    def test_linux_amd64_alias(self, monkeypatch):
        monkeypatch.setattr(vector_adapter.sys, "platform", "linux")
        monkeypatch.setattr(vector_adapter.platform, "machine", lambda: "AMD64")
        assert (
            vector_adapter.bridge_release_target() == "x86_64-unknown-linux-gnu"
        )

    def test_darwin_arm64(self, monkeypatch):
        monkeypatch.setattr(vector_adapter.sys, "platform", "darwin")
        monkeypatch.setattr(vector_adapter.platform, "machine", lambda: "arm64")
        assert vector_adapter.bridge_release_target() == "aarch64-apple-darwin"

    def test_darwin_x86_64(self, monkeypatch):
        monkeypatch.setattr(vector_adapter.sys, "platform", "darwin")
        monkeypatch.setattr(vector_adapter.platform, "machine", lambda: "x86_64")
        assert vector_adapter.bridge_release_target() == "x86_64-apple-darwin"

    def test_windows_none(self, monkeypatch):
        monkeypatch.setattr(vector_adapter.sys, "platform", "win32")
        monkeypatch.setattr(vector_adapter.platform, "machine", lambda: "AMD64")
        assert vector_adapter.bridge_release_target() is None

    def test_unknown_arch(self, monkeypatch):
        monkeypatch.setattr(vector_adapter.sys, "platform", "linux")
        monkeypatch.setattr(vector_adapter.platform, "machine", lambda: "riscv64")
        assert vector_adapter.bridge_release_target() is None


class TestResolveBridgeBin:
    def test_override_wins(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom"
        monkeypatch.setenv("VECTOR_BRIDGE_BIN", str(custom))
        assert vector_adapter.resolve_bridge_bin() == custom

    def test_in_tree_beats_prebuilt(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VECTOR_BRIDGE_BIN", raising=False)
        monkeypatch.setattr(vector_adapter, "_read_vector_yaml_block", lambda: {})
        in_tree = tmp_path / "in-tree"
        in_tree.write_text("")
        prebuilt_dir = tmp_path / "bin"
        prebuilt_dir.mkdir()
        (prebuilt_dir / "vector-bridge").write_text("pre")
        (prebuilt_dir / ".version").write_text(
            f"v{vector_adapter.PLUGIN_VERSION}\n"
        )
        monkeypatch.setattr(vector_adapter, "_DEFAULT_BRIDGE_BIN", in_tree)
        monkeypatch.setattr(
            vector_adapter, "_prebuilt_bin_dir", lambda: prebuilt_dir
        )
        assert vector_adapter.resolve_bridge_bin() == in_tree

    def test_versioned_prebuilt_when_in_tree_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VECTOR_BRIDGE_BIN", raising=False)
        monkeypatch.setattr(vector_adapter, "_read_vector_yaml_block", lambda: {})
        missing = tmp_path / "missing-in-tree"
        prebuilt_dir = tmp_path / "bin"
        prebuilt_dir.mkdir()
        binary = prebuilt_dir / "vector-bridge"
        binary.write_text("pre")
        (prebuilt_dir / ".version").write_text(
            f"v{vector_adapter.PLUGIN_VERSION}\n"
        )
        monkeypatch.setattr(vector_adapter, "_DEFAULT_BRIDGE_BIN", missing)
        monkeypatch.setattr(
            vector_adapter, "_prebuilt_bin_dir", lambda: prebuilt_dir
        )
        assert vector_adapter.resolve_bridge_bin() == binary

    def test_stale_prebuilt_ignored(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VECTOR_BRIDGE_BIN", raising=False)
        monkeypatch.setattr(vector_adapter, "_read_vector_yaml_block", lambda: {})
        missing = tmp_path / "missing-in-tree"
        prebuilt_dir = tmp_path / "bin"
        prebuilt_dir.mkdir()
        (prebuilt_dir / "vector-bridge").write_text("old")
        (prebuilt_dir / ".version").write_text("v0.0.1\n")
        monkeypatch.setattr(vector_adapter, "_DEFAULT_BRIDGE_BIN", missing)
        monkeypatch.setattr(
            vector_adapter, "_prebuilt_bin_dir", lambda: prebuilt_dir
        )
        assert vector_adapter.resolve_bridge_bin() == missing


class TestReleaseCoords:
    def test_default_repo_and_tag(self, monkeypatch):
        monkeypatch.setattr(vector_adapter, "_read_vector_yaml_block", lambda: {})
        assert vector_adapter._release_repo() == "BonesGit/hermes-vector-platform"
        assert vector_adapter._release_tag() == f"v{vector_adapter.PLUGIN_VERSION}"

    def test_yaml_repo_and_tag(self, monkeypatch):
        monkeypatch.setattr(
            vector_adapter,
            "_read_vector_yaml_block",
            lambda: {"prebuilt": {"repo": "Acme/vector-fork", "tag": "v1.2.3"}},
        )
        assert vector_adapter._release_repo() == "Acme/vector-fork"
        assert vector_adapter._release_tag() == "v1.2.3"

    def test_rejects_malformed_repo(self, monkeypatch):
        monkeypatch.setattr(
            vector_adapter,
            "_read_vector_yaml_block",
            lambda: {"prebuilt": {"repo": "../evil/repo"}},
        )
        assert vector_adapter._release_repo() == "BonesGit/hermes-vector-platform"

    def test_tag_gains_v_prefix(self, monkeypatch):
        monkeypatch.setattr(
            vector_adapter,
            "_read_vector_yaml_block",
            lambda: {"prebuilt": {"tag": "0.4.0"}},
        )
        assert vector_adapter._release_tag() == "v0.4.0"


class TestParseSha256Sums:
    def test_two_space_and_star_prefix(self):
        digest = "a" * 64
        asset = "vector-bridge-x86_64-unknown-linux-gnu"
        assert (
            vector_adapter._parse_sha256sums(f"{digest}  {asset}\n", asset)
            == digest
        )
        assert (
            vector_adapter._parse_sha256sums(f"{digest} *{asset}\n", asset)
            == digest
        )

    def test_missing_asset(self):
        digest = "b" * 64
        text = f"{digest}  vector-bridge-other\n"
        assert (
            vector_adapter._parse_sha256sums(
                text, "vector-bridge-x86_64-unknown-linux-gnu"
            )
            is None
        )


class TestRegister:
    def test_register_calls_ctx(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", raising=False)
        ctx = MagicMock()
        vector_adapter.register(ctx)
        ctx.register_redaction_patterns.assert_called_once()
        patterns = ctx.register_redaction_patterns.call_args.args[0]
        assert any("nsec1" in p for p in patterns)
        nsec_pat = next(p for p in patterns if "nsec1" in p)
        assert re.search(nsec_pat, "nsec1abcdefghijklmnopqrstuvwxyzabcdefghijk")
        assert re.search(nsec_pat, "nsec1" + "a" * 20)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "vector"
        assert kwargs["label"] == "Vector"
        assert kwargs["cron_deliver_env_var"] == "VECTOR_HOME_CHANNEL"
        assert kwargs["allowed_users_env"] == "VECTOR_ALLOWED_USERS"
        assert "allow_all_env" not in kwargs or not kwargs.get("allow_all_env")
        assert kwargs["apply_yaml_config_fn"] is vector_adapter._apply_yaml_config
        assert kwargs["parse_target_ref_fn"] is vector_adapter._parse_target_ref
        assert kwargs["parse_target_ref_fn"] is not vector_adapter.normalize_npub
        assert kwargs["parse_target_ref_fn"] is not vector_adapter._parse_npub_target
        assert kwargs["check_fn"] is vector_adapter.check_requirements
        assert kwargs["validate_config"] is vector_adapter.validate_config
        assert kwargs["env_enablement_fn"] is vector_adapter._env_enablement
        assert kwargs["standalone_sender_fn"] is vector_adapter._standalone_send
        assert kwargs["setup_fn"] is vector_adapter.interactive_setup
        assert kwargs.get("ensure_deps_fn") is None
        assert "hermes gateway setup" in kwargs["install_hint"]
        assert kwargs["max_message_length"] == 4000
        assert "markdown" in kwargs["platform_hint"].lower()
        assert "mention" in kwargs["platform_hint"].lower()
        sample = kwargs["parse_target_ref_fn"](HEX_PUBKEY)
        assert sample == (NPUB, None)
        assert not isinstance(sample, str)

    def test_register_floors_hermes_connect_timeout(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", raising=False)
        vector_adapter.register(MagicMock())
        assert os.environ["HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT"] == str(
            vector_adapter.HERMES_CONNECT_TIMEOUT_FLOOR
        )


# ---------------------------------------------------------------------------
# Adapter lifecycle + DM path (mocked HTTP sidecar — no live Vector network)
# ---------------------------------------------------------------------------

import asyncio
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
    """Loopback HTTP sidecar: token auth, /health, /send, /edit, /typing, /events."""

    def __init__(self, token: str, npub: str = NPUB, ready: bool = True):
        self.token = token
        self.npub = npub
        self.ready = ready
        self.sends: list = []
        self.edits: list = []
        self.typing: list = []
        self.reacts: list = []
        self.blocks: list = []
        self.deletes: list = []
        self.blocked: list = []
        self.pending_invites: list = []
        self.invite_accepts: list = []
        self.invite_declines: list = []
        self.health_headers: list = []
        self.send_headers: list = []
        self.edit_headers: list = []
        self.typing_headers: list = []
        self.react_headers: list = []
        self.block_headers: list = []
        self.delete_headers: list = []
        self.events_headers: list = []
        self.inject_queue: list = []
        self.communities: list = []
        self.listed_communities: list = []
        self.profiles: dict = {}
        self.profile_gets: list = []
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
                path, _, query = self.path.partition("?")
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
                if path == "/communities":
                    return self._json(200, {"communities": sidecar.listed_communities})
                if path == "/profile":
                    npub = ""
                    for part in query.split("&"):
                        if part.startswith("npub="):
                            npub = part[5:]
                            break
                    sidecar.profile_gets.append(npub)
                    canned = sidecar.profiles.get(npub)
                    if isinstance(canned, dict):
                        return self._json(200, canned)
                    return self._json(
                        200,
                        {
                            "npub": npub,
                            "name": "",
                            "display_name": "",
                            "about": "",
                            "picture": "",
                            "banner": "",
                            "bot": False,
                            "nip05": "",
                            "website": "",
                        },
                    )
                if path == "/block":
                    return self._json(200, {"blocked": sidecar.blocked})
                if path == "/invites":
                    return self._json(200, {"invites": sidecar.pending_invites})
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
                            evt_id = str((evt.get("data") or {}).get("id") or "")
                            if evt_id:
                                self.wfile.write(b"id: " + evt_id.encode() + b"\n")
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
                if path == "/edit":
                    sidecar.edits.append(data)
                    sidecar.edit_headers.append(dict(self.headers))
                    orig = data.get("message_id") or "evt-edit-target"
                    return self._json(
                        200, {"id": orig, "edit_id": "evt-kind16-1"}
                    )
                if path == "/typing":
                    sidecar.typing.append(data)
                    sidecar.typing_headers.append(dict(self.headers))
                    return self._json(200, {"ok": True})
                if path == "/react":
                    sidecar.reacts.append(data)
                    sidecar.react_headers.append(dict(self.headers))
                    return self._json(200, {"ok": True})
                if path == "/block":
                    sidecar.blocks.append(data)
                    sidecar.block_headers.append(dict(self.headers))
                    npub = data.get("npub") or ""
                    unblock = bool(data.get("unblock"))
                    if unblock:
                        sidecar.blocked = [
                            row
                            for row in sidecar.blocked
                            if (row.get("npub") if isinstance(row, dict) else row)
                            != npub
                        ]
                    elif npub and npub not in [
                        row.get("npub") if isinstance(row, dict) else row
                        for row in sidecar.blocked
                    ]:
                        sidecar.blocked.append({"npub": npub, "name": "", "display_name": ""})
                    return self._json(
                        200, {"ok": True, "npub": npub, "blocked": not unblock}
                    )
                if path == "/delete":
                    sidecar.deletes.append(data)
                    sidecar.delete_headers.append(dict(self.headers))
                    return self._json(
                        200, {"ok": True, "id": data.get("message_id") or ""}
                    )
                if path == "/invites/accept":
                    sidecar.invite_accepts.append(data)
                    cid = data.get("community_id") or ""
                    sidecar.pending_invites = [
                        row
                        for row in sidecar.pending_invites
                        if (row.get("community_id") if isinstance(row, dict) else None)
                        != cid
                    ]
                    return self._json(
                        200,
                        {
                            "ok": True,
                            "community_id": cid,
                            "name": "Ada's house",
                            "channels": [
                                {"channel_id": CHANNEL_ID, "name": "general"}
                            ],
                        },
                    )
                if path == "/invites/decline":
                    sidecar.invite_declines.append(data)
                    cid = data.get("community_id") or ""
                    sidecar.pending_invites = [
                        row
                        for row in sidecar.pending_invites
                        if (row.get("community_id") if isinstance(row, dict) else None)
                        != cid
                    ]
                    return self._json(200, {"ok": True, "community_id": cid})
                if path == "/communities":
                    sidecar.communities.append(data)
                    return self._json(
                        200,
                        {
                            "created": True,
                            "community_id": "cc" * 32,
                            "channel_id": CHANNEL_ID,
                            "name": data.get("name") or "Hermes",
                        },
                    )
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

    if not extra.get("bot_name"):
        monkeypatch.delenv("VECTOR_BOT_NAME", raising=False)
    if not extra.get("bot_about"):
        monkeypatch.delenv("VECTOR_BOT_ABOUT", raising=False)
    if not extra.get("bot_avatar"):
        monkeypatch.delenv("VECTOR_BOT_AVATAR", raising=False)
    if not extra.get("bot_banner"):
        monkeypatch.delenv("VECTOR_BOT_BANNER", raising=False)
    vector_adapter._known_channel_ids.clear()
    if extra.get("allowed_users"):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", extra["allowed_users"])
    if extra.get("group_allowed_users"):
        monkeypatch.setenv("VECTOR_GROUP_ALLOWED_USERS", extra["group_allowed_users"])
    else:
        monkeypatch.delenv("VECTOR_GROUP_ALLOWED_USERS", raising=False)
    if extra.get("group_allow_all"):
        monkeypatch.setenv("VECTOR_GROUP_ALLOW_ALL", extra["group_allow_all"])
    else:
        monkeypatch.delenv("VECTOR_GROUP_ALLOW_ALL", raising=False)
    if extra.get("create_community"):
        monkeypatch.setenv("VECTOR_CREATE_COMMUNITY", "on")
    else:
        monkeypatch.delenv("VECTOR_CREATE_COMMUNITY", raising=False)
    if extra.get("community_download_all"):
        monkeypatch.setenv("VECTOR_COMMUNITY_DOWNLOAD_ALL", "on")
    else:
        monkeypatch.delenv("VECTOR_COMMUNITY_DOWNLOAD_ALL", raising=False)
    monkeypatch.delenv("VECTOR_HOME_CHANNEL", raising=False)
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
        "startup_timeout": extra.get("startup_timeout", 5),
        "data_dir": str(data_dir),
    }
    if extra.get("bot_name"):
        cfg.extra["bot_name"] = extra["bot_name"]
    if extra.get("bot_about"):
        cfg.extra["bot_about"] = extra["bot_about"]
    if extra.get("bot_avatar"):
        cfg.extra["bot_avatar"] = extra["bot_avatar"]
    if extra.get("bot_banner"):
        cfg.extra["bot_banner"] = extra["bot_banner"]
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

    def test_channel_hex_is_group(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        info = asyncio.run(adapter.get_chat_info(CHANNEL_ID))
        assert info["type"] == "group"
        assert info["chat_id"] == CHANNEL_ID
        assert info["name"] == f"{CHANNEL_ID[:16]}..."

    def test_fetches_kind0_name(self, monkeypatch, tmp_path):
        token = "a" * 64
        sidecar = MockSidecar(token=token)
        sidecar.profiles[NPUB] = {
            "npub": NPUB,
            "name": "Ada",
            "display_name": "",
            "about": "hi",
            "picture": "",
            "banner": "",
            "bot": False,
        }
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = token

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    info = await adapter.get_chat_info(NPUB)
                    assert info["name"] == "Ada"
                    assert info["type"] == "dm"
                    assert info["chat_id"] == NPUB
                    assert adapter._peer_label(NPUB) == "Ada"
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.profile_gets == [NPUB]
        finally:
            sidecar.stop()

    def test_empty_kind0_falls_back_to_truncate(self, monkeypatch, tmp_path):
        token = "a" * 64
        sidecar = MockSidecar(token=token)
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = token

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    info = await adapter.get_chat_info(NPUB)
                    assert info["name"] == f"{NPUB[:16]}..."
                    assert NPUB not in adapter._profile_names
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
        finally:
            sidecar.stop()

    def test_channel_name_from_communities_list(self, monkeypatch, tmp_path):
        token = "a" * 64
        sidecar = MockSidecar(token=token)
        sidecar.listed_communities = [
            {
                "community_id": "cc" * 32,
                "name": "Ada's house",
                "channels": [
                    {
                        "channel_id": CHANNEL_ID,
                        "name": "general",
                        "private": False,
                        "readable": True,
                    }
                ],
            }
        ]
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = token

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    info = await adapter.get_chat_info(CHANNEL_ID)
                    assert info["type"] == "group"
                    assert info["name"] == "Ada's house"
                    assert adapter._channel_names[CHANNEL_ID] == "general"
                    assert adapter._community_names["cc" * 32] == "Ada's house"
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
        finally:
            sidecar.stop()

    def test_extra_channel_appended_to_community_name(self, monkeypatch, tmp_path):
        token = "a" * 64
        sidecar = MockSidecar(token=token)
        sidecar.listed_communities = [
            {
                "community_id": "cc" * 32,
                "name": "Ada's house",
                "channels": [
                    {
                        "channel_id": CHANNEL_ID,
                        "name": "random",
                        "private": False,
                        "readable": True,
                    }
                ],
            }
        ]
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = token

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    info = await adapter.get_chat_info(CHANNEL_ID)
                    assert info["name"] == "Ada's house · random"
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
        finally:
            sidecar.stop()

    def test_group_allow_all_published_as_hermes_extra(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch, tmp_path, group_allow_all=CHANNEL_ID.upper()
        )
        assert adapter.config.extra["group_allowed_chats"] == CHANNEL_ID
        assert "group_allow_all" not in adapter.config.extra


class TestJoinedChannelNotice:
    def test_format_is_copy_pasteable(self):
        community = "cc" * 32
        body = vector_adapter._format_joined_notice(
            community,
            [{"channel_id": CHANNEL_ID, "name": "general"}],
            community_name="Ada's house",
        )
        assert "I joined Ada's house." in body
        assert f"channel_id: {CHANNEL_ID}" in body
        assert f"community_id: {community}" in body
        assert "vector.communities.open_channels" in body
        assert "general" in body

    def test_load_save_roundtrip(self, tmp_path):
        ids = {CHANNEL_ID, "cd" * 32}
        vector_adapter._save_notified_channel_ids(tmp_path, ids)
        path = tmp_path / vector_adapter.NOTIFIED_CHANNELS_FILE
        assert oct(path.stat().st_mode)[-3:] == "600"
        assert vector_adapter._load_notified_channel_ids(tmp_path) == ids

    def test_notify_dms_home_once(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        monkeypatch.setenv("VECTOR_HOME_CHANNEL", NPUB)
        adapter._running = True
        adapter._http_client = MagicMock()
        sent = []

        async def fake_send(chat_id, content, reply_to=None, metadata=None):
            sent.append((chat_id, content))
            return vector_adapter.SendResult(success=True, message_id="n1")

        monkeypatch.setattr(adapter, "send", fake_send)
        community = "cc" * 32

        async def go():
            await adapter._notify_joined_channels(
                community, [{"channel_id": CHANNEL_ID, "name": "general"}]
            )
            await adapter._notify_joined_channels(
                community, [{"channel_id": CHANNEL_ID, "name": "general"}]
            )

        asyncio.run(go())
        assert len(sent) == 1
        assert sent[0][0] == NPUB
        assert CHANNEL_ID in sent[0][1]
        saved = json.loads(
            (Path(adapter.data_dir) / vector_adapter.NOTIFIED_CHANNELS_FILE).read_text()
        )
        assert CHANNEL_ID in saved

    def test_notify_without_home_persists(self, monkeypatch, tmp_path, caplog):
        monkeypatch.delenv("VECTOR_HOME_CHANNEL", raising=False)
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._running = True
        sent = []

        async def fake_send(*_a, **_k):
            sent.append(1)
            return vector_adapter.SendResult(success=True)

        monkeypatch.setattr(adapter, "send", fake_send)
        caplog.set_level(logging.INFO, logger="hermes_plugins.vector_platform.adapter")
        asyncio.run(
            adapter._notify_joined_channels(
                "cc" * 32, [{"channel_id": CHANNEL_ID, "name": ""}]
            )
        )
        assert sent == []
        assert CHANNEL_ID in caplog.text
        saved = json.loads(
            (Path(adapter.data_dir) / vector_adapter.NOTIFIED_CHANNELS_FILE).read_text()
        )
        assert CHANNEL_ID in saved

    def test_notify_failed_send_does_not_persist(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        monkeypatch.setenv("VECTOR_HOME_CHANNEL", NPUB)
        adapter._running = True
        adapter._http_client = MagicMock()

        async def fake_send(*_a, **_k):
            return vector_adapter.SendResult(success=False, error="boom")

        monkeypatch.setattr(adapter, "send", fake_send)
        asyncio.run(
            adapter._notify_joined_channels(
                "cc" * 32, [{"channel_id": CHANNEL_ID, "name": "x"}]
            )
        )
        path = Path(adapter.data_dir) / vector_adapter.NOTIFIED_CHANNELS_FILE
        assert not path.exists()
        assert CHANNEL_ID not in adapter._notified_channel_ids

    def test_community_joined_sse(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        monkeypatch.setenv("VECTOR_HOME_CHANNEL", NPUB)
        adapter._running = True
        adapter._http_client = MagicMock()
        sent = []

        async def fake_send(chat_id, content, **_k):
            sent.append(content)
            return vector_adapter.SendResult(success=True, message_id="n1")

        monkeypatch.setattr(adapter, "send", fake_send)
        asyncio.run(
            adapter._dispatch_sse_event(
                {
                    "type": "community_joined",
                    "data": {
                        "community_id": "cc" * 32,
                        "channels": [{"channel_id": CHANNEL_ID, "name": "general"}],
                    },
                }
            )
        )
        assert sent
        assert CHANNEL_ID in sent[0]

    def test_empty_channels_does_not_dm(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        monkeypatch.setenv("VECTOR_HOME_CHANNEL", NPUB)
        adapter._running = True
        sent = []

        async def fake_send(*_a, **_k):
            sent.append(1)
            return vector_adapter.SendResult(success=True)

        monkeypatch.setattr(adapter, "send", fake_send)
        asyncio.run(adapter._notify_joined_channels("cc" * 32, []))
        assert sent == []

    def test_community_joined_empty_syncs(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        called = []

        async def fake_sync():
            called.append(1)

        monkeypatch.setattr(adapter, "_sync_joined_channels", fake_sync)
        asyncio.run(
            adapter._dispatch_sse_event(
                {
                    "type": "community_joined",
                    "data": {"community_id": "cc" * 32, "channels": []},
                }
            )
        )
        assert called == [1]


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
        assert src.role_authorized is not True
        assert captured[0].text == "hi"
        assert captured[0].message_id == "m1"

    def test_blocked_sender_is_dropped(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, npub=NPUB)
        adapter._blocked_npubs.add(PEER_NPUB)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(_message_event(PEER_NPUB, "spam", msg_id="b1"))
        )
        assert captured == []

    def test_message_delete_does_not_start_a_turn(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._record_last_inbound(PEER_NPUB, "deadbeef")
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._dispatch_sse_event(
                {
                    "type": "message_delete",
                    "data": {"id": "deadbeef", "chat_id": PEER_NPUB},
                }
            )
        )
        assert captured == []
        assert PEER_NPUB not in adapter._last_inbound_by_chat
        assert "deadbeef" in adapter._inbound_ids

    def test_operator_block_command_mutes_and_does_not_turn(
        self, monkeypatch, tmp_path
    ):
        token = "a" * 64
        sidecar = MockSidecar(token=token)
        port = sidecar.start()
        spam = vector_adapter.hex_to_npub("11" * 32)
        try:
            adapter = _make_adapter(
                monkeypatch,
                tmp_path,
                bridge_port=port,
                npub=NPUB,
                allowed_users=PEER_NPUB,
            )
            monkeypatch.setenv("VECTOR_HOME_CHANNEL", PEER_NPUB)
            adapter._sidecar_token = token
            adapter._running = True
            captured = []
            sent = []

            async def capture(event):
                captured.append(event)

            adapter.handle_message = capture  # type: ignore[method-assign]

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    orig_send = adapter.send

                    async def wrap_send(chat_id, content, **kwargs):
                        sent.append(content)
                        return await orig_send(chat_id, content, **kwargs)

                    adapter.send = wrap_send  # type: ignore[method-assign]
                    await adapter._handle_message_event(
                        _message_event(
                            PEER_NPUB, f"/block {spam}", msg_id="cmd-block"
                        )
                    )
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert captured == []
            assert sidecar.blocks == [{"npub": spam}]
            assert spam in adapter._blocked_npubs
            assert sent and "Blocked" in sent[0]
        finally:
            sidecar.stop()

    def test_allowlisted_non_home_block_command_is_a_turn(
        self, monkeypatch, tmp_path
    ):
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        monkeypatch.delenv("VECTOR_HOME_CHANNEL", raising=False)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        spam = vector_adapter.hex_to_npub("11" * 32)
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, f"/block {spam}", msg_id="cmd-block-no")
            )
        )
        assert len(captured) == 1
        assert captured[0].text == f"/block {spam}"

    def test_home_invites_command_lists_parked(self, monkeypatch, tmp_path):
        token = "a" * 64
        community_id = "cc" * 32
        sidecar = MockSidecar(token=token)
        sidecar.pending_invites = [
            {
                "community_id": community_id,
                "name": "Ada's house",
                "inviter_npub": NPUB,
                "version": 2,
            }
        ]
        port = sidecar.start()
        try:
            adapter = _make_adapter(
                monkeypatch,
                tmp_path,
                bridge_port=port,
                npub=NPUB,
                allowed_users=PEER_NPUB,
            )
            monkeypatch.setenv("VECTOR_HOME_CHANNEL", PEER_NPUB)
            adapter._sidecar_token = token
            adapter._running = True
            captured = []
            sent = []

            async def capture(event):
                captured.append(event)

            adapter.handle_message = capture  # type: ignore[method-assign]

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    orig_send = adapter.send

                    async def wrap_send(chat_id, content, **kwargs):
                        sent.append(content)
                        return await orig_send(chat_id, content, **kwargs)

                    adapter.send = wrap_send  # type: ignore[method-assign]
                    await adapter._handle_message_event(
                        _message_event(PEER_NPUB, "/invites", msg_id="cmd-invites")
                    )
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert captured == []
            assert sent and "Ada's house" in sent[0]
            assert community_id in sent[0]
        finally:
            sidecar.stop()

    def test_home_join_and_decline_parked_invite(self, monkeypatch, tmp_path):
        token = "a" * 64
        community_id = "cc" * 32
        sidecar = MockSidecar(token=token)
        sidecar.pending_invites = [
            {
                "community_id": community_id,
                "name": "Ada's house",
                "inviter_npub": NPUB,
            }
        ]
        port = sidecar.start()
        try:
            adapter = _make_adapter(
                monkeypatch,
                tmp_path,
                bridge_port=port,
                npub=NPUB,
                allowed_users=PEER_NPUB,
            )
            monkeypatch.setenv("VECTOR_HOME_CHANNEL", PEER_NPUB)
            adapter._sidecar_token = token
            adapter._running = True
            captured = []
            sent = []

            async def capture(event):
                captured.append(event)

            adapter.handle_message = capture  # type: ignore[method-assign]

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    orig_send = adapter.send

                    async def wrap_send(chat_id, content, **kwargs):
                        sent.append(content)
                        return await orig_send(chat_id, content, **kwargs)

                    adapter.send = wrap_send  # type: ignore[method-assign]
                    await adapter._handle_message_event(
                        _message_event(
                            PEER_NPUB,
                            f"/join {community_id}",
                            msg_id="cmd-join",
                        )
                    )
                    await adapter._handle_message_event(
                        _message_event(
                            PEER_NPUB,
                            f"/decline {community_id}",
                            msg_id="cmd-decline",
                        )
                    )
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert captured == []
            assert sidecar.invite_accepts == [{"community_id": community_id}]
            assert sidecar.invite_declines == [{"community_id": community_id}]
            assert sent and "I joined" in sent[0]
            assert CHANNEL_ID in sent[0]
            assert any("Declined" in body for body in sent)
            assert CHANNEL_ID in adapter._notified_channel_ids
        finally:
            sidecar.stop()

    def test_allowlisted_non_home_invite_command_is_a_turn(
        self, monkeypatch, tmp_path
    ):
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        monkeypatch.delenv("VECTOR_HOME_CHANNEL", raising=False)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "/invites", msg_id="cmd-invites-no")
            )
        )
        assert len(captured) == 1
        assert captured[0].text == "/invites"

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

    def test_group_mention_dispatches(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            bot_name="Hermes",
            allowed_users=PEER_NPUB,
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"hey @{NPUB} ping",
                    msg_id="g-mention",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    community_id="cc" * 32,
                )
            )
        )
        assert len(captured) == 1
        src = captured[0].source
        assert src.chat_type == "group"
        assert src.chat_id == CHANNEL_ID
        assert src.user_id == PEER_NPUB
        assert src.parent_chat_id == "cc" * 32
        assert src.role_authorized is True

    def test_group_unmentioned_dropped(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "just chatting",
                    msg_id="g-quiet",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert captured == []

    def test_group_approve_without_mention_dispatches(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "/approve session",
                    msg_id="g-approve",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert len(captured) == 1
        assert captured[0].text == "/approve session"
        assert captured[0].source.chat_type == "group"
        assert captured[0].source.role_authorized is True

    def test_group_deny_and_approve_args_without_mention(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        for text, msg_id in (
            ("/deny too broad", "g-deny-slash"),
            ("/approve always", "g-approve-always"),
            ("/approve all session", "g-approve-all-session"),
            ("/deny all", "g-deny-all"),
        ):
            captured.clear()
            asyncio.run(
                adapter._handle_message_event(
                    _message_event(
                        PEER_NPUB,
                        text,
                        msg_id=msg_id,
                        is_group=True,
                        chat_id=CHANNEL_ID,
                    )
                )
            )
            assert len(captured) == 1, text
            assert captured[0].text == text

    def test_group_unknown_slash_still_needs_mention(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "/shrug",
                    msg_id="g-shrug",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert captured == []

    def test_group_is_command_flag_without_mention(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "/approve always",
                    msg_id="g-flag",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    is_command=True,
                )
            )
        )
        assert len(captured) == 1
        assert captured[0].text == "/approve always"

    def test_group_unauthorized_slash_still_dropped(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=NPUB
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "/approve",
                    msg_id="g-unauth-approve",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    is_command=True,
                )
            )
        )
        assert captured == []

    def test_group_unauthorized_mention_dropped_even_with_pairing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_PAIRING", "on")
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=NPUB,
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} please-pair",
                    msg_id="g-unauth",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert captured == []

    def test_group_reply_to_bot_counts(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=PEER_NPUB,
        )
        adapter._record_sent_message("bot-msg-1")
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "following up",
                    msg_id="g-reply",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    reply_to="bot-msg-1",
                )
            )
        )
        assert len(captured) == 1
        assert captured[0].source.chat_type == "group"

    def test_group_unauthorized_sender_dropped_even_with_mention(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, npub=NPUB, allowed_users=NPUB)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} hello",
                    msg_id="g-deny",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert captured == []

    def test_group_only_user_mention_dispatches(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=NPUB,
            group_allowed_users=PEER_NPUB,
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} ping",
                    msg_id="g-group-only",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert len(captured) == 1
        assert captured[0].source.user_id == PEER_NPUB
        assert captured[0].source.role_authorized is True

    def test_group_allow_all_any_member_mention(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=NPUB,
            group_allow_all=CHANNEL_ID,
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} ping",
                    msg_id="g-open",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert len(captured) == 1
        assert captured[0].source.role_authorized is True

    def test_group_allow_all_still_needs_mention(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=NPUB,
            group_allow_all=CHANNEL_ID,
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "@everyone hello",
                    msg_id="g-everyone",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert captured == []

    def test_group_allow_all_does_not_need_look_list(self, monkeypatch, tmp_path):
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=NPUB,
            group_allow_all=CHANNEL_ID,
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} ping",
                    msg_id="g-open-no-look",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                )
            )
        )
        assert len(captured) == 1

    def test_group_only_user_cannot_dm(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_PAIRING", "off")
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=NPUB,
            group_allowed_users=PEER_NPUB,
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "please dm", msg_id="dm-group-only")
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
        assert "VECTOR_BOT_NAME" not in env
        assert "VECTOR_BOT_ABOUT" not in env
        assert "VECTOR_BOT_AVATAR" not in env
        assert "VECTOR_BOT_BANNER" not in env
        assert "VECTOR_NSEC" not in env
        assert "VECTOR_MNEMONIC" not in env
        assert "VECTOR_STUB" not in env
        if sys.platform != "win32":
            assert kwargs.get("start_new_session") is True
            assert "preexec_fn" not in kwargs
        adapter._close_bridge_log()

    def test_spawn_passes_name_when_set(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, bot_name="Ada")
        adapter._sidecar_token = "tok" + "ab" * 30
        captured: dict = {}

        def fake_popen(*args, **kwargs):
            captured["kwargs"] = kwargs
            return FakeBridgeProc()

        monkeypatch.setattr(vector_adapter.subprocess, "Popen", fake_popen)
        adapter._spawn_bridge()
        assert captured["kwargs"]["env"]["VECTOR_BOT_NAME"] == "Ada"
        adapter._close_bridge_log()

    def test_spawn_passes_about_when_set(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, bot_about="kind-0 bio")
        adapter._sidecar_token = "tok" + "ab" * 30
        captured: dict = {}

        def fake_popen(*args, **kwargs):
            captured["kwargs"] = kwargs
            return FakeBridgeProc()

        monkeypatch.setattr(vector_adapter.subprocess, "Popen", fake_popen)
        adapter._spawn_bridge()
        assert captured["kwargs"]["env"]["VECTOR_BOT_ABOUT"] == "kind-0 bio"
        adapter._close_bridge_log()

    def test_spawn_passes_avatar_when_file_exists(self, monkeypatch, tmp_path):
        pic = tmp_path / "face.png"
        pic.write_bytes(b"png")
        adapter = _make_adapter(monkeypatch, tmp_path, bot_avatar=str(pic))
        adapter._sidecar_token = "tok" + "ab" * 30
        captured: dict = {}

        def fake_popen(*args, **kwargs):
            captured["kwargs"] = kwargs
            return FakeBridgeProc()

        monkeypatch.setattr(vector_adapter.subprocess, "Popen", fake_popen)
        adapter._spawn_bridge()
        assert captured["kwargs"]["env"]["VECTOR_BOT_AVATAR"] == str(pic.resolve())
        adapter._close_bridge_log()

    def test_spawn_passes_banner_when_file_exists(self, monkeypatch, tmp_path):
        pic = tmp_path / "banner.png"
        pic.write_bytes(b"png")
        adapter = _make_adapter(monkeypatch, tmp_path, bot_banner=str(pic))
        adapter._sidecar_token = "tok" + "ab" * 30
        captured: dict = {}

        def fake_popen(*args, **kwargs):
            captured["kwargs"] = kwargs
            return FakeBridgeProc()

        monkeypatch.setattr(vector_adapter.subprocess, "Popen", fake_popen)
        adapter._spawn_bridge()
        assert captured["kwargs"]["env"]["VECTOR_BOT_BANNER"] == str(pic.resolve())
        adapter._close_bridge_log()

    def test_init_discovers_avatar_in_data_dir(self, monkeypatch, tmp_path):
        data_dir = tmp_path / "sdk"
        data_dir.mkdir()
        pic = data_dir / "avatar.png"
        pic.write_bytes(b"png")
        adapter = _make_adapter(monkeypatch, tmp_path, data_dir=str(data_dir))
        assert adapter.bot_avatar == pic


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

    def test_send_and_typing_channel_hex(self, monkeypatch, tmp_path):
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
                    result = await adapter.send(CHANNEL_ID, "hello group")
                    assert result.success
                    await adapter.send_typing(CHANNEL_ID)
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.sends == [{"to": CHANNEL_ID, "body": "hello group"}]
            assert sidecar.typing == [{"to": CHANNEL_ID}]
        finally:
            sidecar.stop()

    def test_edit_message_posts_edit_and_keeps_original_id(
        self, monkeypatch, tmp_path
    ):
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
                    result = await adapter.edit_message(
                        PEER_NPUB, "orig-msg-1", "updated text", finalize=True
                    )
                    assert result.success
                    assert result.message_id == "orig-msg-1"
                    assert result.retryable is False
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.edits == [
                {
                    "to": PEER_NPUB,
                    "message_id": "orig-msg-1",
                    "body": "updated text",
                }
            ]
            assert sidecar.edit_headers[0].get("X-Hermes-Sidecar-Token") == token
            assert "evt-kind16-1" in adapter._sent_message_ids
            assert "evt-kind16-1" in adapter._inbound_ids
        finally:
            sidecar.stop()

    def test_edit_message_channel_hex(self, monkeypatch, tmp_path):
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
                    result = await adapter.edit_message(
                        CHANNEL_ID, "orig-group-1", "group edit"
                    )
                    assert result.success
                    assert result.message_id == "orig-group-1"
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.edits == [
                {
                    "to": CHANNEL_ID,
                    "message_id": "orig-group-1",
                    "body": "group edit",
                }
            ]
        finally:
            sidecar.stop()

    def test_edit_message_requires_id_and_body(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._running = True
        adapter._http_client = object()
        empty_id = asyncio.run(adapter.edit_message(PEER_NPUB, "", "text"))
        assert empty_id.success is False
        assert "message id" in (empty_id.error or "")
        empty_body = asyncio.run(adapter.edit_message(PEER_NPUB, "orig-1", ""))
        assert empty_body.success is False
        assert "Empty" in (empty_body.error or "")

    def test_edit_message_requires_running(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        result = asyncio.run(adapter.edit_message(PEER_NPUB, "orig-1", "text"))
        assert result.success is False
        assert result.error == "Not connected"

    def test_edit_message_5xx_is_retryable(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._running = True

        class _Resp:
            status_code = 503
            text = "not ready"

        class _Http:
            async def post(self, url, json=None, headers=None, timeout=None):
                assert url.endswith("/edit")
                return _Resp()

        adapter._http_client = _Http()
        result = asyncio.run(adapter.edit_message(PEER_NPUB, "orig-1", "text"))
        assert result.success is False
        assert result.retryable is True
        assert "503" in (result.error or "")

    def test_delete_message_posts_delete(self, monkeypatch, tmp_path):
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
                    assert await adapter.delete_message(PEER_NPUB, "orig-1") is True
                    assert await adapter.delete_message(CHANNEL_ID, "orig-2") is True
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.deletes == [
                {"to": PEER_NPUB, "message_id": "orig-1"},
                {"to": CHANNEL_ID, "message_id": "orig-2"},
            ]
            assert sidecar.delete_headers[0].get("X-Hermes-Sidecar-Token") == token
        finally:
            sidecar.stop()

    def test_delete_message_empty_id_is_false(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._http_client = object()
        assert asyncio.run(adapter.delete_message(PEER_NPUB, "")) is False

    def test_block_user_posts_block_and_unblock(self, monkeypatch, tmp_path):
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
                    assert await adapter.block_user(PEER_NPUB) is True
                    rows = await adapter.list_blocked()
                    assert rows[0]["npub"] == PEER_NPUB
                    assert await adapter.block_user(PEER_NPUB, unblock=True) is True
                    assert await adapter.list_blocked() == []
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.blocks == [
                {"npub": PEER_NPUB},
                {"npub": PEER_NPUB, "unblock": True},
            ]
            assert sidecar.block_headers[0].get("X-Hermes-Sidecar-Token") == token
        finally:
            sidecar.stop()

    def test_pending_invite_http(self, monkeypatch, tmp_path):
        token = "a" * 64
        community_id = "cc" * 32
        sidecar = MockSidecar(token=token)
        sidecar.pending_invites = [
            {
                "community_id": community_id,
                "name": "Ada's house",
                "inviter_npub": PEER_NPUB,
                "version": 2,
            }
        ]
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = token
            adapter._running = True

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    rows = await adapter.list_pending_invites()
                    assert rows[0]["community_id"] == community_id
                    data = await adapter.accept_invite(community_id)
                    assert data and data["ok"] is True
                    assert await adapter.decline_invite(community_id) is True
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.invite_accepts == [{"community_id": community_id}]
            assert sidecar.invite_declines == [{"community_id": community_id}]
        finally:
            sidecar.stop()

    def test_ensure_home_community_seeds_allowlist(self, monkeypatch, tmp_path):
        token = "a" * 64
        sidecar = MockSidecar(token=token)
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            adapter._sidecar_token = token
            vector_adapter._known_channel_ids.clear()

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    await adapter._ensure_home_community()
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.communities
            assert CHANNEL_ID in vector_adapter._known_channel_ids
        finally:
            sidecar.stop()

    def test_sync_joined_channels_notifies_home(self, monkeypatch, tmp_path):
        token = "a" * 64
        sidecar = MockSidecar(token=token)
        sidecar.listed_communities = [
            {
                "community_id": "cc" * 32,
                "channels": [{"channel_id": CHANNEL_ID, "name": "general"}],
            }
        ]
        port = sidecar.start()
        try:
            adapter = _make_adapter(monkeypatch, tmp_path, bridge_port=port)
            monkeypatch.setenv("VECTOR_HOME_CHANNEL", NPUB)
            adapter._sidecar_token = token
            adapter._running = True

            async def go():
                adapter._http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)
                try:
                    await adapter._sync_joined_channels()
                finally:
                    await adapter._http_client.aclose()

            asyncio.run(go())
            assert sidecar.sends
            assert sidecar.sends[0]["to"] == NPUB
            assert CHANNEL_ID in sidecar.sends[0]["body"]
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

    def test_connect_polls_health_and_sends_token(self, monkeypatch, tmp_path, caplog):
        token = "b" * 64
        nsec_value = "nsec1" + "shouldneverappearinthelogsxxxx"
        sidecar = MockSidecar(token=token, npub=NPUB)
        port = sidecar.start()
        try:
            adapter = _make_adapter(
                monkeypatch, tmp_path, bridge_port=port, startup_timeout=5
            )
            monkeypatch.setenv("VECTOR_NSEC", nsec_value)
            monkeypatch.setattr(
                vector_adapter.secrets, "token_hex", lambda n: token
            )
            caplog.set_level(
                logging.INFO, logger="hermes_plugins.vector_platform.adapter"
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
                if os.name == "posix":
                    assert (record_path.stat().st_mode & 0o777) == 0o600
                truncated = vector_adapter._truncate_npub(NPUB)
                assert truncated in caplog.text
                assert "bot npub" in caplog.text
                assert NPUB not in caplog.text
                assert nsec_value not in caplog.text
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

    def test_sse_commits_last_event_id_after_dispatch(self, monkeypatch, tmp_path):
        """The resume point advances only once an event has been handed off."""
        token = "c" * 64
        sidecar = MockSidecar(token=token, npub=NPUB)
        sidecar.inject_queue.append(
            _message_event(PEER_NPUB, "from-sse", msg_id="sse-77")
        )
        port = sidecar.start()
        try:
            adapter = _make_adapter(
                monkeypatch, tmp_path, bridge_port=port, startup_timeout=5
            )
            monkeypatch.setattr(vector_adapter.secrets, "token_hex", lambda n: token)
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
                assert await adapter.connect() is True
                for _ in range(50):
                    if captured:
                        break
                    await asyncio.sleep(0.05)
                await adapter.disconnect()

            asyncio.run(go())
            assert len(captured) == 1
            assert adapter._sse_last_event_id == "sse-77"
        finally:
            sidecar.stop()

    def test_sse_reconnect_sends_last_event_id(self, monkeypatch, tmp_path):
        """A known resume point is offered to the sidecar so it can replay."""
        token = "c" * 64
        sidecar = MockSidecar(token=token, npub=NPUB)
        port = sidecar.start()
        try:
            adapter = _make_adapter(
                monkeypatch, tmp_path, bridge_port=port, startup_timeout=5
            )
            monkeypatch.setattr(vector_adapter.secrets, "token_hex", lambda n: token)
            adapter._sse_last_event_id = "sse-42"

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
                assert await adapter.connect() is True
                for _ in range(50):
                    if sidecar.events_headers:
                        break
                    await asyncio.sleep(0.05)
                await adapter.disconnect()

            asyncio.run(go())
            assert sidecar.events_headers
            assert sidecar.events_headers[0].get("Last-Event-ID") == "sse-42"
        finally:
            sidecar.stop()

    def test_sse_fresh_connect_omits_last_event_id(self, monkeypatch, tmp_path):
        """No resume point means no header — the sidecar must not replay."""
        token = "c" * 64
        sidecar = MockSidecar(token=token, npub=NPUB)
        port = sidecar.start()
        try:
            adapter = _make_adapter(
                monkeypatch, tmp_path, bridge_port=port, startup_timeout=5
            )
            monkeypatch.setattr(vector_adapter.secrets, "token_hex", lambda n: token)

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
                assert await adapter.connect() is True
                for _ in range(50):
                    if sidecar.events_headers:
                        break
                    await asyncio.sleep(0.05)
                await adapter.disconnect()

            asyncio.run(go())
            assert sidecar.events_headers
            assert "Last-Event-ID" not in sidecar.events_headers[0]
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

    def test_reap_kills_vector_bridge_and_deletes_record(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        rec = tmp_path / "runtime" / "vector-sidecar.json"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            vector_adapter, "_find_listener_pids", lambda _port: [4242]
        )
        monkeypatch.setattr(
            vector_adapter, "_pid_is_vector_bridge", lambda _pid: True
        )
        monkeypatch.setattr(vector_adapter, "_pid_alive", lambda _pid: False)
        killed = []
        monkeypatch.setattr(
            vector_adapter.os, "kill", lambda pid, sig: killed.append((pid, sig))
        )
        asyncio.run(adapter._reap_orphan_sidecar())
        assert killed == [(4242, signal.SIGTERM)]
        assert not rec.exists()

    def test_connect_reaps_when_port_listening(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        listening = {"busy": True}
        reaped = []

        def fake_listening(*_a, **_k):
            return listening["busy"]

        async def fake_reap(self):
            reaped.append(True)
            listening["busy"] = False

        def boom(*_a, **_k):
            raise OSError("stop after reap")

        monkeypatch.setattr(vector_adapter, "bridge_port_is_listening", fake_listening)
        monkeypatch.setattr(
            vector_adapter.VectorAdapter, "_reap_orphan_sidecar", fake_reap
        )
        monkeypatch.setattr(vector_adapter.subprocess, "Popen", boom)
        ok = asyncio.run(adapter.connect())
        assert reaped == [True]
        assert ok is False
        assert adapter.fatal_error_code == "vector_bridge_spawn_failed"


# ---------------------------------------------------------------------------
# Pairing pre-filter, display YAML merge, standalone send, setup wizard
# ---------------------------------------------------------------------------

from types import SimpleNamespace

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class TestPairingHelpers:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("VECTOR_PAIRING", raising=False)
        assert vector_adapter._pairing_enabled() is True

    def test_on_values(self, monkeypatch):
        for val in ("on", "ON", "true", "1", "yes"):
            monkeypatch.setenv("VECTOR_PAIRING", val)
            assert vector_adapter._pairing_enabled() is True, val

    def test_off_values(self, monkeypatch):
        for val in ("off", "OFF", "0", "false", "no", "disabled"):
            monkeypatch.setenv("VECTOR_PAIRING", val)
            assert vector_adapter._pairing_enabled() is False, val

    def test_operator_hex_normalized_into_allowlist(self):
        merged = vector_adapter._merge_allowed_users(NPUB, HEX_PUBKEY)
        assert merged == NPUB

    def test_merge_keeps_other_npubs_after_operator(self):
        merged = vector_adapter._merge_allowed_users(NPUB, PEER_NPUB)
        assert merged.split(",")[0] == NPUB
        assert PEER_NPUB in merged.split(",")

    def test_sender_authorized_via_hex_allowlist(self, monkeypatch):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", HEX_PUBKEY)
        assert vector_adapter._sender_is_authorized(NPUB) is True
        assert vector_adapter._sender_is_authorized(PEER_NPUB) is False


class TestPairingPrefilter:
    def test_off_drops_unauthorized_before_handle_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_PAIRING", "off")
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", NPUB)
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "stranger", msg_id="unauth-1")
            )
        )
        assert captured == []

    def test_off_allows_allowlisted_sender(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_PAIRING", "off")
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_HEX)
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "hello-op", msg_id="auth-1")
            )
        )
        assert len(captured) == 1
        assert captured[0].text == "hello-op"

    def test_on_forwards_unauthorized_for_pairing_code(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_PAIRING", "on")
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", NPUB)
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "please-pair", msg_id="pair-1")
            )
        )
        assert len(captured) == 1
        assert captured[0].source.chat_id == PEER_NPUB


class TestSupersededReplay:
    """A reconnect burst must cost one turn per chat, not one per message."""

    def test_superseded_replay_files_context_without_a_turn(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("VECTOR_PAIRING", "off")
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_HEX)
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []
        breadcrumbs = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        monkeypatch.setattr(
            vector_adapter.VectorAdapter,
            "_append_session_breadcrumb",
            lambda self, source, content: breadcrumbs.append(content),
        )

        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "are you there?",
                    msg_id="sup-1",
                    replayed=True,
                    superseded=True,
                )
            )
        )

        assert captured == [], "superseded replay must not start an agent turn"
        assert len(breadcrumbs) == 1
        assert "are you there?" in breadcrumbs[0]
        assert "context only" in breadcrumbs[0]

    def test_newest_replayed_message_still_runs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_PAIRING", "off")
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_HEX)
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]

        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB, "so what do you think?", msg_id="sup-2", replayed=True
                )
            )
        )

        assert len(captured) == 1
        assert captured[0].text == "so what do you think?"

    def test_replayed_without_superseded_flag_is_a_normal_turn(
        self, monkeypatch, tmp_path
    ):
        """`superseded` alone (no `replayed`) must not suppress a live message."""
        monkeypatch.setenv("VECTOR_PAIRING", "off")
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_HEX)
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]

        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "live", msg_id="sup-3", superseded=True)
            )
        )

        assert len(captured) == 1

    def test_superseded_reaction_replay_is_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_PAIRING", "off")
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_HEX)
        adapter = _make_adapter(monkeypatch, tmp_path)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        adapter._sent_message_ids["target-1"] = None

        asyncio.run(
            adapter._handle_message_update(
                {
                    "id": "target-1",
                    "chat_id": PEER_NPUB,
                    "npub": PEER_NPUB,
                    "mine": True,
                    "text": "bot said this",
                    "reactions": [
                        {"id": "r1", "author_id": PEER_NPUB, "emoji": "👍"}
                    ],
                    "replayed": True,
                    "superseded": True,
                }
            )
        )

        assert captured == []


class TestDisplayYamlMerge:
    def test_preserves_other_config(self, tmp_path):
        if yaml is None:
            return
        path = tmp_path / "config.yaml"
        path.write_text(
            "model:\n  default: foo\n"
            "display:\n  tool_progress: all\n  platforms:\n"
            "    telegram:\n      tool_progress: all\n      streaming: true\n"
            "other: 1\n",
            encoding="utf-8",
        )
        assert vector_adapter._merge_vector_display_config(path) is True
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["model"]["default"] == "foo"
        assert data["other"] == 1
        assert data["display"]["tool_progress"] == "all"
        assert data["display"]["platforms"]["telegram"]["tool_progress"] == "all"
        assert data["display"]["platforms"]["telegram"]["streaming"] is True
        assert data["display"]["platforms"]["vector"]["tool_progress"] == "new"
        assert data["display"]["platforms"]["vector"]["interim_assistant_messages"] is False
        assert data["display"]["platforms"]["vector"]["long_running_notifications"] is False
        assert data["display"]["platforms"]["vector"]["busy_ack_detail"] is False
        assert data["display"]["platforms"]["vector"]["streaming"] is False

    def test_writes_vector_platform_block(self, tmp_path):
        if yaml is None:
            return
        path = tmp_path / "config.yaml"
        path.write_text("model:\n  default: foo\n", encoding="utf-8")
        platform = {
            "bot": {"name": "Ada"},
            "unauthorized_dm_behavior": "ignore",
            "communities": {"create": True, "name": "Hermes"},
        }
        assert vector_adapter._merge_vector_display_config(path, platform) is True
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["model"]["default"] == "foo"
        assert data["vector"]["bot"]["name"] == "Ada"
        assert data["vector"]["unauthorized_dm_behavior"] == "ignore"
        assert data["vector"]["communities"]["create"] is True

    def test_creates_file_when_missing(self, tmp_path):
        if yaml is None:
            return
        path = tmp_path / "nested" / "config.yaml"
        assert vector_adapter._merge_vector_display_config(path) is True
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["display"]["platforms"]["vector"]["tool_progress"] == "new"
        assert data["display"]["platforms"]["vector"]["interim_assistant_messages"] is False
        assert data["display"]["platforms"]["vector"]["long_running_notifications"] is False
        assert data["display"]["platforms"]["vector"]["busy_ack_detail"] is False
        assert data["display"]["platforms"]["vector"]["streaming"] is False

    def test_preserves_comments_when_ruamel_available(self, tmp_path):
        if yaml is None:
            return
        try:
            from ruamel.yaml import YAML  # noqa: F401
        except ImportError:
            return
        path = tmp_path / "config.yaml"
        path.write_text(
            "# keep me\n"
            "model:\n  default: foo  # model comment\n"
            "display:\n  tool_progress: all\n",
            encoding="utf-8",
        )
        assert vector_adapter._merge_vector_display_config(path) is True
        text = path.read_text(encoding="utf-8")
        assert "# keep me" in text
        assert "model comment" in text
        data = yaml.safe_load(text)
        assert data["model"]["default"] == "foo"
        assert data["display"]["tool_progress"] == "all"
        assert data["display"]["platforms"]["vector"]["tool_progress"] == "new"

    def test_refuses_unparseable(self, tmp_path):
        path = tmp_path / "config.yaml"
        original = "this: [is: not: yaml: {{{"
        path.write_text(original, encoding="utf-8")
        assert vector_adapter._merge_vector_display_config(path) is False
        assert path.read_text(encoding="utf-8") == original

    def test_refuses_non_mapping_root(self, tmp_path):
        path = tmp_path / "config.yaml"
        original = "- just a list\n"
        path.write_text(original, encoding="utf-8")
        assert vector_adapter._merge_vector_display_config(path) is False
        assert path.read_text(encoding="utf-8") == original

    def test_clears_vector_keys_when_none(self, tmp_path):
        if yaml is None:
            return
        path = tmp_path / "config.yaml"
        path.write_text(
            "vector:\n  unauthorized_dm_behavior: ignore\n  bot:\n    name: Ada\n",
            encoding="utf-8",
        )
        assert (
            vector_adapter._merge_vector_display_config(
                path,
                {
                    "bot": {"name": "Ada"},
                    "unauthorized_dm_behavior": None,
                    "communities": None,
                },
            )
            is True
        )
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["vector"]["bot"]["name"] == "Ada"
        assert "unauthorized_dm_behavior" not in data["vector"]
        assert "communities" not in data["vector"]


class TestApplyYamlConfig:
    def test_bridges_bot_and_communities(self, monkeypatch):
        keys = (
            "VECTOR_BOT_NAME",
            "VECTOR_BOT_ABOUT",
            "VECTOR_CREATE_COMMUNITY",
            "VECTOR_GROUP_ALLOW_ALL",
            "VECTOR_GROUP_ALLOWED_USERS",
            "VECTOR_PAIRING",
            "VECTOR_REACTIONS",
        )
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ.pop(k, None)
            seeded = vector_adapter._apply_yaml_config(
                {},
                {
                    "bot": {"name": "Ada", "about": "bot"},
                    "unauthorized_dm_behavior": "ignore",
                    "reactions": True,
                    "communities": {
                        "create": True,
                        "open_channels": [CHANNEL_ID],
                        "group_allowed_users": [PEER_NPUB],
                    },
                },
            )
            assert seeded["bot_name"] == "Ada"
            assert seeded["create_community"] == "on"
            assert seeded["group_allowed_chats"] == CHANNEL_ID
            assert seeded["group_allowed_users"] == PEER_NPUB
            assert os.environ["VECTOR_BOT_NAME"] == "Ada"
            assert os.environ["VECTOR_PAIRING"] == "off"
            assert os.environ["VECTOR_REACTIONS"] == "on"
            assert os.environ["VECTOR_CREATE_COMMUNITY"] == "on"
            assert os.environ["VECTOR_GROUP_ALLOW_ALL"] == CHANNEL_ID
        finally:
            for k in keys:
                if saved[k] is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = saved[k]

    def test_env_wins_over_yaml(self, monkeypatch):
        monkeypatch.setenv("VECTOR_BOT_NAME", "FromEnv")
        seeded = vector_adapter._apply_yaml_config({}, {"bot": {"name": "FromYaml"}})
        assert seeded["bot_name"] == "FromYaml"
        assert os.environ["VECTOR_BOT_NAME"] == "FromEnv"

    def test_replay_block_seeds_env(self, monkeypatch):
        monkeypatch.delenv("VECTOR_SSE_REPLAY_MAX", raising=False)
        monkeypatch.delenv("VECTOR_SSE_REPLAY_MAX_AGE_SECS", raising=False)
        seeded = vector_adapter._apply_yaml_config(
            {}, {"replay": {"max_messages": 8, "max_age_secs": 120}}
        )
        assert seeded["replay_max"] == "8"
        assert seeded["replay_max_age_secs"] == "120"
        assert os.environ["VECTOR_SSE_REPLAY_MAX"] == "8"
        assert os.environ["VECTOR_SSE_REPLAY_MAX_AGE_SECS"] == "120"

    def test_replay_zero_is_preserved(self, monkeypatch):
        """`0` disables replay / removes the age limit, so it must not be
        treated as an empty value."""
        monkeypatch.delenv("VECTOR_SSE_REPLAY_MAX", raising=False)
        monkeypatch.delenv("VECTOR_SSE_REPLAY_MAX_AGE_SECS", raising=False)
        seeded = vector_adapter._apply_yaml_config(
            {}, {"replay": {"max_messages": 0, "max_age_secs": 0}}
        )
        assert seeded["replay_max"] == "0"
        assert seeded["replay_max_age_secs"] == "0"
        assert os.environ["VECTOR_SSE_REPLAY_MAX"] == "0"

        env: dict = {}
        vector_adapter._overlay_sidecar_extra_env(env, seeded)
        assert env["VECTOR_SSE_REPLAY_MAX"] == "0", "0 must reach the sidecar"
        assert env["VECTOR_SSE_REPLAY_MAX_AGE_SECS"] == "0"

    def test_replay_env_wins_over_yaml(self, monkeypatch):
        monkeypatch.setenv("VECTOR_SSE_REPLAY_MAX", "99")
        seeded = vector_adapter._apply_yaml_config({}, {"replay": {"max_messages": 8}})
        assert seeded["replay_max"] == "8"
        assert os.environ["VECTOR_SSE_REPLAY_MAX"] == "99"

        env = {"VECTOR_SSE_REPLAY_MAX": "99"}
        vector_adapter._overlay_sidecar_extra_env(env, seeded)
        assert env["VECTOR_SSE_REPLAY_MAX"] == "99"

    def test_replay_rejects_non_counts(self, monkeypatch):
        """A typo falls back to the documented default instead of silently
        disabling replay. `false` in particular must not read as 0."""
        for bad in ("soon", False, True, -1, "", None, 1.5, "10 minutes"):
            monkeypatch.delenv("VECTOR_SSE_REPLAY_MAX", raising=False)
            seeded = vector_adapter._apply_yaml_config(
                {}, {"replay": {"max_messages": bad}}
            )
            assert (seeded or {}).get("replay_max") is None, f"accepted {bad!r}"
            assert "VECTOR_SSE_REPLAY_MAX" not in os.environ, f"set env for {bad!r}"

    def test_replay_absent_keys_are_untouched(self, monkeypatch):
        monkeypatch.delenv("VECTOR_SSE_REPLAY_MAX", raising=False)
        monkeypatch.delenv("VECTOR_SSE_REPLAY_MAX_AGE_SECS", raising=False)
        seeded = vector_adapter._apply_yaml_config({}, {"replay": {"max_messages": 4}})
        assert seeded["replay_max"] == "4"
        assert "replay_max_age_secs" not in seeded
        assert "VECTOR_SSE_REPLAY_MAX_AGE_SECS" not in os.environ

    def test_prebuilt_block_seeds_extra_not_env(self, monkeypatch):
        monkeypatch.delenv("VECTOR_BRIDGE_RELEASE_REPO", raising=False)
        monkeypatch.delenv("VECTOR_BRIDGE_RELEASE_TAG", raising=False)
        monkeypatch.delenv("VECTOR_BRIDGE_SKIP_DOWNLOAD", raising=False)
        seeded = vector_adapter._apply_yaml_config(
            {},
            {
                "prebuilt": {
                    "download": False,
                    "repo": "Acme/vector-fork",
                    "tag": "0.9.0",
                }
            },
        )
        assert seeded["prebuilt_download"] == "off"
        assert seeded["prebuilt_repo"] == "Acme/vector-fork"
        assert seeded["prebuilt_tag"] == "v0.9.0"
        assert "VECTOR_BRIDGE_RELEASE_REPO" not in os.environ
        assert "VECTOR_BRIDGE_RELEASE_TAG" not in os.environ
        assert "VECTOR_BRIDGE_SKIP_DOWNLOAD" not in os.environ

    def test_prebuilt_rejects_malformed_repo(self, monkeypatch):
        seeded = vector_adapter._apply_yaml_config(
            {}, {"prebuilt": {"repo": "../evil/repo"}}
        )
        assert (seeded or {}).get("prebuilt_repo") is None


class TestStandaloneSend:
    def test_reads_runtime_record_and_sends_token(self, monkeypatch, tmp_path):
        token = "e" * 64
        sidecar = MockSidecar(token=token)
        port = sidecar.start()
        try:
            monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
            rec_path = tmp_path / "runtime" / "vector-sidecar.json"
            rec_path.parent.mkdir(parents=True, exist_ok=True)
            rec_path.write_text(
                json.dumps(
                    {
                        "port": port,
                        "token": token,
                        "pid": os.getpid(),
                        "npub": NPUB,
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(rec_path, 0o600)
            pconfig = MagicMock()
            pconfig.extra = {}
            result = asyncio.run(
                vector_adapter._standalone_send(pconfig, PEER_NPUB, "cron-hi")
            )
            assert result.get("success") is True
            assert result.get("platform") == "vector"
            assert result.get("chat_id") == PEER_NPUB
            assert sidecar.sends == [{"to": PEER_NPUB, "body": "cron-hi"}]
            assert sidecar.send_headers[0].get("X-Hermes-Sidecar-Token") == token
        finally:
            sidecar.stop()

    def _write_live_record(self, tmp_path, port, token):
        rec_path = tmp_path / "runtime" / "vector-sidecar.json"
        rec_path.parent.mkdir(parents=True, exist_ok=True)
        rec_path.write_text(
            json.dumps(
                {"port": port, "token": token, "pid": os.getpid(), "npub": NPUB}
            ),
            encoding="utf-8",
        )
        return rec_path

    def test_media_only_is_error(self, monkeypatch, tmp_path):
        token = "e" * 64
        sidecar = MockSidecar(token=token)
        port = sidecar.start()
        try:
            monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
            self._write_live_record(tmp_path, port, token)
            pconfig = MagicMock()
            pconfig.extra = {}
            result = asyncio.run(
                vector_adapter._standalone_send(
                    pconfig, PEER_NPUB, "", media_files=["/tmp/file.png"]
                )
            )
            assert "error" in result
            assert "media" in result["error"].lower()
            assert sidecar.sends == []
        finally:
            sidecar.stop()

    def test_text_plus_media_warns_not_silent_success(self, monkeypatch, tmp_path):
        token = "e" * 64
        sidecar = MockSidecar(token=token)
        port = sidecar.start()
        try:
            monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
            self._write_live_record(tmp_path, port, token)
            pconfig = MagicMock()
            pconfig.extra = {}
            result = asyncio.run(
                vector_adapter._standalone_send(
                    pconfig, PEER_NPUB, "cron-hi", media_files=["/tmp/file.png"]
                )
            )
            assert result.get("success") is True
            assert "warning" in result
            assert "ignored" in result["warning"].lower()
            assert sidecar.sends == [{"to": PEER_NPUB, "body": "cron-hi"}]
        finally:
            sidecar.stop()

    def test_missing_record_errors(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        pconfig = MagicMock()
        pconfig.extra = {}
        result = asyncio.run(
            vector_adapter._standalone_send(pconfig, PEER_NPUB, "nope")
        )
        assert "error" in result
        assert "running sidecar" in result["error"]

    def test_stale_pid_errors(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(vector_adapter, "_sidecar_pid_alive", lambda _pid: False)
        rec_path = tmp_path / "runtime" / "vector-sidecar.json"
        rec_path.parent.mkdir(parents=True, exist_ok=True)
        rec_path.write_text(
            json.dumps({"port": 8096, "token": "f" * 64, "pid": 999999}),
            encoding="utf-8",
        )
        pconfig = MagicMock()
        pconfig.extra = {}
        result = asyncio.run(
            vector_adapter._standalone_send(pconfig, PEER_NPUB, "stale")
        )
        assert "error" in result
        assert "stale" in result["error"].lower() or "down" in result["error"].lower()


class TestWizardHelpers:
    def test_parse_rustc_version(self):
        assert vector_adapter._parse_rustc_version(
            "rustc 1.75.0 (82e1608df 2023-12-21)"
        ) == (1, 75)
        assert vector_adapter._parse_rustc_version("rustc 1.85.0-nightly") == (1, 85)
        assert vector_adapter._parse_rustc_version("not rust") is None
        assert vector_adapter._parse_rustc_version(
            "rustc 1.74.0"
        ) < vector_adapter.MIN_RUSTC

    def test_parse_bridge_json(self):
        stdout = "noise\n{\"status\": \"existing\", \"npub\": \"%s\"}\n" % NPUB
        data = vector_adapter._parse_bridge_json(stdout)
        assert data["status"] == "existing"
        assert data["npub"] == NPUB
        assert vector_adapter._parse_bridge_json("no json here") is None
        err = vector_adapter._parse_bridge_json(
            '{"error": "not a valid nsec", "code": "invalid_nsec"}'
        )
        assert err["code"] == "invalid_nsec"

    def test_normalize_identity_choice(self):
        assert vector_adapter._normalize_identity_choice("create") == "create"
        assert vector_adapter._normalize_identity_choice("C") == "create"
        assert vector_adapter._normalize_identity_choice("nsec") == "nsec"
        assert vector_adapter._normalize_identity_choice("mnemonic") == "mnemonic"
        assert vector_adapter._normalize_identity_choice("nope") is None

    def test_bridge_cli_env_strips_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_NSEC", "nsec1secret")
        monkeypatch.setenv("VECTOR_MNEMONIC", "abandon " * 12)
        monkeypatch.setenv("VECTOR_STUB", "1")
        monkeypatch.setenv("VECTOR_SIDECAR_TOKEN", "tok")
        env = vector_adapter._bridge_cli_env(tmp_path)
        assert env["VECTOR_DATA_DIR"] == str(tmp_path)
        assert "VECTOR_NSEC" not in env
        assert "VECTOR_MNEMONIC" not in env
        assert "VECTOR_STUB" not in env
        assert "VECTOR_SIDECAR_TOKEN" not in env

    def test_write_temp_secret_is_0600(self, tmp_path):
        path = vector_adapter._write_temp_secret("nsec1testsecret", tmp_path)
        try:
            assert path.is_file()
            assert path.parent == tmp_path
            assert path.read_text(encoding="utf-8").strip() == "nsec1testsecret"
            if os.name == "posix":
                assert (path.stat().st_mode & 0o777) == 0o600
        finally:
            vector_adapter._shred_unlink(path)
            assert not path.exists()

    def test_backup_identity_roundtrip(self, tmp_path):
        nsec = tmp_path / "identity.nsec"
        nsec.write_text("nsec1original\n")
        bak = vector_adapter._backup_identity_nsec(tmp_path)
        assert bak is not None
        assert not nsec.exists()
        assert bak.read_text() == "nsec1original\n"
        vector_adapter._restore_identity_nsec(tmp_path, bak)
        assert nsec.read_text() == "nsec1original\n"
        assert not bak.exists()

    def test_backup_identity_includes_mnemonic(self, tmp_path):
        nsec = tmp_path / "identity.nsec"
        mnemonic = tmp_path / "identity.mnemonic"
        nsec.write_text("nsec1original\n")
        mnemonic.write_text("abandon " * 11 + "about\n")
        baks = vector_adapter._backup_identity(tmp_path)
        assert not nsec.exists()
        assert not mnemonic.exists()
        assert {p.name for p in baks} == {
            "identity.nsec.bak",
            "identity.mnemonic.bak",
        }
        for bak in baks:
            vector_adapter._restore_identity_backup(bak)
        assert nsec.read_text() == "nsec1original\n"
        assert mnemonic.read_text() == "abandon " * 11 + "about\n"
        assert not (tmp_path / "identity.nsec.bak").exists()
        assert not (tmp_path / "identity.mnemonic.bak").exists()

    def test_ensure_bridge_binary_skips_cargo_when_present(self, monkeypatch, tmp_path):
        fake = tmp_path / "vector-bridge"
        fake.write_text("")
        monkeypatch.setattr(vector_adapter, "resolve_bridge_bin", lambda: fake)
        cargo_calls = []
        monkeypatch.setattr(
            vector_adapter.subprocess,
            "run",
            lambda *a, **k: cargo_calls.append((a, k)) or MagicMock(),
        )
        io = SimpleNamespace(print_info=lambda *_a, **_k: None, print_error=lambda *_a, **_k: None)
        assert vector_adapter._ensure_bridge_binary(io) == fake
        assert cargo_calls == []

    def test_ensure_bridge_binary_hints_when_cargo_missing(self, monkeypatch, tmp_path):
        missing = tmp_path / "no-bridge"
        monkeypatch.setattr(vector_adapter, "resolve_bridge_bin", lambda: missing)
        monkeypatch.delenv("VECTOR_BRIDGE_BIN", raising=False)
        monkeypatch.setattr(
            vector_adapter, "_try_install_prebuilt_bridge", lambda _io: None
        )
        monkeypatch.setattr(vector_adapter.shutil, "which", lambda _name: None)
        errors = []
        io = SimpleNamespace(
            print_info=lambda *_a, **_k: None,
            print_error=lambda msg, *a, **k: errors.append(msg),
            print_success=lambda *_a, **_k: None,
        )
        assert vector_adapter._ensure_bridge_binary(io) is None
        assert errors
        assert "cargo" in errors[0].lower() or "rustup" in errors[0].lower()

    def test_ensure_bridge_binary_downloads_before_cargo(self, monkeypatch, tmp_path):
        missing = tmp_path / "no-bridge"
        downloaded = tmp_path / "downloaded"
        downloaded.write_text("ok")
        monkeypatch.setattr(vector_adapter, "resolve_bridge_bin", lambda: missing)
        monkeypatch.delenv("VECTOR_BRIDGE_BIN", raising=False)
        monkeypatch.setattr(
            vector_adapter, "_try_install_prebuilt_bridge", lambda _io: downloaded
        )
        cargo_calls = []
        monkeypatch.setattr(
            vector_adapter.subprocess,
            "run",
            lambda *a, **k: cargo_calls.append((a, k)) or MagicMock(),
        )
        io = SimpleNamespace(
            print_info=lambda *_a, **_k: None,
            print_error=lambda *_a, **_k: None,
            print_success=lambda *_a, **_k: None,
        )
        assert vector_adapter._ensure_bridge_binary(io) == downloaded
        assert cargo_calls == []

    def test_try_install_skips_when_yaml_download_false(self, monkeypatch):
        monkeypatch.setattr(
            vector_adapter,
            "_read_vector_yaml_block",
            lambda: {"prebuilt": {"download": False}},
        )
        io = SimpleNamespace(
            print_info=lambda *_a, **_k: None,
            print_error=lambda *_a, **_k: None,
            print_success=lambda *_a, **_k: None,
        )
        assert vector_adapter._try_install_prebuilt_bridge(io) is None

    def test_try_install_writes_verified_binary(self, monkeypatch, tmp_path):
        payload = b"sidecar-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        asset = "vector-bridge-x86_64-unknown-linux-gnu"
        dest_dir = tmp_path / "bin"
        monkeypatch.setattr(vector_adapter, "_read_vector_yaml_block", lambda: {})
        monkeypatch.setattr(
            vector_adapter,
            "bridge_release_target",
            lambda: "x86_64-unknown-linux-gnu",
        )
        monkeypatch.setattr(vector_adapter, "_prebuilt_bin_dir", lambda: dest_dir)
        monkeypatch.setattr(vector_adapter, "_release_tag", lambda: "v0.4.0")
        monkeypatch.setattr(vector_adapter.sys, "platform", "linux")

        def fake_get(url, *, max_bytes=None):
            if url.endswith("SHA256SUMS"):
                return f"{digest}  {asset}\n".encode()
            if url.endswith(asset):
                return payload
            raise AssertionError(url)

        monkeypatch.setattr(vector_adapter, "_http_get_bytes", fake_get)
        io = SimpleNamespace(
            print_info=lambda *_a, **_k: None,
            print_error=lambda *_a, **_k: None,
            print_success=lambda *_a, **_k: None,
        )
        path = vector_adapter._try_install_prebuilt_bridge(io)
        assert path == dest_dir / "vector-bridge"
        assert path.read_bytes() == payload
        assert path.stat().st_mode & 0o111
        assert (dest_dir / ".version").read_text(encoding="utf-8").strip() == "v0.4.0"

    def test_try_install_rejects_bad_checksum(self, monkeypatch, tmp_path):
        payload = b"sidecar-bytes"
        dest_dir = tmp_path / "bin"
        monkeypatch.setattr(vector_adapter, "_read_vector_yaml_block", lambda: {})
        monkeypatch.setattr(
            vector_adapter,
            "bridge_release_target",
            lambda: "x86_64-unknown-linux-gnu",
        )
        monkeypatch.setattr(vector_adapter, "_prebuilt_bin_dir", lambda: dest_dir)
        monkeypatch.setattr(vector_adapter, "_release_tag", lambda: "v0.4.0")

        def fake_get(url, *, max_bytes=None):
            if url.endswith("SHA256SUMS"):
                return f"{'c' * 64}  vector-bridge-x86_64-unknown-linux-gnu\n".encode()
            return payload

        monkeypatch.setattr(vector_adapter, "_http_get_bytes", fake_get)
        errors = []
        io = SimpleNamespace(
            print_info=lambda *_a, **_k: None,
            print_error=lambda msg, *a, **k: errors.append(msg),
            print_success=lambda *_a, **_k: None,
        )
        assert vector_adapter._try_install_prebuilt_bridge(io) is None
        assert any("checksum" in e.lower() for e in errors)
        assert not (dest_dir / "vector-bridge").exists()


def _fake_setup_io(*, prompts=None, yes_no=None, env=None):
    saved: dict = {}
    env_map = dict(env or {})

    def prompt(question, default=None, password=False):
        for key, val in (prompts or {}).items():
            if key in question:
                return default if val is None else val
        return default or ""

    def prompt_yes_no(question, default=True):
        for key, val in (yes_no or {}).items():
            if key in question:
                return val
        return default

    def get_env_value(key):
        return env_map.get(key)

    def save_env_value(key, value):
        saved[key] = value
        env_map[key] = value

    logs = {"info": [], "warn": [], "err": [], "ok": [], "header": []}
    return SimpleNamespace(
        prompt=prompt,
        prompt_yes_no=prompt_yes_no,
        get_env_value=get_env_value,
        save_env_value=save_env_value,
        print_header=lambda m: logs["header"].append(m),
        print_info=lambda m: logs["info"].append(m),
        print_warning=lambda m: logs["warn"].append(m),
        print_success=lambda m: logs["ok"].append(m),
        print_error=lambda m: logs["err"].append(m),
        saved=saved,
        logs=logs,
    )


class TestInteractiveSetup:
    def test_create_normalizes_operator_npub_and_writes_env(
        self, monkeypatch, tmp_path
    ):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(
            vector_adapter, "resolve_data_dir", lambda: tmp_path / "sdk"
        )
        cli_calls = []

        def fake_cli(_bin, _data, args, timeout=60):
            cli_calls.append(list(args))
            if "--check" in args:
                return {"status": "not_registered"}, 0, ""
            if "--setup" in args:
                assert "--nsec-file" not in args
                assert "--mnemonic-file" not in args
                sdk = tmp_path / "sdk"
                sdk.mkdir(parents=True, exist_ok=True)
                (sdk / "identity.mnemonic").write_text(
                    "abandon abandon abandon abandon abandon abandon "
                    "abandon abandon abandon abandon abandon about\n"
                )
                return {"status": "created", "npub": NPUB}, 0, ""
            return None, 1, "unexpected"

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            prompts={
                "Identity [create / nsec / mnemonic]": "create",
                "Bot display name": "Hermes",
                "Your Vector npub": HEX_PUBKEY,
            },
            yes_no={"Enable pairing codes": True},
        )
        vector_adapter._run_interactive_setup(io)
        assert io.saved["VECTOR_NPUB"] == NPUB
        assert io.saved["VECTOR_HOME_CHANNEL"] == NPUB
        assert io.saved["VECTOR_ALLOWED_USERS"] == NPUB
        assert "VECTOR_PAIRING" not in io.saved
        assert "VECTOR_CREATE_COMMUNITY" not in io.saved
        assert "VECTOR_BOT_NAME" not in io.saved
        assert "VECTOR_BOT_ABOUT" not in io.saved
        assert "VECTOR_DATA_DIR" not in io.saved
        assert "VECTOR_NSEC" not in io.saved
        assert "VECTOR_MNEMONIC" not in io.saved
        assert cli_calls[0] == ["--check"]
        assert cli_calls[1] == ["--setup"]
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["display"]["platforms"]["vector"]["tool_progress"] == "new"
        assert cfg["display"]["platforms"]["vector"]["interim_assistant_messages"] is False
        assert cfg["vector"]["bot"]["name"] == "Hermes"
        assert "unauthorized_dm_behavior" not in cfg.get("vector", {})
        assert "communities" not in cfg.get("vector", {})
        assert any("Share this npub" in m for m in io.logs["info"])
        assert any("identity.mnemonic" in m for m in io.logs["info"])

    def test_setup_copies_avatar_into_data_dir(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        data_dir = tmp_path / "sdk"
        monkeypatch.setattr(vector_adapter, "resolve_data_dir", lambda: data_dir)
        src = tmp_path / "me.jpg"
        src.write_bytes(b"jpeg")

        def fake_cli(_bin, _data, args, timeout=60):
            if "--check" in args:
                return {"status": "not_registered"}, 0, ""
            if "--setup" in args:
                return {"status": "created", "npub": NPUB}, 0, ""
            return None, 1, "unexpected"

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            prompts={
                "Identity [create / nsec / mnemonic]": "create",
                "Bot display name": "Hermes",
                "Bot about text": "Public bio",
                "Bot avatar image path": str(src),
                "Bot banner image path": str(src),
                "Your Vector npub": HEX_PUBKEY,
            },
            yes_no={"Enable pairing codes": True},
        )
        vector_adapter._run_interactive_setup(io)
        dest = data_dir / "avatar.jpg"
        banner = data_dir / "banner.jpg"
        assert dest.is_file()
        assert dest.read_bytes() == b"jpeg"
        assert banner.is_file()
        assert banner.read_bytes() == b"jpeg"
        assert "VECTOR_BOT_AVATAR" not in io.saved
        assert "VECTOR_BOT_BANNER" not in io.saved
        assert "VECTOR_BOT_NAME" not in io.saved
        assert "VECTOR_BOT_ABOUT" not in io.saved
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["vector"]["bot"]["name"] == "Hermes"
        assert cfg["vector"]["bot"]["about"] == "Public bio"

    def test_import_nsec_uses_temp_0600_file_not_env(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(
            vector_adapter, "resolve_data_dir", lambda: tmp_path / "sdk"
        )
        seen_files = []

        def fake_cli(_bin, _data, args, timeout=60):
            if "--check" in args:
                return {"status": "not_registered"}, 0, ""
            if "--nsec-file" in args:
                idx = args.index("--nsec-file")
                path = Path(args[idx + 1])
                seen_files.append(path)
                assert path.is_file()
                if os.name == "posix":
                    assert (path.stat().st_mode & 0o777) == 0o600
                assert "nsec1imported" in path.read_text(encoding="utf-8")
                return {"status": "restored", "npub": NPUB}, 0, ""
            return None, 1, "missing nsec-file"

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            prompts={
                "Identity [create / nsec / mnemonic]": "nsec",
                "nsec (nsec1": "nsec1imported",
                "Bot display name": "Hermes",
                "Your Vector npub": f"nostr:{PEER_NPUB}",
            },
            yes_no={
                "Import this identity as the Hermes bot?": True,
                "Enable pairing codes": False,
            },
        )
        vector_adapter._run_interactive_setup(io)
        assert seen_files
        assert seen_files[0].parent == tmp_path / "sdk"
        assert not seen_files[0].exists()
        assert io.saved["VECTOR_HOME_CHANNEL"] == PEER_NPUB
        assert io.saved["VECTOR_ALLOWED_USERS"] == PEER_NPUB
        assert "VECTOR_PAIRING" not in io.saved
        assert "VECTOR_NSEC" not in io.saved
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["vector"]["unauthorized_dm_behavior"] == "ignore"

    def test_existing_identity_skips_create_import_unless_confirmed(
        self, monkeypatch, tmp_path
    ):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(
            vector_adapter, "resolve_data_dir", lambda: tmp_path / "sdk"
        )
        cli_calls = []

        def fake_cli(_bin, _data, args, timeout=60):
            cli_calls.append(list(args))
            if "--check" in args:
                return {"status": "existing", "npub": NPUB}, 0, ""
            if "--setup" in args:
                assert "--nsec-file" not in args
                return {"status": "existing", "npub": NPUB}, 0, ""
            return None, 1, "unexpected"

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            prompts={
                "Identity [create / nsec / mnemonic]": "nsec",
                "nsec (nsec1": "nsec1shouldnotbeused",
                "Bot display name": "Hermes",
                "Your Vector npub": PEER_NPUB,
            },
            yes_no={
                "Reconfigure identity anyway?": False,
                "Enable pairing codes": True,
            },
        )
        vector_adapter._run_interactive_setup(io)
        assert cli_calls[0] == ["--check"]
        assert cli_calls[1] == ["--setup"]
        assert io.saved["VECTOR_NPUB"] == NPUB
        assert io.saved["VECTOR_HOME_CHANNEL"] == PEER_NPUB

    def test_already_configured_can_skip(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(
            vector_adapter,
            "_ensure_bridge_binary",
            lambda _io: called.append("build") or tmp_path / "x",
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        io = _fake_setup_io(
            env={"VECTOR_NPUB": NPUB},
            yes_no={"Reconfigure Vector?": False},
        )
        vector_adapter._run_interactive_setup(io)
        assert called == []
        assert io.saved == {}
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["display"]["platforms"]["vector"]["tool_progress"] == "new"
        assert cfg["display"]["platforms"]["vector"]["interim_assistant_messages"] is False

    def test_skip_reconfigure_still_adopts_stale_bak(self, monkeypatch, tmp_path):
        called = []
        data_dir = tmp_path / "sdk"
        data_dir.mkdir()
        bak = data_dir / "identity.nsec.bak"
        bak.write_text("nsec1original\n")
        monkeypatch.setattr(
            vector_adapter,
            "_ensure_bridge_binary",
            lambda _io: called.append("build") or tmp_path / "x",
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(vector_adapter, "resolve_data_dir", lambda: data_dir)
        io = _fake_setup_io(
            env={"VECTOR_NPUB": NPUB},
            yes_no={"Reconfigure Vector?": False},
        )
        vector_adapter._run_interactive_setup(io)
        assert called == []
        assert (data_dir / "identity.nsec").read_text() == "nsec1original\n"
        assert not bak.exists()
        assert any("identity.nsec.bak" in m for m in io.logs["warn"])

    def test_operator_npub_required(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(
            vector_adapter,
            "_run_bridge_cli",
            lambda *_a, **_k: ({"status": "not_registered"}, 0, ""),
        )
        io = _fake_setup_io(
            prompts={
                "Identity [create / nsec / mnemonic]": "create",
                "Bot display name": "Hermes",
                "Your Vector npub": "not-an-npub",
            },
        )
        vector_adapter._run_interactive_setup(io)
        assert io.saved == {}
        assert any("required" in m.lower() for m in io.logs["err"])

    def test_import_requires_bot_warning_yes(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(
            vector_adapter,
            "_run_bridge_cli",
            lambda *_a, **_k: ({"status": "not_registered"}, 0, ""),
        )
        io = _fake_setup_io(
            prompts={"Identity [create / nsec / mnemonic]": "nsec"},
            yes_no={"Import this identity as the Hermes bot?": False},
        )
        vector_adapter._run_interactive_setup(io)
        assert io.saved == {}
        assert any("tagged as a bot" in m for m in io.logs["warn"])

    def test_reconfigure_restores_identity_on_setup_failure(
        self, monkeypatch, tmp_path
    ):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        data_dir = tmp_path / "sdk"
        data_dir.mkdir()
        nsec = data_dir / "identity.nsec"
        nsec.write_text("nsec1original\n")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(vector_adapter, "resolve_data_dir", lambda: data_dir)

        def fake_cli(_bin, _data, args, timeout=60):
            if "--check" in args:
                return {"status": "existing", "npub": NPUB}, 0, ""
            if "--setup" in args:
                return None, 1, "invalid nsec"
            return None, 1, "unexpected"

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            prompts={
                "Identity [create / nsec / mnemonic]": "nsec",
                "nsec (nsec1": "nsec1imported",
                "Bot display name": "Hermes",
                "Your Vector npub": PEER_NPUB,
            },
            yes_no={
                "Reconfigure identity anyway?": True,
                "Import this identity as the Hermes bot?": True,
                "Enable pairing codes": True,
            },
        )
        vector_adapter._run_interactive_setup(io)
        assert nsec.read_text() == "nsec1original\n"
        assert not (data_dir / "identity.nsec.bak").exists()
        assert io.saved == {}

    def test_invalid_nsec_offers_replace(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        data_dir = tmp_path / "sdk"
        data_dir.mkdir()
        (data_dir / "identity.nsec").write_text("not-an-nsec\n")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(vector_adapter, "resolve_data_dir", lambda: data_dir)
        cli_calls = []

        def fake_cli(_bin, _data, args, timeout=60):
            cli_calls.append(list(args))
            if "--check" in args:
                return (
                    {"error": "not a valid nsec", "code": "invalid_nsec"},
                    1,
                    '{"error":"not a valid nsec","code":"invalid_nsec"}',
                )
            if "--setup" in args:
                return {"status": "created", "npub": NPUB}, 0, ""
            return None, 1, "unexpected"

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            prompts={
                "Identity [create / nsec / mnemonic]": "create",
                "Bot display name": "Hermes",
                "Your Vector npub": PEER_NPUB,
            },
            yes_no={
                "Replace the unreadable identity.nsec?": True,
                "Enable pairing codes": True,
            },
        )
        vector_adapter._run_interactive_setup(io)
        assert io.saved["VECTOR_NPUB"] == NPUB
        assert cli_calls[0] == ["--check"]
        assert cli_calls[1] == ["--setup"]
        assert not (data_dir / "identity.nsec.bak").exists()

    def test_check_timeout_does_not_offer_replace(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        data_dir = tmp_path / "sdk"
        data_dir.mkdir()
        nsec = data_dir / "identity.nsec"
        nsec.write_text("nsec1original\n")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "resolve_data_dir", lambda: data_dir)
        cli_calls = []

        def fake_cli(_bin, _data, args, timeout=60):
            cli_calls.append(list(args))
            if "--check" in args:
                return None, 124, "timed out after 30s"
            return {"status": "created", "npub": NPUB}, 0, ""

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            yes_no={"Replace the unreadable identity.nsec?": True},
        )
        vector_adapter._run_interactive_setup(io)
        assert cli_calls == [["--check"]]
        assert io.saved == {}
        assert nsec.read_text() == "nsec1original\n"
        assert not any("unreadable" in m.lower() for m in io.logs["warn"])

    def test_interrupt_during_setup_restores_identity(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        data_dir = tmp_path / "sdk"
        data_dir.mkdir()
        nsec = data_dir / "identity.nsec"
        nsec.write_text("nsec1original\n")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(vector_adapter, "resolve_data_dir", lambda: data_dir)

        def fake_cli(_bin, _data, args, timeout=60):
            if "--check" in args:
                return {"status": "existing", "npub": NPUB}, 0, ""
            if "--setup" in args:
                raise KeyboardInterrupt()
            return None, 1, "unexpected"

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            prompts={
                "Identity [create / nsec / mnemonic]": "create",
                "Bot display name": "Hermes",
                "Your Vector npub": PEER_NPUB,
            },
            yes_no={
                "Reconfigure identity anyway?": True,
                "Enable pairing codes": True,
            },
        )
        raised = False
        try:
            vector_adapter._run_interactive_setup(io)
        except KeyboardInterrupt:
            raised = True
        assert raised
        assert nsec.read_text() == "nsec1original\n"
        assert not (data_dir / "identity.nsec.bak").exists()
        assert io.saved == {}

    def test_stale_bak_restored_before_check(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "vector-bridge"
        fake_bin.write_text("")
        data_dir = tmp_path / "sdk"
        data_dir.mkdir()
        bak = data_dir / "identity.nsec.bak"
        bak.write_text("nsec1original\n")
        monkeypatch.setattr(
            vector_adapter, "_ensure_bridge_binary", lambda _io: fake_bin
        )
        monkeypatch.setattr(vector_adapter, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(vector_adapter, "resolve_data_dir", lambda: data_dir)
        seen_nsec = []

        def fake_cli(_bin, data, args, timeout=60):
            if "--check" in args:
                nsec = Path(data) / "identity.nsec"
                seen_nsec.append(nsec.read_text() if nsec.is_file() else None)
                return {"status": "existing", "npub": NPUB}, 0, ""
            if "--setup" in args:
                return {"status": "existing", "npub": NPUB}, 0, ""
            return None, 1, "unexpected"

        monkeypatch.setattr(vector_adapter, "_run_bridge_cli", fake_cli)
        io = _fake_setup_io(
            prompts={
                "Bot display name": "Hermes",
                "Your Vector npub": PEER_NPUB,
            },
            yes_no={
                "Reconfigure identity anyway?": False,
                "Enable pairing codes": True,
            },
        )
        vector_adapter._run_interactive_setup(io)
        assert seen_nsec == ["nsec1original\n"]
        assert (data_dir / "identity.nsec").read_text() == "nsec1original\n"
        assert not bak.exists()
        assert io.saved["VECTOR_NPUB"] == NPUB


class TestInboxFilenames:
    def test_sanitize_strips_path_parts(self):
        assert vector_adapter._sanitize_filename("uploads/nested/notes.pdf") == "notes.pdf"

    def test_sanitize_empty_fallback(self):
        assert vector_adapter._sanitize_filename("...") == "file"

    def test_unique_path_adds_suffix(self, tmp_path):
        first = vector_adapter._unique_path(tmp_path, "a.pdf")
        first.write_text("1")
        second = vector_adapter._unique_path(tmp_path, "a.pdf")
        assert second.name == "a-2.pdf"


class TestInboundFiles:
    def test_file_only_acks_and_breadcrumb_skips_agent(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_NPUB)
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(monkeypatch, tmp_path)
        handled = []
        acks = []
        crumbs = []

        async def capture(event):
            handled.append(event)

        async def fake_download(att, dest, *, author_npub):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"pdf-bytes")
            return dest

        async def fake_send(**kwargs):
            acks.append(kwargs)
            return vector_adapter.SendResult(success=True, message_id="ack")

        adapter.handle_message = capture  # type: ignore[method-assign]
        adapter._download_attachment = fake_download  # type: ignore[method-assign]
        adapter.send = fake_send  # type: ignore[method-assign]
        adapter._append_session_breadcrumb = (  # type: ignore[method-assign]
            lambda source, content: crumbs.append(content)
        )

        att = {
            "id": "att1",
            "name": "notes.pdf",
            "extension": "pdf",
            "size": 9,
            "key": "",
            "nonce": "",
            "url": "",
            "path": "",
        }
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "",
                    msg_id="file-only-1",
                    is_file=True,
                    attachments=[att],
                    at_ms=1_700_000_000_000,
                )
            )
        )
        assert handled == []
        assert acks and "saved" in acks[0]["content"]
        assert crumbs and "notes.pdf" in crumbs[0]
        inbox_files = list((tmp_path / "files" / "inbox").rglob("*.pdf"))
        assert len(inbox_files) == 1
        assert (tmp_path / "files" / "index.jsonl").is_file()

    def test_file_only_followup_text_attaches_pending_inbox(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_NPUB)
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(monkeypatch, tmp_path)
        handled = []

        async def capture(event):
            handled.append(event)

        async def fake_download(att, dest, *, author_npub):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"img")
            return dest

        async def fake_send(**kwargs):
            return vector_adapter.SendResult(success=True, message_id="ack")

        adapter.handle_message = capture  # type: ignore[method-assign]
        adapter._download_attachment = fake_download  # type: ignore[method-assign]
        adapter.send = fake_send  # type: ignore[method-assign]
        adapter._append_session_breadcrumb = lambda *a, **k: None  # type: ignore[method-assign]

        att = {"id": "att3", "name": "pic.jpg", "extension": "jpg", "size": 3}
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB, "", msg_id="fo1", is_file=True, attachments=[att]
                )
            )
        )
        assert handled == []
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "what is that image?", msg_id="fo2")
            )
        )
        assert len(handled) == 1
        assert handled[0].text == "what is that image?"
        assert handled[0].media_urls
        assert handled[0].media_types[0].startswith("image/")
        assert handled[0].message_type == vector_adapter.MessageType.PHOTO
        assert adapter._pending_inbox.get(PEER_NPUB) in (None, [])

    def test_sequential_file_only_messages_accumulate_pending_inbox(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_NPUB)
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(monkeypatch, tmp_path)
        handled = []

        async def capture(event):
            handled.append(event)

        async def fake_download(att, dest, *, author_npub):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"bytes")
            return dest

        async def fake_send(**kwargs):
            return vector_adapter.SendResult(success=True, message_id="ack")

        adapter.handle_message = capture  # type: ignore[method-assign]
        adapter._download_attachment = fake_download  # type: ignore[method-assign]
        adapter.send = fake_send  # type: ignore[method-assign]
        adapter._append_session_breadcrumb = lambda *a, **k: None  # type: ignore[method-assign]

        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "",
                    msg_id="seq-1",
                    is_file=True,
                    attachments=[
                        {"id": "a", "name": "pic.jpg", "extension": "jpg", "size": 5}
                    ],
                )
            )
        )
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "",
                    msg_id="seq-2",
                    is_file=True,
                    attachments=[
                        {"id": "b", "name": "notes.pdf", "extension": "pdf", "size": 5}
                    ],
                )
            )
        )
        assert handled == []
        pending = adapter._pending_inbox.get(PEER_NPUB) or []
        assert len(pending) == 2
        names = [Path(p).name for p, _m in pending]
        assert any("pic.jpg" in n for n in names)
        assert any("notes.pdf" in n for n in names)

        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "process these", msg_id="seq-3")
            )
        )
        assert len(handled) == 1
        assert handled[0].text == "process these"
        assert len(handled[0].media_urls) == 2
        assert len(handled[0].media_types) == 2
        assert adapter._pending_inbox.get(PEER_NPUB) in (None, [])

    def test_breadcrumb_uses_session_store_id_not_routing_key(
        self, monkeypatch, tmp_path
    ):
        adapter = _make_adapter(monkeypatch, tmp_path)
        written = []

        class _Entry:
            session_id = "20260829_real_session"

        class _Store:
            def get_or_create_session(self, source, touch_activity=True):
                written.append(("store", getattr(source, "chat_id", None), touch_activity))
                return _Entry()

        class _DB:
            def ensure_session(self, session_id, source="unknown"):
                written.append(("ensure", session_id, source))

            def append_message(self, session_id, role, content, **kwargs):
                written.append(("append", session_id, role, content[:40], kwargs.get("display_kind")))

        class _Runner:
            _session_db = _DB()

        adapter._session_store = _Store()
        adapter.gateway_runner = _Runner()
        src = adapter.build_source(
            chat_id=PEER_NPUB, chat_type="dm", user_id=PEER_NPUB, user_name="p"
        )
        adapter._append_session_breadcrumb(src, "[Vector inbox] saved pic.jpg")
        kinds = [w[0] for w in written]
        assert kinds == ["store", "ensure", "append"]
        assert written[1][1] == "20260829_real_session"
        assert written[2][1] == "20260829_real_session"
        assert written[2][1].startswith("2026")
        assert "agent:main:vector" not in written[2][1]

    def test_file_plus_caption_goes_to_agent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", PEER_NPUB)
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(monkeypatch, tmp_path)
        handled = []

        async def capture(event):
            handled.append(event)

        async def fake_download(att, dest, *, author_npub):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"img")
            return dest

        adapter.handle_message = capture  # type: ignore[method-assign]
        adapter._download_attachment = fake_download  # type: ignore[method-assign]

        att = {
            "id": "att2",
            "name": "shot.png",
            "extension": "png",
            "size": 3,
        }
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "what's in this",
                    msg_id="file-cap-1",
                    is_file=True,
                    attachments=[att],
                )
            )
        )
        assert len(handled) == 1
        assert handled[0].text == "what's in this"
        assert handled[0].media_urls
        assert handled[0].message_type == vector_adapter.MessageType.PHOTO
        assert Path(handled[0].media_urls[0]).is_file()

    def test_file_only_unauthorized_pairing_off_not_saved(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("VECTOR_PAIRING", "off")
        monkeypatch.setenv("VECTOR_ALLOWED_USERS", NPUB)
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(monkeypatch, tmp_path)
        downloads = []

        async def fake_download(att, dest, *, author_npub):
            downloads.append(dest)
            return dest

        adapter._download_attachment = fake_download  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "",
                    msg_id="unauth-file",
                    is_file=True,
                    attachments=[{"id": "x", "name": "x.bin", "extension": "bin"}],
                )
            )
        )
        assert downloads == []
        assert not (tmp_path / "files" / "inbox").exists()


class TestGroupFiles:
    def _wire(self, adapter, tmp_path):
        handled = []
        downloads = []
        acks = []
        crumbs = []

        async def capture(event):
            handled.append(event)

        async def fake_download(att, dest, *, author_npub):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"bytes")
            downloads.append(str(dest))
            return dest

        async def fake_send(**kwargs):
            acks.append(kwargs)
            return vector_adapter.SendResult(success=True, message_id="ack")

        adapter.handle_message = capture  # type: ignore[method-assign]
        adapter._download_attachment = fake_download  # type: ignore[method-assign]
        adapter.send = fake_send  # type: ignore[method-assign]
        adapter._append_session_breadcrumb = (  # type: ignore[method-assign]
            lambda source, content: crumbs.append(content)
        )
        return handled, downloads, acks, crumbs

    def _file_event(self, *, msg_id: str, **overrides):
        data = dict(
            is_group=True,
            is_file=True,
            chat_id=CHANNEL_ID,
            community_id="cc" * 32,
            attachments=[
                {"id": "a", "name": "notes.pdf", "extension": "pdf", "size": 4}
            ],
        )
        data.update(overrides)
        return _message_event(PEER_NPUB, "", msg_id=msg_id, **data)

    def test_file_only_not_downloaded_by_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        handled, downloads, acks, crumbs = self._wire(adapter, tmp_path)
        asyncio.run(
            adapter._handle_message_event(self._file_event(msg_id="g-file-quiet"))
        )
        assert handled == []
        assert downloads == []
        assert acks == []
        assert crumbs == []
        assert not (tmp_path / "files" / "inbox").exists()
        pending = vector_adapter._group_file_pending_path(CHANNEL_ID, "g-file-quiet")
        assert pending.is_file()

    def test_reply_mention_only_downloads_no_turn(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        handled, downloads, acks, crumbs = self._wire(adapter, tmp_path)
        asyncio.run(
            adapter._handle_message_event(self._file_event(msg_id="g-file-1"))
        )
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB}",
                    msg_id="g-reply-only",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    reply_to="g-file-1",
                )
            )
        )
        assert handled == []
        assert downloads
        assert acks == []
        assert crumbs and "notes.pdf" in crumbs[0]
        inbox = list((tmp_path / "files" / "inbox" / CHANNEL_ID).rglob("*.pdf"))
        assert len(inbox) == 1

    def test_reply_mention_plus_text_takes_turn(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        handled, downloads, acks, _crumbs = self._wire(adapter, tmp_path)
        asyncio.run(
            adapter._handle_message_event(self._file_event(msg_id="g-file-2"))
        )
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} what's in this",
                    msg_id="g-reply-ask",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    reply_to="g-file-2",
                )
            )
        )
        assert len(handled) == 1
        assert handled[0].source.chat_type == "group"
        assert handled[0].media_urls
        assert handled[0].message_type == vector_adapter.MessageType.DOCUMENT
        assert downloads
        assert acks == []

    def test_reply_without_mention_not_downloaded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        handled, downloads, acks, _crumbs = self._wire(adapter, tmp_path)
        asyncio.run(
            adapter._handle_message_event(self._file_event(msg_id="g-file-3"))
        )
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    "nice pdf",
                    msg_id="g-reply-chat",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    reply_to="g-file-3",
                )
            )
        )
        assert handled == []
        assert downloads == []
        assert acks == []

    def test_unauthorized_reply_mention_not_downloaded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=NPUB
        )
        handled, downloads, _acks, _crumbs = self._wire(adapter, tmp_path)
        asyncio.run(
            adapter._handle_message_event(self._file_event(msg_id="g-file-4"))
        )
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} look",
                    msg_id="g-reply-unauth",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    reply_to="g-file-4",
                )
            )
        )
        assert handled == []
        assert downloads == []

    def test_same_event_file_plus_mention_still_works(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(
            monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB
        )
        handled, downloads, acks, _crumbs = self._wire(adapter, tmp_path)
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} what's in this",
                    msg_id="g-file-cap",
                    is_group=True,
                    is_file=True,
                    chat_id=CHANNEL_ID,
                    attachments=[
                        {"id": "g1", "name": "shot.png", "extension": "png", "size": 5}
                    ],
                )
            )
        )
        assert len(handled) == 1
        assert handled[0].media_urls
        assert downloads
        assert acks == []

    def test_download_all_saves_file_only_silently(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=PEER_NPUB,
            community_download_all=True,
        )
        handled, downloads, acks, crumbs = self._wire(adapter, tmp_path)
        asyncio.run(
            adapter._handle_message_event(self._file_event(msg_id="g-file-all"))
        )
        assert handled == []
        assert downloads
        assert acks == []
        assert crumbs
        inbox = list((tmp_path / "files" / "inbox" / CHANNEL_ID).rglob("*.pdf"))
        assert len(inbox) == 1

    def test_download_all_then_reply_mention_text_uses_saved(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vector_adapter, "resolve_files_root", lambda: tmp_path / "files"
        )
        adapter = _make_adapter(
            monkeypatch,
            tmp_path,
            npub=NPUB,
            allowed_users=PEER_NPUB,
            community_download_all=True,
        )
        handled, downloads, _acks, _crumbs = self._wire(adapter, tmp_path)
        asyncio.run(
            adapter._handle_message_event(self._file_event(msg_id="g-file-all-2"))
        )
        n_first = len(downloads)
        asyncio.run(
            adapter._handle_message_event(
                _message_event(
                    PEER_NPUB,
                    f"@{NPUB} summarize this",
                    msg_id="g-reply-all",
                    is_group=True,
                    chat_id=CHANNEL_ID,
                    reply_to="g-file-all-2",
                )
            )
        )
        assert len(handled) == 1
        assert handled[0].media_urls
        assert Path(handled[0].media_urls[0]).is_file()
        assert len(downloads) == n_first


class TestOutboundFiles:
    def test_send_document_posts_send_file(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._running = True
        payload = tmp_path / "out.pdf"
        payload.write_bytes(b"pdf")
        monkeypatch.setattr(
            vector_adapter.VectorAdapter,
            "validate_media_delivery_path",
            staticmethod(lambda path, session_key="": str(path)),
        )

        class _Resp:
            status_code = 200
            content = b'{"id":"file1"}'

            def json(self):
                return {"id": "file1"}

        posts = []

        class _Client:
            async def post(self, url, json=None, headers=None, timeout=None):
                posts.append({"url": url, "json": json})
                return _Resp()

        adapter._http_client = _Client()
        result = asyncio.run(
            adapter.send_document(PEER_NPUB, str(payload), caption="here")
        )
        assert result.success
        assert any(p["url"].endswith("/send-file") for p in posts)
        assert posts[0]["json"]["path"] == str(payload)
        assert any(p["url"].endswith("/send") for p in posts)

    def test_send_document_to_channel_posts_channel_id(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._running = True
        payload = tmp_path / "out.pdf"
        payload.write_bytes(b"pdf")
        monkeypatch.setattr(
            vector_adapter.VectorAdapter,
            "validate_media_delivery_path",
            staticmethod(lambda path, session_key="": str(path)),
        )

        class _Resp:
            status_code = 200
            content = b'{"id":"file1"}'

            def json(self):
                return {"id": "file1"}

        posts = []

        class _Client:
            async def post(self, url, json=None, headers=None, timeout=None):
                posts.append({"url": url, "json": json})
                return _Resp()

        adapter._http_client = _Client()
        result = asyncio.run(
            adapter.send_document(CHANNEL_ID, str(payload), caption="here")
        )
        assert result.success
        file_post = next(p for p in posts if p["url"].endswith("/send-file"))
        cap_post = next(p for p in posts if p["url"].endswith("/send"))
        assert file_post["json"]["to"] == CHANNEL_ID
        assert cap_post["json"]["to"] == CHANNEL_ID
        assert not file_post["json"]["to"].startswith("npub1")


_EYES = "\U0001f440"
_CHECK = "✅"


class _FakeHttp:
    def __init__(self, status: int = 200):
        self.posts: list = []
        self.status = status

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        status = self.status

        class _Resp:
            status_code = status

        return _Resp()


class TestReactions:
    def test_add_reaction_posts_react(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        http = _FakeHttp()
        adapter._http_client = http
        result = asyncio.run(
            adapter.add_reaction(PEER_NPUB, _EYES, message_id="target-1")
        )
        assert result == {"success": True, "message_id": "target-1"}
        assert http.posts[0]["url"].endswith("/react")
        assert http.posts[0]["json"] == {
            "to": PEER_NPUB,
            "message_id": "target-1",
            "emoji": _EYES,
        }

    def test_add_reaction_falls_back_to_last_inbound(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        http = _FakeHttp()
        adapter._http_client = http
        adapter._record_last_inbound(PEER_NPUB, "inbound-9")
        result = asyncio.run(adapter.add_reaction(PEER_NPUB, "👍"))
        assert result["success"] is True
        assert result["message_id"] == "inbound-9"
        assert http.posts[0]["json"]["message_id"] == "inbound-9"

    def test_add_reaction_hex_chat_uses_canonical_last_inbound(
        self, monkeypatch, tmp_path
    ):
        adapter = _make_adapter(monkeypatch, tmp_path)
        http = _FakeHttp()
        adapter._http_client = http
        adapter._record_last_inbound(PEER_HEX, "inbound-hex")
        result = asyncio.run(adapter.add_reaction(PEER_NPUB, "👍"))
        assert result["message_id"] == "inbound-hex"

    def test_add_reaction_without_target_errors(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        adapter._http_client = _FakeHttp()
        result = asyncio.run(adapter.add_reaction(PEER_NPUB, "👍"))
        assert result["success"] is False
        assert "no message" in result["error"]

    def test_remove_reaction_posts_remove(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path)
        http = _FakeHttp()
        adapter._http_client = http
        result = asyncio.run(
            adapter.remove_reaction(PEER_NPUB, message_id="target-1")
        )
        assert result == {"success": True, "message_id": "target-1"}
        assert http.posts[0]["json"] == {
            "to": PEER_NPUB,
            "message_id": "target-1",
            "remove": True,
        }

    def test_processing_hooks_noop_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VECTOR_REACTIONS", raising=False)
        adapter = _make_adapter(monkeypatch, tmp_path)
        http = _FakeHttp()
        adapter._http_client = http
        from gateway.platforms.base import (
            MessageEvent,
            MessageType,
            ProcessingOutcome,
        )

        event = MessageEvent(
            text="hi",
            message_type=MessageType.TEXT,
            source=adapter.build_source(
                chat_id=PEER_NPUB,
                chat_name="p",
                chat_type="dm",
                user_id=PEER_NPUB,
                user_name="p",
            ),
            message_id="target-1",
        )
        asyncio.run(adapter.on_processing_start(event))
        asyncio.run(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))
        assert http.posts == []

    def test_processing_start_adds_eyes_when_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTOR_REACTIONS", "on")
        adapter = _make_adapter(monkeypatch, tmp_path)
        http = _FakeHttp()
        adapter._http_client = http
        from gateway.platforms.base import MessageEvent, MessageType

        event = MessageEvent(
            text="hi",
            message_type=MessageType.TEXT,
            source=adapter.build_source(
                chat_id=PEER_NPUB,
                chat_name="p",
                chat_type="dm",
                user_id=PEER_NPUB,
                user_name="p",
            ),
            message_id="target-1",
        )
        asyncio.run(adapter.on_processing_start(event))
        assert len(http.posts) == 1
        assert http.posts[0]["json"]["emoji"] == _EYES
        assert http.posts[0]["json"]["message_id"] == "target-1"

    def test_processing_complete_swaps_to_check_when_enabled(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("VECTOR_REACTIONS", "on")
        adapter = _make_adapter(monkeypatch, tmp_path)
        http = _FakeHttp()
        adapter._http_client = http
        from gateway.platforms.base import (
            MessageEvent,
            MessageType,
            ProcessingOutcome,
        )

        event = MessageEvent(
            text="hi",
            message_type=MessageType.TEXT,
            source=adapter.build_source(
                chat_id=PEER_NPUB,
                chat_name="p",
                chat_type="dm",
                user_id=PEER_NPUB,
                user_name="p",
            ),
            message_id="target-1",
        )
        asyncio.run(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))
        assert [p["json"] for p in http.posts] == [
            {"to": PEER_NPUB, "message_id": "target-1", "remove": True},
            {"to": PEER_NPUB, "message_id": "target-1", "emoji": _CHECK},
        ]

    def test_inbound_records_last_inbound(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, allowed_users=PEER_NPUB)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_event(
                _message_event(PEER_NPUB, "hi", msg_id="live-1")
            )
        )
        assert adapter._last_inbound_by_chat[PEER_NPUB] == "live-1"
        assert captured[0].message_id == "live-1"

    def test_inbound_peer_reaction_on_ours(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, npub=NPUB, allowed_users=PEER_NPUB)
        adapter._record_sent_message("bot-msg-1")
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_update(
                {
                    "type": "message_update",
                    "data": {
                        "id": "bot-msg-1",
                        "chat_id": PEER_NPUB,
                        "npub": PEER_NPUB,
                        "mine": True,
                        "text": "the bot's earlier reply",
                        "reactions": [
                            {
                                "id": "react-1",
                                "author_id": PEER_NPUB,
                                "emoji": "❤️",
                            }
                        ],
                    },
                }
            )
        )
        assert len(captured) == 1
        event = captured[0]
        assert event.text == "reaction:added:❤️"
        assert event.reply_to_message_id == "bot-msg-1"
        assert event.reply_to_text == "the bot's earlier reply"
        assert event.reply_to_is_own_message is True

    def test_inbound_our_own_reaction_is_skipped(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, tmp_path, npub=NPUB)
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        asyncio.run(
            adapter._handle_message_update(
                {
                    "type": "message_update",
                    "data": {
                        "id": "peer-msg-1",
                        "chat_id": PEER_NPUB,
                        "npub": PEER_NPUB,
                        "mine": False,
                        "text": "hi",
                        "reactions": [
                            {
                                "id": "react-ours",
                                "author_id": NPUB,
                                "emoji": "👀",
                            }
                        ],
                    },
                }
            )
        )
        assert captured == []

    def test_reactions_env_default_off(self, monkeypatch):
        monkeypatch.delenv("VECTOR_REACTIONS", raising=False)
        assert vector_adapter._processing_reactions_enabled() is False
        monkeypatch.setenv("VECTOR_REACTIONS", "on")
        assert vector_adapter._processing_reactions_enabled() is True
        monkeypatch.setenv("VECTOR_REACTIONS", "false")
        assert vector_adapter._processing_reactions_enabled() is False
