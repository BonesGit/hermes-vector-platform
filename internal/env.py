"""Profile-scoped environment reads and on/off feature flags."""

from __future__ import annotations

import os
from typing import Optional


def _scoped_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Profile env read. A multiplexed secondary never borrows ``os.environ``.

    Same contract as ``gateway.platforms._shared.get_scoped_secret`` (the
    gateway config path fixed in Hermes #50094). Single-profile installs and
    the default multiplex profile keep reading ``os.environ``. If that helper
    cannot be imported, fall back to ``os.environ`` so an older Hermes still
    loads the plugin.
    """
    try:
        from gateway.platforms._shared import get_scoped_secret
    except Exception:
        val = os.environ.get(name)
        return default if val is None else val
    return get_scoped_secret(name, default)


def _scoped_env_str(name: str, default: str = "") -> str:
    val = _scoped_env(name, default)
    return default if val is None else str(val)


def _env_flag(name: str, default: str = "") -> str:
    return (_scoped_env(name) or default).strip().lower()


def _pairing_enabled() -> bool:
    """Pairing codes unless VECTOR_PAIRING is off or YAML says ignore.

    Default on. ``unauthorized_dm_behavior: ignore`` is bridged to
    ``VECTOR_PAIRING=off`` by ``_apply_yaml_config``.
    """
    return _env_flag("VECTOR_PAIRING", "on") not in (
        "off",
        "0",
        "false",
        "no",
        "disabled",
        "ignore",
    )


def _processing_reactions_enabled() -> bool:
    """VECTOR_REACTIONS default off. 👀/✅/❌ on the triggering DM while the agent works."""
    return _env_flag("VECTOR_REACTIONS") in ("1", "true", "yes", "on")


def _create_community_enabled() -> bool:
    return _env_flag("VECTOR_CREATE_COMMUNITY") in ("1", "true", "yes", "on")


def _community_download_all() -> bool:
    """VECTOR_COMMUNITY_DOWNLOAD_ALL default off. on = ingest every group file."""
    return _env_flag("VECTOR_COMMUNITY_DOWNLOAD_ALL") in ("1", "true", "yes", "on")


def _group_context_enabled() -> bool:
    """VECTOR_GROUP_CONTEXT default off."""
    return _env_flag("VECTOR_GROUP_CONTEXT") in ("1", "true", "yes", "on")


def _env_nonneg_int(name: str, default: int) -> int:
    raw = _scoped_env_str(name).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _group_context_max() -> int:
    """Message cap. ``0`` means no client cap; the sidecar still clamps the page."""
    return _env_nonneg_int("VECTOR_GROUP_CONTEXT_MAX", 20)


def _group_context_max_chars() -> int:
    """Char cap. ``0`` means no cap, same convention as the age knob."""
    return _env_nonneg_int("VECTOR_GROUP_CONTEXT_MAX_CHARS", 8000)


def _group_context_max_age_secs() -> int:
    return _env_nonneg_int("VECTOR_GROUP_CONTEXT_MAX_AGE_SECS", 7200)
