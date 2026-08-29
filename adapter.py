"""Vector platform adapter for Hermes Agent.

Registers the ``vector`` platform, npub helpers, and a ``BasePlatformAdapter``
stub. Vector users are identified by a bech32 ``npub1…`` public key.

Sidecar HTTP is not implemented.

Required env vars / config.extra keys:
    VECTOR_NPUB           Bot public key (npub1…)
    VECTOR_ALLOWED_USERS  Comma-separated allowlisted npubs
    VECTOR_HOME_CHANNEL   Operator npub for cron delivery
    VECTOR_BRIDGE_PORT    HTTP port (default 8096)
    VECTOR_BRIDGE_HOST    Bind address (default 127.0.0.1)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from hermes_constants import get_hermes_home
from gateway.platforms.base import (
    BasePlatformAdapter,
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


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class VectorAdapter(BasePlatformAdapter):
    """Vector ``BasePlatformAdapter``. Sidecar HTTP is not implemented."""

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

        logger.info(
            "Vector plugin v%s initialized: port=%d host=%s bot=%s",
            PLUGIN_VERSION,
            self.bridge_port,
            self.bridge_host,
            self.bot_name,
        )

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        logger.warning("%s — connect() is a stub", _NOT_WIRED)
        return False

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return SendResult(success=False, error=_NOT_WIRED)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        npub = normalize_npub(chat_id) or (chat_id or "").strip()
        name = f"{npub[:16]}..." if len(npub) > 16 else npub
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
    """Out-of-process Vector delivery. No sidecar HTTP yet."""
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
