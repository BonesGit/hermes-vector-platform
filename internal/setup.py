"""Identity backup, bridge CLI, and the interactive gateway setup wizard."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from .bridge_bin import (
    _BRIDGE_DIR,
    _DEFAULT_BRIDGE_BIN,
    _try_install_prebuilt_bridge,
    resolve_bridge_bin,
)
from .constants import (
    BRIDGE_CHECK_TIMEOUT,
    BRIDGE_SETUP_TIMEOUT,
    CARGO_BUILD_TIMEOUT,
    MIN_RUSTC,
)
from .bech32 import normalize_npub
from .env import _scoped_env_str
from .groups import _merge_allowed_users, _truncate_npub
from .paths import (
    discover_bot_image,
    install_bot_image,
    resolve_data_dir,
    validate_bot_image_src,
)
from .yaml_config import (
    _build_setup_vector_yaml,
    _merge_vector_display_config,
    _read_vector_yaml_block,
)

logger = logging.getLogger("hermes_plugins.vector_platform.adapter")


def _parse_rustc_version(text: str) -> Optional[tuple]:
    """Parse ``rustc 1.75.0 (...)`` → ``(1, 75)``."""
    match = re.search(r"rustc\s+(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))

def _probe_rustc() -> Optional[tuple]:
    try:
        result = subprocess.run(
            ["rustc", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_rustc_version(result.stdout or result.stderr or "")

def _parse_bridge_json(text: str) -> Optional[Dict[str, Any]]:
    """First JSON object that carries ``status``, ``code``, or ``error``."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and (
            "status" in data or "code" in data or "error" in data
        ):
            return data
    return None

def _rewrite_sidecar_profile_env(env: Dict[str, str]) -> None:
    """Drop inherited ``VECTOR_*`` and restore the active profile's values.

    No-op unless multiplexing is on and a secret scope is installed. The
    default profile and single-profile gateways keep ``os.environ``, which
    is already theirs. ``VECTOR_NSEC`` / ``VECTOR_MNEMONIC`` are never
    copied — runtime identity is ``identity.nsec`` in the data dir.
    """
    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active

        scope = current_secret_scope()
        if not (is_multiplex_active() and scope is not None):
            return
    except Exception:
        return
    for key in [k for k in env if k.startswith("VECTOR_")]:
        env.pop(key, None)
    for key, val in scope.items():
        if not key.startswith("VECTOR_") or key in ("VECTOR_NSEC", "VECTOR_MNEMONIC", "VECTOR_STUB"):
            continue
        text = str(val).strip() if val is not None else ""
        if text:
            env[key] = text

def _overlay_sidecar_extra_env(env: Dict[str, str], extra: dict) -> None:
    """Copy YAML-seeded extra flags into sidecar env when unset."""
    mapping = (
        ("VECTOR_INVITE_POLICY", "invite_policy"),
        ("VECTOR_TRUSTED_INVITERS", "trusted_inviters"),
        ("VECTOR_SLASH_COMMANDS", "slash_commands"),
        ("VECTOR_MISSED_REACT", "missed_react"),
        ("VECTOR_MISSED_REACT_EMOJI", "missed_react_emoji"),
        ("VECTOR_SSE_REPLAY_MAX", "replay_max"),
        ("VECTOR_SSE_REPLAY_MAX_AGE_SECS", "replay_max_age_secs"),
        ("VECTOR_COMMUNITY_NAME", "community_name"),
        ("VECTOR_CREATE_COMMUNITY", "create_community"),
        ("VECTOR_COMMUNITY_DOWNLOAD_ALL", "community_download_all"),
        ("VECTOR_REACTIONS", "reactions"),
        ("VECTOR_GROUP_ALLOWED_USERS", "group_allowed_users"),
        ("VECTOR_GROUP_ALLOW_ALL", "group_allowed_chats"),
    )
    for env_key, extra_key in mapping:
        if (env.get(env_key) or "").strip():
            continue
        val = extra.get(extra_key)
        if val is None or val == "":
            continue
        if isinstance(val, bool):
            env[env_key] = "on" if val else "off"
        elif isinstance(val, (list, tuple)):
            env[env_key] = ",".join(str(v).strip() for v in val if str(v).strip())
        else:
            env[env_key] = str(val)

