//! Hermes Vector sidecar: identity CLI plus localhost HTTP wrapping VectorBot.
//!
//! `--check` / `--setup` read and write `<VECTOR_DATA_DIR>/identity.nsec` offline.
//! Runtime binds HTTP first, then `VectorBot::build` + `on_event` in the background.
//! `VECTOR_STUB=1` keeps the HTTP stub (no live relays) for tests.

mod api;
mod commands;
mod events;
mod missed;
mod profile;

use std::fs;
use std::io::{self, Write};
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

use bip39::Mnemonic;
use nostr::nips::nip06::FromMnemonic;
use serde_json::json;
use tokio::sync::watch;
use vector_sdk::nostr::{FromBech32, Keys, PublicKey, SecretKey, ToBech32};
use vector_sdk::{InvitePolicy, VectorBot};

use crate::api::{router, stub_npub, AppState};

const IDENTITY_FILE: &str = "identity.nsec";
const MNEMONIC_FILE: &str = "identity.mnemonic";
const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8096;
const DEFAULT_SSE_PING: Duration = Duration::from_secs(30);

#[tokio::main]
async fn main() -> ExitCode {
    harden_process();
    match run(std::env::args().collect()).await {
        Ok(code) => code,
        Err(err) => {
            err.write_stderr();
            ExitCode::from(1)
        }
    }
}

/// Block ptrace, `/proc/pid/mem`, and core dumps of this process.
#[cfg(all(target_os = "linux", not(debug_assertions)))]
fn harden_process() {
    // SAFETY: PR_SET_DUMPABLE with 0 is a valid no-operand prctl on Linux.
    let _ = unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0) };
}

#[cfg(not(all(target_os = "linux", not(debug_assertions))))]
fn harden_process() {}

async fn run(args: Vec<String>) -> Result<ExitCode, CliError> {
    match parse_args(&args)? {
        Mode::Help => {
            print_usage();
            Ok(ExitCode::SUCCESS)
        }
        Mode::Check => cmd_check(&require_data_dir()?),
        Mode::Setup {
            nsec_file,
            mnemonic_file,
        } => cmd_setup(&require_data_dir()?, nsec_file, mnemonic_file),
        Mode::Serve => serve().await,
    }
}

enum Mode {
    Help,
    Check,
    Setup {
        nsec_file: Option<PathBuf>,
        mnemonic_file: Option<PathBuf>,
    },
    Serve,
}

fn parse_args(args: &[String]) -> Result<Mode, CliError> {
    let mut check = false;
    let mut setup = false;
    let mut help = false;
    let mut nsec_file = None;
    let mut mnemonic_file = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "-h" | "--help" => help = true,
            "--check" => check = true,
            "--setup" => setup = true,
            "--nsec-file" => {
                i += 1;
                let path = args
                    .get(i)
                    .ok_or_else(|| CliError::usage("--nsec-file requires a path"))?;
                nsec_file = Some(PathBuf::from(path));
            }
            "--mnemonic-file" => {
                i += 1;
                let path = args
                    .get(i)
                    .ok_or_else(|| CliError::usage("--mnemonic-file requires a path"))?;
                mnemonic_file = Some(PathBuf::from(path));
            }
            other => return Err(CliError::usage(format!("unknown argument: {other}"))),
        }
        i += 1;
    }

    if help && !check && !setup {
        return Ok(Mode::Help);
    }
    if check && setup {
        return Err(CliError::usage("specify only one of --check or --setup"));
    }
    if nsec_file.is_some() && mnemonic_file.is_some() {
        return Err(CliError::usage(
            "specify only one of --nsec-file or --mnemonic-file",
        ));
    }
    if check {
        if nsec_file.is_some() || mnemonic_file.is_some() {
            return Err(CliError::usage(
                "--nsec-file/--mnemonic-file are only valid with --setup",
            ));
        }
        return Ok(Mode::Check);
    }
    if setup {
        return Ok(Mode::Setup {
            nsec_file,
            mnemonic_file,
        });
    }
    if nsec_file.is_some() || mnemonic_file.is_some() {
        return Err(CliError::usage(
            "--nsec-file/--mnemonic-file are only valid with --setup",
        ));
    }
    Ok(Mode::Serve)
}

