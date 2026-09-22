"""Read the ``vector:`` block from Hermes config.yaml.

The rest of the YAML translator lives with the setup hooks. Bridge download
needs this reader without importing the adapter.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .constants import _RELEASE_REPO_RE, _RELEASE_TAG_RE
from .env import _scoped_env_str

from . import paths

logger = logging.getLogger("hermes_plugins.vector_platform.adapter")


def _config_yaml_path() -> Path:
    try:
        home = paths.get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    return Path(home) / "config.yaml"

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

def _yaml_list_to_csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()

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
    """Write ``os.environ`` only when the process does not already have ``key``.

    The "already set" check stays on ``os.environ`` (explicit process env
    wins). Multiplex secondaries pass ``skip=True`` so this never publishes
    their YAML into the shared process env.
    """
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
) -> Dict[str, Any]:
    """Wizard answers as a top-level ``vector:`` mapping. Empties clear keys.

    Communities are not part of the wizard — operators set them in
    ``config.yaml`` ``vector.communities`` if needed. Omitting ``communities``
    here leaves any existing block untouched.
    """
    bot: Dict[str, Any] = {}
    if bot_name:
        bot["name"] = bot_name
    if bot_about:
        bot["about"] = bot_about
    return {
        "bot": bot or None,
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

    Env wins. Single-profile gateways bridge YAML into process env for the
    sidecar. Multiplex secondaries skip that write; spawn restores their
    scoped env into the child instead.
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

    group_context = vector_cfg.get("group_context")
    if isinstance(group_context, dict):
        if "enabled" in group_context:
            enabled = _yaml_on_off(group_context.get("enabled"))
            if enabled is not None:
                seeded["group_context"] = enabled
                _set_env_if_unset("VECTOR_GROUP_CONTEXT", enabled, skip=skip)
            else:
                logger.warning(
                    "Vector: ignoring vector.group_context.enabled=%r "
                    "(want true/false)",
                    group_context.get("enabled"),
                )
        for yaml_key, extra_key, env_key in (
            ("max_messages", "group_context_max", "VECTOR_GROUP_CONTEXT_MAX"),
            (
                "max_chars",
                "group_context_max_chars",
                "VECTOR_GROUP_CONTEXT_MAX_CHARS",
            ),
            (
                "max_age_secs",
                "group_context_max_age_secs",
                "VECTOR_GROUP_CONTEXT_MAX_AGE_SECS",
            ),
        ):
            if yaml_key not in group_context:
                continue
            count = _yaml_count(group_context.get(yaml_key))
            if count is None:
                logger.warning(
                    "Vector: ignoring vector.group_context.%s=%r "
                    "(want a non-negative integer); using the default",
                    yaml_key,
                    group_context.get(yaml_key),
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

