"""Ports, timeouts, and other values that do not read the environment."""

from __future__ import annotations

import re

PLUGIN_VERSION = "0.5.2"

DEFAULT_BRIDGE_PORT = 8096
DEFAULT_BRIDGE_HOST = "127.0.0.1"
# VectorBot::build is usually ~1s; slash kind-10304 used to run *before*
# Ready and take 20–40s. /health is now ready after build, but Hermes still
# wraps connect() in HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT (default 30).
# Keep VECTOR_STARTUP_TIMEOUT below that floor unless the operator raised it.
DEFAULT_STARTUP_TIMEOUT = 60
HERMES_CONNECT_TIMEOUT_FLOOR = 90
MAX_MESSAGE_LENGTH = 4000
AVATAR_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
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
LAST_INBOUND_CHATS_MAX = 200
SENT_IDS_MAX = 1000
RUNTIME_RECORD_NAME = "vector-sidecar.json"
NOTIFIED_CHANNELS_FILE = "notified-channels.json"
WELCOME_SENT_FILE = "welcome-sent.json"
INBOX_NAME_MAX = 180
DOWNLOAD_TIMEOUT = 120.0
SEND_FILE_TIMEOUT = 120.0
DEFAULT_INBOUND_MEDIA_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_RELEASE_REPO = "BonesGit/hermes-vector-platform"
PREBUILT_MAX_BYTES = 80 * 1024 * 1024
PREBUILT_DOWNLOAD_TIMEOUT = 60.0
_RELEASE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RELEASE_TAG_RE = re.compile(r"^v?[A-Za-z0-9._-]+$")