fn print_usage() {
    eprintln!(
        "Usage: vector-bridge [--check | --setup [--nsec-file PATH | --mnemonic-file PATH]]\n\
         \n\
         No flags: bind VECTOR_BRIDGE_HOST:VECTOR_BRIDGE_PORT (default 127.0.0.1:8096),\n\
         then VectorBot::build from VECTOR_DATA_DIR/identity.nsec.\n\
         VECTOR_SIDECAR_TOKEN is required for the HTTP server; empty token exits 1.\n\
         VECTOR_DATA_DIR is required at runtime (identity.nsec must already exist).\n\
         VECTOR_STUB=1 skips VectorBot for HTTP tests (no live relays).\n\
         Identity CLI: <VECTOR_DATA_DIR>/identity.nsec (VECTOR_DATA_DIR required).\n\
         --check never creates an identity. --setup writes the file if missing."
    );
}

fn require_data_dir() -> Result<PathBuf, CliError> {
    match std::env::var_os("VECTOR_DATA_DIR") {
        Some(p) if !p.is_empty() => Ok(PathBuf::from(p)),
        _ => Err(CliError::usage("VECTOR_DATA_DIR is required")),
    }
}

fn identity_path(data_dir: &Path) -> PathBuf {
    data_dir.join(IDENTITY_FILE)
}

fn mnemonic_path(data_dir: &Path) -> PathBuf {
    data_dir.join(MNEMONIC_FILE)
}

/// Missing or empty file → `None`. Does not create the path.
fn read_nsec(path: &Path) -> Result<Option<String>, CliError> {
    match fs::read_to_string(path) {
        Ok(contents) => {
            let nsec = contents.trim();
            if nsec.is_empty() {
                Ok(None)
            } else {
                Ok(Some(nsec.to_string()))
            }
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(err) => Err(CliError::io(path, err)),
    }
}

fn npub_from_nsec(nsec: &str) -> Result<String, CliError> {
    let secret = SecretKey::from_bech32(nsec).map_err(|_| CliError::InvalidNsec)?;
    Keys::new(secret)
        .public_key()
        .to_bech32()
        .map_err(|e| CliError::Other(e.to_string()))
}

fn write_identity(path: &Path, nsec: &str) -> Result<(), CliError> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(|err| CliError::io(parent, err))?;
        }
    }
    write_restricted(path, nsec.as_bytes())
}

fn write_mnemonic(path: &Path, phrase: &str) -> Result<(), CliError> {
    let body = format!("{}\n", phrase.trim());
    write_restricted(path, body.as_bytes())
}

#[cfg(unix)]
fn write_restricted(path: &Path, contents: &[u8]) -> Result<(), CliError> {
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

    let mut file = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .map_err(|err| CliError::io(path, err))?;
    file.write_all(contents)
        .map_err(|err| CliError::io(path, err))?;
    // OpenOptions.mode applies only to newly created files.
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
        .map_err(|err| CliError::io(path, err))?;
    Ok(())
}

#[cfg(not(unix))]
fn write_restricted(path: &Path, contents: &[u8]) -> Result<(), CliError> {
    fs::write(path, contents).map_err(|err| CliError::io(path, err))
}

fn cmd_check(data_dir: &Path) -> Result<ExitCode, CliError> {
    let path = identity_path(data_dir);
    match read_nsec(&path)? {
        None => {
            print_json(json!({ "status": "not_registered" }));
            Ok(ExitCode::SUCCESS)
        }
        Some(nsec) => {
            let npub = npub_from_nsec(&nsec)?;
            print_json(json!({ "status": "existing", "npub": npub }));
            Ok(ExitCode::SUCCESS)
        }
    }
}