def _bridge_cli_env(data_dir: Path) -> Dict[str, str]:
    """Env for --check/--setup: data dir set, secrets never inherited."""
    env = {**os.environ, "VECTOR_DATA_DIR": str(data_dir)}
    env.pop("VECTOR_NSEC", None)
    env.pop("VECTOR_MNEMONIC", None)
    env.pop("VECTOR_STUB", None)
    env.pop("VECTOR_SIDECAR_TOKEN", None)
    return env

def _run_bridge_cli(
    bin_path: Path,
    data_dir: Path,
    args: List[str],
    *,
    timeout: float = 60.0,
) -> tuple:
    """Run vector-bridge identity CLI. Returns ``(parsed_json, returncode, stderr)``."""
    try:
        result = subprocess.run(
            [str(bin_path), *args],
            env=_bridge_cli_env(data_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, 124, f"timed out after {timeout:.0f}s"
    except OSError as e:
        return None, 127, str(e)
    data = _parse_bridge_json(result.stdout or "")
    err = (result.stderr or "").strip()
    if data is None and err:
        data = _parse_bridge_json(err)
    return data, result.returncode, err

def _write_temp_secret(contents: str, directory: Optional[Path] = None) -> Path:
    """Write a one-shot 0600 file for --nsec-file / --mnemonic-file.

    Prefer ``VECTOR_DATA_DIR`` so a SIGKILL leftover sits next to identity
    material, not in ``/tmp``.
    """
    parent = Path(directory) if directory else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=str(parent), prefix=".vector-import.", suffix=".tmp"
    )
    try:
        try:
            os.chmod(name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write((contents or "").strip() + "\n")
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)

def _shred_unlink(path: Path) -> None:
    """Overwrite then unlink a one-shot secret file."""
    try:
        if path.is_file():
            size = max(path.stat().st_size, 1)
            with open(path, "r+b") as fh:
                fh.write(b"\0" * size)
                fh.flush()
                os.fsync(fh.fileno())
        path.unlink(missing_ok=True)
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

def _backup_identity_file(data_dir: Path, name: str) -> Optional[Path]:
    """Rename ``name`` → ``name.bak``. None if missing."""
    src = Path(data_dir) / name
    if not src.is_file():
        return None
    bak = Path(data_dir) / f"{name}.bak"
    if bak.exists():
        bak.unlink()
    src.replace(bak)
    return bak

def _backup_identity_nsec(data_dir: Path) -> Optional[Path]:
    """Rename ``identity.nsec`` → ``identity.nsec.bak``. None if missing."""
    return _backup_identity_file(data_dir, "identity.nsec")

def _backup_identity(data_dir: Path) -> List[Path]:
    """Move nsec and mnemonic aside so a failed replace can put them back."""
    baks: List[Path] = []
    for name in ("identity.nsec", "identity.mnemonic"):
        bak = _backup_identity_file(data_dir, name)
        if bak is not None:
            baks.append(bak)
    return baks

def _restore_identity_backup(bak: Optional[Path]) -> None:
    """Rename ``foo.bak`` → ``foo`` next to it."""
    if bak is None or not bak.is_file():
        return
    name = bak.name
    if not name.endswith(".bak"):
        return
    src = bak.with_name(name[: -len(".bak")])
    try:
        if src.exists():
            src.unlink()
    except OSError:
        pass
    try:
        bak.replace(src)
    except OSError as e:
        logger.warning("Vector: failed to restore %s from backup: %s", src.name, e)

def _restore_identity_nsec(data_dir: Path, bak: Optional[Path]) -> None:
    """Put the backup back if ``--setup`` failed after the rename."""
    _restore_identity_backup(bak)

def _discard_identity_backup(bak: Optional[Path]) -> None:
    if bak is None:
        return
    try:
        bak.unlink(missing_ok=True)
    except OSError:
        pass

def _discard_identity_backups(baks: List[Path]) -> None:
    for bak in baks:
        _discard_identity_backup(bak)

def _identity_nsec_locally_unreadable(data_dir: Path) -> bool:
    """True when identity.nsec exists but cannot be read (or is empty)."""
    path = Path(data_dir) / "identity.nsec"
    try:
        if not path.is_file():
            return False
        if path.stat().st_size == 0:
            return True
        path.read_bytes()
        return False
    except OSError:
        return True

def _adopt_stale_identity_backup(data_dir: Path, io) -> None:
    """If a previous setup left only ``*.bak`` identity files, put them back."""
    for name in ("identity.nsec", "identity.mnemonic"):
        src = Path(data_dir) / name
        bak = Path(data_dir) / f"{name}.bak"
        try:
            src_ok = src.is_file() and src.stat().st_size > 0
        except OSError:
            src_ok = False
        if src_ok or not bak.is_file():
            continue
        io.print_warning(
            f"Found {name}.bak but no {name} "
            "(previous setup may have been interrupted). Restoring the backup."
        )
        _restore_identity_backup(bak)

def _normalize_identity_choice(raw: str) -> Optional[str]:
    value = (raw or "").strip().lower()
    if value in ("c", "create", "new"):
        return "create"
    if value in ("n", "nsec", "import", "import nsec"):
        return "nsec"
    if value in ("m", "mnemonic", "seed", "import mnemonic"):
        return "mnemonic"
    return None

def _ensure_bridge_binary(io) -> Optional[Path]:
    """Return vector-bridge: current file, GitHub prebuilt, then cargo build.

    A stale prebuilt stamp is not "current" — download again. Runtime
    ``resolve_bridge_bin()`` still uses that file so gateway start works.
    """
    bin_path = resolve_bridge_bin(require_current=True)
    if bin_path.is_file():
        io.print_info(f"Using vector-bridge at {bin_path}")
        return bin_path

    override = _scoped_env_str("VECTOR_BRIDGE_BIN").strip()
    if override and Path(override) != _DEFAULT_BRIDGE_BIN:
        io.print_error(f"VECTOR_BRIDGE_BIN={override} does not exist.")
        io.print_info(
            "Unset VECTOR_BRIDGE_BIN to let setup download or build "
            "vector-bridge, or point it at a built binary."
        )
        return None

    prebuilt = _try_install_prebuilt_bridge(io)
    if prebuilt is not None and prebuilt.is_file():
        return prebuilt

    cargo = shutil.which("cargo")
    if not cargo:
        io.print_error("cargo not found. Install Rust 1.75+ from https://rustup.rs")
        io.print_info(
            "Or wait for a GitHub Release with a prebuilt sidecar for this platform."
        )
        io.print_info("Then re-run: hermes gateway setup")
        return None

    rustc = _probe_rustc()
    if rustc is None:
        io.print_error("rustc not found. Install Rust 1.75+ from https://rustup.rs")
        return None
    if rustc < MIN_RUSTC:
        io.print_error(
            f"rustc {rustc[0]}.{rustc[1]} is too old; vector-bridge needs >= 1.75"
        )
        return None

    cargo_toml = _BRIDGE_DIR / "Cargo.toml"
    if not cargo_toml.is_file():
        io.print_error(f"Bridge crate not found at {cargo_toml}")
        io.print_info("Reinstall the vector-platform plugin so bridge/ is present.")
        return None

    io.print_info(
        "Building vector-bridge (cargo build --release --locked; "
        "may take several minutes)..."
    )
    try:
        result = subprocess.run(
            [cargo, "build", "--release", "--locked"],
            cwd=str(_BRIDGE_DIR),
            timeout=CARGO_BUILD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        io.print_error(
            f"cargo build timed out after {CARGO_BUILD_TIMEOUT}s. Retry or build "
            "manually: cd bridge && cargo build --release --locked"
        )
        return None
    except OSError as e:
        io.print_error(f"cargo build failed to start: {e}")
        return None

    if result.returncode != 0:
        io.print_error(
            "cargo build --release --locked failed (see compiler output above)."
        )
        return None

    built = _DEFAULT_BRIDGE_BIN
    if not built.is_file():
        io.print_error(f"cargo build succeeded but {built} is missing")
        return None
    io.print_success(f"Built vector-bridge at {built}")
    return built

def _load_setup_io():
    """CLI printers/prompts. Lazy so the plugin stays importable in tests."""
    try:
        from hermes_cli.setup import (
            prompt,
            prompt_yes_no,
            save_env_value,
            get_env_value,
            print_header,
            print_info,
            print_warning,
            print_success,
            print_error,
        )
    except ImportError:
        from hermes_cli.config import get_env_value, save_env_value
        from hermes_cli.cli_output import (
            prompt,
            prompt_yes_no,
            print_header,
            print_info,
            print_warning,
            print_success,
            print_error,
        )
    return SimpleNamespace(
        prompt=prompt,
        prompt_yes_no=prompt_yes_no,
        save_env_value=save_env_value,
        get_env_value=get_env_value,
        print_header=print_header,
        print_info=print_info,
        print_warning=print_warning,
        print_success=print_success,
        print_error=print_error,
    )

def _maybe_merge_display(io, platform: Optional[Dict[str, Any]] = None) -> None:
    if _merge_vector_display_config(platform=platform):
        io.print_info(
            "Wrote display.platforms.vector.tool_progress: new to config.yaml"
        )
        if platform:
            io.print_info("Wrote vector: settings to config.yaml")
    else:
        io.print_warning(
            "Could not merge Vector settings into config.yaml. "
            "Add tool_progress: new under display.platforms.vector yourself "
            "or Hermes inherits the global default (all)."
        )

def _confirm_import_as_bot(io) -> bool:
    io.print_warning(
        "This identity will be tagged as a bot and will receive agent replies. "
        "Do not import your personal daily-driver nsec unless you intend that."
    )
    return io.prompt_yes_no("Continue to import the identity as the Hermes bot?", False)

def _run_interactive_setup(io) -> None:
    """Wizard body (testable with a mocked io + subprocess)."""
    io.print_header("Vector")
    data_dir = Path(io.get_env_value("VECTOR_DATA_DIR") or resolve_data_dir())
    # Adopt before the already-configured early return: an interrupted
    # reconfigure leaves VECTOR_NPUB set and only identity.nsec.bak on disk.
    _adopt_stale_identity_backup(data_dir, io)
    existing_npub = (io.get_env_value("VECTOR_NPUB") or "").strip()
    if existing_npub:
        io.print_info(
            f"Vector: already configured (npub: {_truncate_npub(existing_npub)})"
        )
        if not io.prompt_yes_no("Reconfigure Vector?", False):
            _ensure_bridge_binary(io)
            _maybe_merge_display(io)
            return

    bin_path = _ensure_bridge_binary(io)
    if not bin_path:
        return

    io.print_info(f"Data dir: {data_dir}")
    _adopt_stale_identity_backup(data_dir, io)

    check_data, check_code, check_err = _run_bridge_cli(
        bin_path, data_dir, ["--check"], timeout=BRIDGE_CHECK_TIMEOUT
    )

    existing_identity_npub = None
    if check_data and check_data.get("status") == "existing":
        existing_identity_npub = (check_data.get("npub") or "").strip() or None

    identity_choice: Optional[str] = "create"
    import_secret = None
    import_kind = None  # "nsec" | "mnemonic"
    wipe_identity = False
    env_nsec = (io.get_env_value("VECTOR_NSEC") or "").strip()
    env_mnemonic = (io.get_env_value("VECTOR_MNEMONIC") or "").strip()

    if existing_identity_npub:
        io.print_warning(
            f"An identity already exists (npub: {existing_identity_npub})."
        )
        io.print_warning(
            "Replacing identity.nsec creates a NEW bot. Contacts will not recognize it."
        )
        if io.prompt_yes_no("Reconfigure identity anyway?", False):
            wipe_identity = True
        else:
            identity_choice = None
    elif check_code not in (0, None):
        check_code_name = (
            (check_data or {}).get("code") if isinstance(check_data, dict) else None
        )
        io.print_error(
            f"vector-bridge --check failed (exit {check_code}): {check_err or 'no output'}"
        )
        # Only offer replace for a corrupt nsec. Timeout/crash/wrong-arch
        # must not be described as an unreadable identity.
        if check_code_name == "invalid_nsec" or _identity_nsec_locally_unreadable(
            data_dir
        ):
            io.print_warning(
                "identity.nsec is unreadable (corrupt or invalid nsec). "
                "Replacing it creates a NEW bot."
            )
            if not io.prompt_yes_no("Replace the unreadable identity.nsec?", False):
                return
            wipe_identity = True
        else:
            return

    if identity_choice is not None:
        default_mode = "create"
        if env_nsec:
            default_mode = "nsec"
            io.print_info(
                "VECTOR_NSEC is set in .env; choosing 'nsec' will copy it into "
                "identity.nsec (then delete the env var)."
            )
        elif env_mnemonic:
            default_mode = "mnemonic"
            io.print_info(
                "VECTOR_MNEMONIC is set in .env; choosing 'mnemonic' will import it."
            )
        io.print_info(
            "Create a new Vector identity, or import an nsec / 12-word mnemonic."
        )
        raw_choice = io.prompt(
            "Identity [create / nsec / mnemonic]", default=default_mode
        )
        identity_choice = _normalize_identity_choice(raw_choice or default_mode)
        if identity_choice is None:
            io.print_error("Choose create, nsec, or mnemonic.")
            return
        if identity_choice in ("nsec", "mnemonic"):
            if not _confirm_import_as_bot(io):
                io.print_info("Import cancelled.")
                return
        if identity_choice == "nsec":
            import_secret = env_nsec or io.prompt("nsec (nsec1…; input hidden)", password=True)
            if not (import_secret or "").strip():
                io.print_error("nsec is required for import.")
                return
            import_kind = "nsec"
        elif identity_choice == "mnemonic":
            import_secret = env_mnemonic or io.prompt(
                "12-word mnemonic (input hidden)", password=True
            )
            words = (import_secret or "").split()
            if len(words) != 12:
                io.print_error("Invalid mnemonic — must be exactly 12 words.")
                return
            import_kind = "mnemonic"

    io.print_info(
        "Name, about, avatar, and banner are public Nostr kind-0 metadata: "
        "anyone who has the bot npub can fetch them from relays. Leave them "
        "blank to publish no profile card."
    )
    existing_vector = _read_vector_yaml_block()
    bot_yaml = existing_vector.get("bot")
    bot_yaml = bot_yaml if isinstance(bot_yaml, dict) else {}
    bot_name = (
        io.prompt(
            "Bot display name (optional, public; blank = do not publish)",
            default=(bot_yaml.get("name") or io.get_env_value("VECTOR_BOT_NAME") or None),
        )
        or ""
    ).strip()
    bot_about = (
        io.prompt(
            "Bot about text (optional, public; blank = do not publish)",
            default=(bot_yaml.get("about") or io.get_env_value("VECTOR_BOT_ABOUT") or None),
        )
        or ""
    ).strip()

    current_avatar = discover_bot_image(data_dir, "avatar")
    if current_avatar:
        io.print_info(f"Current bot avatar: {current_avatar}")
    avatar_raw = (
        io.prompt(
            "Bot avatar image path (jpg/png/webp/gif; blank keeps current)",
            default=None,
        )
        or ""
    ).strip()
    pending_avatar: Optional[str] = None
    if avatar_raw:
        try:
            pending_avatar = str(validate_bot_image_src(avatar_raw))
        except ValueError as e:
            io.print_error(f"Avatar not installed: {e}")
            io.print_info("Continuing without changing the avatar.")

    current_banner = discover_bot_image(data_dir, "banner")
    if current_banner:
        io.print_info(f"Current bot banner: {current_banner}")
    banner_raw = (
        io.prompt(
            "Bot banner image path (jpg/png/webp/gif; blank keeps current)",
            default=None,
        )
        or ""
    ).strip()
    pending_banner: Optional[str] = None
    if banner_raw:
        try:
            pending_banner = str(validate_bot_image_src(banner_raw))
        except ValueError as e:
            io.print_error(f"Banner not installed: {e}")
            io.print_info("Continuing without changing the banner.")

    io.print_info("Enter YOUR Vector npub (hex / npub1 / nostr:npub1).")
    io.print_info("This is who the bot will DM and who is allowed to message it.")
    existing_home = (io.get_env_value("VECTOR_HOME_CHANNEL") or "").strip()
    operator_raw = io.prompt(
        "Your Vector npub", default=existing_home or None
    )
    operator_npub = normalize_npub(operator_raw or "")
    if not operator_npub:
        io.print_error(
            "A valid Vector npub is required (hex, npub1…, or nostr:npub1)."
        )
        operator_raw = io.prompt("Your Vector npub")
        operator_npub = normalize_npub(operator_raw or "")
    if not operator_npub:
        io.print_error("Operator npub is required — aborting Vector setup.")
        return

    pairing_on = io.prompt_yes_no(
        "Enable pairing codes for unknown npubs?", False
    )

    extra_args: List[str] = []
    temp_secret: Optional[Path] = None
    baks: List[Path] = []
    setup_ok = False
    setup_data: Optional[Dict[str, Any]] = None
    setup_code = 1
    setup_err = ""
    try:
        if wipe_identity:
            try:
                baks = _backup_identity(data_dir)
            except OSError as e:
                io.print_error(f"Could not replace identity.nsec: {e}")
                return
        if import_kind and import_secret:
            temp_secret = _write_temp_secret(import_secret, data_dir)
            flag = "--nsec-file" if import_kind == "nsec" else "--mnemonic-file"
            extra_args = [flag, str(temp_secret)]

        io.print_info("Running vector-bridge --setup...")
        setup_data, setup_code, setup_err = _run_bridge_cli(
            bin_path,
            data_dir,
            ["--setup", *extra_args],
            timeout=BRIDGE_SETUP_TIMEOUT,
        )
        bot_npub = ((setup_data or {}).get("npub") or "").strip()
        if setup_data and setup_code == 0 and bot_npub:
            setup_ok = True
        else:
            io.print_error(
                f"vector-bridge --setup failed (exit {setup_code}): "
                f"{setup_err or 'could not parse output'}"
            )
            return
    finally:
        if temp_secret is not None:
            _shred_unlink(temp_secret)
        # Ctrl+C / errors after the rename must put identity files back.
        if setup_ok:
            _discard_identity_backups(baks)
        else:
            for bak in baks:
                _restore_identity_backup(bak)

    bot_npub = ((setup_data or {}).get("npub") or "").strip()
    status = (setup_data or {}).get("status") or ""
    if not bot_npub:
        io.print_error("Bridge returned incomplete data (no npub).")
        return

    existing_allowed = io.get_env_value("VECTOR_ALLOWED_USERS") or ""
    io.save_env_value("VECTOR_NPUB", bot_npub)
    io.save_env_value("VECTOR_HOME_CHANNEL", operator_npub)
    io.save_env_value(
        "VECTOR_ALLOWED_USERS", _merge_allowed_users(operator_npub, existing_allowed)
    )
    if pending_avatar:
        try:
            install_bot_image(pending_avatar, data_dir, "avatar")
        except ValueError as e:
            io.print_error(f"Avatar not installed: {e}")
    if pending_banner:
        try:
            install_bot_image(pending_banner, data_dir, "banner")
        except ValueError as e:
            io.print_error(f"Banner not installed: {e}")

    if env_nsec:
        io.print_warning(
            "VECTOR_NSEC is still in .env. Delete it — the sidecar never reads it."
        )
    if env_mnemonic:
        io.print_warning(
            "VECTOR_MNEMONIC is still in .env. Delete it after you have a backup."
        )

    _maybe_merge_display(
        io,
        platform=_build_setup_vector_yaml(
            bot_name=bot_name,
            bot_about=bot_about,
            pairing_on=pairing_on,
        ),
    )

    if status == "created":
        io.print_success(f"Account created! Bot npub: {bot_npub}")
    elif status == "restored":
        io.print_success(f"Account restored! Bot npub: {bot_npub}")
    else:
        io.print_success(f"Existing account found! Bot npub: {bot_npub}")
    io.print_info("Share this npub with contacts.")
    io.print_info(
        "After gateway start the bot DMs your Vector account a hello "
        "(VECTOR_HOME_CHANNEL). Reply there to talk."
    )
    backup_bits = [str(data_dir / "identity.nsec")]
    mnemonic_path = data_dir / "identity.mnemonic"
    if mnemonic_path.is_file():
        backup_bits.append(str(mnemonic_path))
    io.print_info(
        "Back up "
        + " and ".join(backup_bits)
        + " offline — replacing them is a new bot."
    )
    io.print_success("Vector configured!")
    io.print_info("Restart the gateway: hermes gateway restart")

def interactive_setup() -> None:
    """Interactive ``hermes gateway setup`` flow for Vector."""
    _run_interactive_setup(_load_setup_io())

