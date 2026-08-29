//! Identity bootstrap CLI for the Hermes Vector sidecar.
//!
//! `--check` / `--setup` read and write `<VECTOR_DATA_DIR>/identity.nsec` offline.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use nostr::nips::nip06::FromMnemonic;
use serde_json::json;
use vector_sdk::nostr::{FromBech32, Keys, SecretKey, ToBech32};
use vector_sdk::VectorBot;

const IDENTITY_FILE: &str = "identity.nsec";

fn main() -> ExitCode {
    harden_process();
    match run(std::env::args().collect()) {
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

fn run(args: Vec<String>) -> Result<ExitCode, CliError> {
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
    }
}

enum Mode {
    Help,
    Check,
    Setup {
        nsec_file: Option<PathBuf>,
        mnemonic_file: Option<PathBuf>,
    },
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
    Err(CliError::usage("specify --check or --setup"))
}

fn print_usage() {
    eprintln!(
        "Usage: vector-bridge --check | --setup [--nsec-file PATH | --mnemonic-file PATH]\n\
         \n\
         Identity is <VECTOR_DATA_DIR>/identity.nsec. VECTOR_DATA_DIR is required.\n\
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
        fs::create_dir_all(parent).map_err(|err| CliError::io(parent, err))?;
    }
    fs::write(path, nsec).map_err(|err| CliError::io(path, err))?;
    restrict_to_owner(path);
    Ok(())
}

#[cfg(unix)]
fn restrict_to_owner(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn restrict_to_owner(_path: &Path) {}

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

    let (nsec, status) = if let Some(src) = nsec_file {
        (load_nsec_file(&src)?, "restored")
    } else if let Some(src) = mnemonic_file {
        (nsec_from_mnemonic_file(&src)?, "restored")
    } else {
        let nsec = VectorBot::generate_nsec().map_err(|e| CliError::Other(e.to_string()))?;
        (nsec, "created")
    };

    let npub = npub_from_nsec(&nsec)?;
    write_identity(&path, &nsec)?;
    if status == "created" {
        eprintln!(
            "[vector-bridge] Created a new bot identity {} (stored at {}). \
             Back it up — that file is the bot.",
            npub,
            path.display()
        );
    }
    print_json(json!({ "status": status, "npub": npub }));
    Ok(ExitCode::SUCCESS)
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

fn nsec_from_mnemonic_file(src: &Path) -> Result<String, CliError> {
    let contents = fs::read_to_string(src).map_err(|err| CliError::io(src, err))?;
    let mnemonic = contents.trim();
    if mnemonic.is_empty() {
        return Err(CliError::InvalidMnemonic);
    }
    let keys = Keys::from_mnemonic(mnemonic, None).map_err(|_| CliError::InvalidMnemonic)?;
    keys.secret_key()
        .to_bech32()
        .map_err(|e| CliError::Other(e.to_string()))
}

fn print_json(value: serde_json::Value) {
    println!("{value}");
}

enum CliError {
    Usage(String),
    InvalidNsec,
    InvalidMnemonic,
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