fn cmd_setup(
    data_dir: &Path,
    nsec_file: Option<PathBuf>,
    mnemonic_file: Option<PathBuf>,
) -> Result<ExitCode, CliError> {
    let path = identity_path(data_dir);
    if let Some(nsec) = read_nsec(&path)? {
        let npub = npub_from_nsec(&nsec)?;
        print_json(json!({ "status": "existing", "npub": npub }));
        return Ok(ExitCode::SUCCESS);
    }

    let (nsec, status, mnemonic) = if let Some(src) = nsec_file {
        (load_nsec_file(&src)?, "restored", None)
    } else if let Some(src) = mnemonic_file {
        let (nsec, phrase) = nsec_from_mnemonic_file(&src)?;
        (nsec, "restored", Some(phrase))
    } else {
        let (phrase, nsec) = generate_mnemonic_nsec()?;
        (nsec, "created", Some(phrase))
    };

    let npub = npub_from_nsec(&nsec)?;
    write_identity(&path, &nsec)?;
    if let Some(phrase) = mnemonic {
        write_mnemonic(&mnemonic_path(data_dir), &phrase)?;
    }
    if status == "created" {
        eprintln!(
            "[vector-bridge] Created a new bot identity {} (nsec {}, mnemonic {}). \
             Back those files up — they are the bot.",
            npub,
            path.display(),
            mnemonic_path(data_dir).display()
        );
    }
    print_json(json!({ "status": status, "npub": npub }));
    Ok(ExitCode::SUCCESS)
}

/// Mint a NIP-06 identity: random 12-word BIP-39 English mnemonic → nsec.
fn generate_mnemonic_nsec() -> Result<(String, String), CliError> {
    let mnemonic = Mnemonic::generate(12).map_err(|e| CliError::Other(e.to_string()))?;
    let phrase = mnemonic.to_string();
    let keys = Keys::from_mnemonic(&phrase, None).map_err(|_| CliError::InvalidMnemonic)?;
    let nsec = keys
        .secret_key()
        .to_bech32()
        .map_err(|e| CliError::Other(e.to_string()))?;
    Ok((phrase, nsec))
}

fn load_nsec_file(src: &Path) -> Result<String, CliError> {
    let contents = fs::read_to_string(src).map_err(|err| CliError::io(src, err))?;
    let nsec = contents.trim();
    if nsec.is_empty() {
        return Err(CliError::InvalidNsec);
    }
    let nsec = nsec.to_string();
    npub_from_nsec(&nsec)?;
    Ok(nsec)
}

fn nsec_from_mnemonic_file(src: &Path) -> Result<(String, String), CliError> {
    let contents = fs::read_to_string(src).map_err(|err| CliError::io(src, err))?;
    let mnemonic = contents.trim();
    if mnemonic.is_empty() {
        return Err(CliError::InvalidMnemonic);
    }
    let keys = Keys::from_mnemonic(mnemonic, None).map_err(|_| CliError::InvalidMnemonic)?;
    let nsec = keys
        .secret_key()
        .to_bech32()
        .map_err(|e| CliError::Other(e.to_string()))?;
    Ok((nsec, mnemonic.to_string()))
}

fn print_json(value: serde_json::Value) {
    println!("{value}");
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stop {
    Running,
    Graceful,
    ListenEnded,
    LoginFailed,
}

fn stub_mode() -> bool {
    std::env::var("VECTOR_STUB")
        .map(|v| v == "1")
        .unwrap_or(false)
}

/// Runtime identity: require an existing nsec so `build()` cannot mint.
fn require_runtime_data_dir() -> Result<PathBuf, CliError> {
    let data_dir = require_data_dir()?;
    let path = identity_path(&data_dir);
    match read_nsec(&path)? {
        None => Err(CliError::Other(format!(
            "missing {}; run --setup first (runtime will not mint)",
            path.display()
        ))),
        Some(nsec) => {
            let _ = npub_from_nsec(&nsec)?;
            Ok(data_dir)
        }
    }
}

fn request_stop(tx: &watch::Sender<Stop>, why: Stop) {
    let _ = tx.send_if_modified(|cur| {
        if *cur == Stop::Running {
            *cur = why;
            true
        } else {
            false
        }
    });
}

async fn wait_stop(mut rx: watch::Receiver<Stop>) -> Stop {
    loop {
        let current = *rx.borrow_and_update();
        if current != Stop::Running {
            return current;
        }
        if rx.changed().await.is_err() {
            return Stop::Graceful;
        }
    }
}

fn spawn_vector_bot(state: AppState, data_dir: PathBuf, stop_tx: watch::Sender<Stop>) {
    tokio::spawn(async move {
        match VectorBot::builder()
            .data_dir(&data_dir)
            .invite_policy(invite_policy_from_env())
            .build()
            .await
        {
            Ok(bot) => {
                state.set_bot(bot.clone()).await;
                // /health ready as soon as the bot identity is loaded. Do not
                // wait for BotEvent::Ready: if slash commands are registered
                // before on_event, prepare_listen publishes kind-10304 to
                // discovery relays first (20–40s) and used to overrun the
                // connect timeout so Hermes killed a live sidecar.
                state.mark_ready(bot.npub()).await;
                eprintln!("[vector-bridge] bot online");
                // Do **not** register slash commands here. SDK try_command
                // reads a live RwLock, so BotEvent::Ready can attach handlers
                // after listen starts; the picker publishes in the background.
                // Unset name/about/avatar/banner = do not publish a public kind-0 card.
                let bot_name = std::env::var("VECTOR_BOT_NAME")
                    .map(|s| s.trim().to_string())
                    .unwrap_or_default();
                let bot_about = std::env::var("VECTOR_BOT_ABOUT")
                    .map(|s| s.trim().to_string())
                    .unwrap_or_default();
                let env_image = |key: &str| {
                    std::env::var(key)
                        .ok()
                        .map(|s| s.trim().to_string())
                        .filter(|s| !s.is_empty())
                        .map(PathBuf::from)
                };
                let avatar_path = env_image("VECTOR_BOT_AVATAR");
                let banner_path = env_image("VECTOR_BOT_BANNER");
                let profile_dir = data_dir.clone();
                let listen_state = state.clone();
                let handler_state = listen_state.clone();
                tokio::spawn(async move {
                    let result = bot
                        .on_event(move |b, event| {
                            let state = handler_state.clone();
                            let bot_name = bot_name.clone();
                            let bot_about = bot_about.clone();
                            let avatar_path = avatar_path.clone();
                            let banner_path = banner_path.clone();
                            let profile_dir = profile_dir.clone();
                            async move {
                                events::handle_bot_event(
                                    &state,
                                    &b,
                                    event,
                                    &bot_name,
                                    &bot_about,
                                    avatar_path.as_deref(),
                                    banner_path.as_deref(),
                                    Some(profile_dir.as_path()),
                                )
                                .await;
                            }
                        })
                        .await;
                    match result {
                        Ok(()) => eprintln!("[vector-bridge] listen ended"),
                        Err(err) => eprintln!("[vector-bridge] listen ended: {err}"),
                    }
                    listen_state.events().disconnect_all();
                    request_stop(&stop_tx, Stop::ListenEnded);
                });
            }
            Err(err) => {
                eprintln!("[vector-bridge] login failed: {err}");
                request_stop(&stop_tx, Stop::LoginFailed);
            }
        }
    });
}

async fn serve() -> Result<ExitCode, CliError> {
    let token = std::env::var("VECTOR_SIDECAR_TOKEN").unwrap_or_default();
    if token.trim().is_empty() {
        return Err(CliError::EmptyToken);
    }

    let stub = stub_mode();
    let data_dir = if stub {
        None
    } else {
        Some(require_runtime_data_dir()?)
    };

    let host = env_or("VECTOR_BRIDGE_HOST", DEFAULT_HOST);
    if !is_loopback(&host) {
        eprintln!(
            "[vector-bridge] warning: VECTOR_BRIDGE_HOST={host} is not loopback; \
             LAN clients with the token can reach this sidecar"
        );
    }
    let port: u16 = env_or("VECTOR_BRIDGE_PORT", &DEFAULT_PORT.to_string())
        .parse()
        .map_err(|_| CliError::Other("VECTOR_BRIDGE_PORT must be a port number".into()))?;

    let ping_interval = parse_ms_env("VECTOR_SSE_PING_MS").unwrap_or(DEFAULT_SSE_PING);
    let state = AppState::new(token, ping_interval);

    let addr = format!("{host}:{port}");
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| CliError::Other(format!("bind {addr}: {e}")))?;
    let bound = listener
        .local_addr()
        .map_err(|e| CliError::Other(e.to_string()))?;
    eprintln!("[vector-bridge] listening on {bound}");

    let (stop_tx, stop_rx) = watch::channel(Stop::Running);

    match data_dir {
        Some(data_dir) => spawn_vector_bot(state.clone(), data_dir, stop_tx.clone()),
        None => {
            if let Some(delay) = parse_ms_env("VECTOR_STUB_READY_AFTER_MS") {
                let ready_state = state.clone();
                tokio::spawn(async move {
                    tokio::time::sleep(delay).await;
                    ready_state.mark_ready(stub_npub()).await;
                });
            }
        }
    }

    let watch_stdin = std::env::var("VECTOR_SIDECAR_WATCH_STDIN")
        .map(|v| v == "1")
        .unwrap_or(false);

    let signal_tx = stop_tx.clone();
    tokio::spawn(async move {
        wait_shutdown(watch_stdin).await;
        request_stop(&signal_tx, Stop::Graceful);
    });

    let shutdown_state = state.clone();
    let shutdown_rx = stop_rx.clone();
    let shutdown = async move {
        let _ = wait_stop(shutdown_rx).await;
        shutdown_state.events().disconnect_all();
    };

    let force_rx = stop_rx.clone();
    let server = axum::serve(listener, router(state)).with_graceful_shutdown(shutdown);
    tokio::select! {
        result = server => {
            result.map_err(|e| CliError::Other(e.to_string()))?;
        }
        _ = async {
            let _ = wait_stop(force_rx).await;
            tokio::time::sleep(Duration::from_secs(2)).await;
        } => {}
    }

    let why = *stop_rx.borrow();
    match why {
        Stop::LoginFailed | Stop::ListenEnded => Ok(ExitCode::from(1)),
        _ => Ok(ExitCode::SUCCESS),
    }
}

async fn wait_shutdown(watch_stdin: bool) {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };

    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    let stdin_eof = async {
        if watch_stdin {
            use tokio::io::AsyncReadExt;
            let mut stdin = tokio::io::stdin();
            let mut buf = [0u8; 256];
            loop {
                match stdin.read(&mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
            }
        } else {
            std::future::pending::<()>().await;
        }
    };

    tokio::select! {
        _ = ctrl_c => {}
        _ = terminate => {}
        _ = stdin_eof => {}
    }
}

/// Community invite policy from env. Never `.public()` — a public bot is a
/// spam surface. Default is whitelist of `VECTOR_TRUSTED_INVITERS`, falling
/// back to `VECTOR_ALLOWED_USERS`. Empty list or `VECTOR_INVITE_POLICY=manual`
/// parks every invite.
fn invite_policy_from_env() -> InvitePolicy {
    invite_policy_from_parts(
        &std::env::var("VECTOR_INVITE_POLICY").unwrap_or_default(),
        std::env::var("VECTOR_TRUSTED_INVITERS").ok(),
        std::env::var("VECTOR_ALLOWED_USERS").ok(),
    )
}

fn invite_policy_from_parts(
    mode: &str,
    trusted: Option<String>,
    allowed: Option<String>,
) -> InvitePolicy {
    let mode = mode.trim().to_ascii_lowercase();
    if mode == "manual" {
        eprintln!("[vector-bridge] invite policy=manual (park all community invites)");
        return InvitePolicy::Manual;
    }
    if mode == "public" {
        eprintln!(
            "[vector-bridge] VECTOR_INVITE_POLICY=public is not supported; \
             using whitelist/manual instead"
        );
    }
    let raw = trusted
        .filter(|s| !s.trim().is_empty())
        .or(allowed)
        .unwrap_or_default();
    let npubs = parse_invite_whitelist(&raw);
    if npubs.is_empty() {
        eprintln!(
            "[vector-bridge] invite policy=manual \
             (no VECTOR_TRUSTED_INVITERS / VECTOR_ALLOWED_USERS)"
        );
        InvitePolicy::Manual
    } else {
        eprintln!(
            "[vector-bridge] invite policy=whitelist ({} inviter{})",
            npubs.len(),
            if npubs.len() == 1 { "" } else { "s" }
        );
        InvitePolicy::Whitelist(npubs)
    }
}

fn parse_invite_whitelist(raw: &str) -> Vec<String> {
    raw.split(',')
        .filter_map(|s| {
            let s = s.trim();
            if s.is_empty() {
                return None;
            }
            PublicKey::parse(s).ok().and_then(|pk| pk.to_bech32().ok())
        })
        .collect()
}

fn env_or(key: &str, default: &str) -> String {
    match std::env::var(key) {
        Ok(v) if !v.is_empty() => v,
        _ => default.to_string(),
    }
}

fn parse_ms_env(key: &str) -> Option<Duration> {
    let raw = std::env::var(key).ok()?;
    let ms: u64 = raw.parse().ok()?;
    Some(Duration::from_millis(ms))
}

fn is_loopback(host: &str) -> bool {
    match host.parse::<IpAddr>() {
        Ok(ip) => ip.is_loopback(),
        Err(_) => host.eq_ignore_ascii_case("localhost"),
    }
}

enum CliError {
    Usage(String),
    InvalidNsec,
    InvalidMnemonic,
    EmptyToken,
    Io { path: PathBuf, source: io::Error },
    Other(String),
}

impl CliError {
    fn usage(msg: impl Into<String>) -> Self {
        CliError::Usage(msg.into())
    }

    fn io(path: impl AsRef<Path>, source: io::Error) -> Self {
        CliError::Io {
            path: path.as_ref().to_path_buf(),
            source,
        }
    }

    fn write_stderr(&self) {
        match self {
            CliError::InvalidNsec => {
                let _ = writeln!(
                    io::stderr(),
                    "{}",
                    json!({ "error": "not a valid nsec", "code": "invalid_nsec" })
                );
            }
            CliError::InvalidMnemonic => {
                let _ = writeln!(
                    io::stderr(),
                    "{}",
                    json!({
                        "error": "not a valid BIP-39 mnemonic",
                        "code": "invalid_mnemonic"
                    })
                );
            }
            CliError::EmptyToken => {
                let _ = writeln!(
                    io::stderr(),
                    "vector-bridge: VECTOR_SIDECAR_TOKEN is empty; refusing to bind"
                );
            }
            CliError::Usage(msg) => {
                let _ = writeln!(io::stderr(), "vector-bridge: {msg}");
                print_usage();
            }
            CliError::Io { path, source } => {
                let _ = writeln!(io::stderr(), "vector-bridge: {}: {source}", path.display());
            }
            CliError::Other(msg) => {
                let _ = writeln!(io::stderr(), "vector-bridge: {msg}");
            }
        }
    }
}

#[cfg(test)]
mod invite_policy_tests {
    use super::*;

    const NPUB: &str = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6";
    const HEX: &str = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d";

    fn whitelist(policy: &InvitePolicy) -> Option<&[String]> {
        match policy {
            InvitePolicy::Whitelist(list) => Some(list.as_slice()),
            _ => None,
        }
    }

    #[test]
    fn manual_mode_parks_even_with_allowlist() {
        let policy = invite_policy_from_parts("manual", None, Some(NPUB.into()));
        assert!(matches!(policy, InvitePolicy::Manual));
    }

    #[test]
    fn empty_lists_are_manual() {
        let policy = invite_policy_from_parts("", None, Some("  ,  ".into()));
        assert!(matches!(policy, InvitePolicy::Manual));
    }

    #[test]
    fn trusted_inviters_win_over_allowed_users() {
        let policy =
            invite_policy_from_parts("whitelist", Some(NPUB.into()), Some("npub1nope".into()));
        let list = whitelist(&policy).expect("whitelist");
        assert_eq!(list, &[NPUB]);
    }

    #[test]
    fn allowed_users_are_the_default_whitelist() {
        let policy = invite_policy_from_parts("", None, Some(format!("{HEX},not-an-npub")));
        let list = whitelist(&policy).expect("whitelist");
        assert_eq!(list, &[NPUB]);
    }

    #[test]
    fn public_mode_is_refused() {
        let policy = invite_policy_from_parts("public", None, Some(NPUB.into()));
        assert!(whitelist(&policy).is_some());
    }
}
