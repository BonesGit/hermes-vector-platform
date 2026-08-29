//! Offline identity CLI: --check never mints; --setup writes identity.nsec.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;
use tempfile::TempDir;

fn bin_path() -> PathBuf {
    if let Some(p) = std::env::var_os("CARGO_BIN_EXE_vector_bridge") {
        return PathBuf::from(p);
    }
    let target_dir = std::env::var_os("CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target"));
    let profile = if cfg!(debug_assertions) {
        "debug"
    } else {
        "release"
    };
    target_dir.join(profile).join("vector-bridge")
}

fn bin() -> Command {
    Command::new(bin_path())
}

fn run(dir: &Path, args: &[&str]) -> Output {
    bin()
        .args(args)
        .env("VECTOR_DATA_DIR", dir)
        .env_remove("VECTOR_NSEC")
        .env_remove("VECTOR_MNEMONIC")
        .output()
        .expect("spawn vector-bridge")
}

fn stdout_json(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|_| {
        panic!(
            "stdout is not JSON: {}",
            String::from_utf8_lossy(&output.stdout)
        )
    })
}

fn assert_no_nsec_on_stdout(output: &Output) {
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(!stdout.contains("nsec1"), "nsec leaked to stdout: {stdout}");
}

fn identity_path(dir: &Path) -> std::path::PathBuf {
    dir.join("identity.nsec")
}

#[test]
fn check_empty_dir_is_not_registered() {
    let dir = TempDir::new().unwrap();
    let out = run(dir.path(), &["--check"]);
    assert!(
        out.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(stdout_json(&out)["status"], "not_registered");
    assert!(!identity_path(dir.path()).exists());
    assert_no_nsec_on_stdout(&out);
}

#[test]
fn check_does_not_create_identity() {
    let dir = TempDir::new().unwrap();
    let _ = run(dir.path(), &["--check"]);
    let _ = run(dir.path(), &["--check"]);
    assert!(!identity_path(dir.path()).exists());
    let entries: Vec<_> = fs::read_dir(dir.path()).unwrap().collect();
    assert!(
        entries.is_empty(),
        "check must not write into VECTOR_DATA_DIR"
    );
}

#[test]
fn setup_then_check_same_npub() {
    let dir = TempDir::new().unwrap();
    let created = run(dir.path(), &["--setup"]);
    assert!(
        created.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&created.stderr)
    );
    assert_no_nsec_on_stdout(&created);
    let created_json = stdout_json(&created);
    assert_eq!(created_json["status"], "created");
    let npub = created_json["npub"].as_str().expect("npub");
    assert!(npub.starts_with("npub1"), "npub={npub}");

    assert!(identity_path(dir.path()).exists());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(identity_path(dir.path()))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);
    }

    let checked = run(dir.path(), &["--check"]);
    assert!(checked.status.success());
    assert_no_nsec_on_stdout(&checked);
    let checked_json = stdout_json(&checked);
    assert_eq!(checked_json["status"], "existing");
    assert_eq!(checked_json["npub"], npub);

    let again = run(dir.path(), &["--setup"]);
    assert!(again.status.success());
    assert_no_nsec_on_stdout(&again);
    let again_json = stdout_json(&again);
    assert_eq!(again_json["status"], "existing");
    assert_eq!(again_json["npub"], npub);
}

#[test]
fn setup_restores_from_nsec_file() {
    let src_dir = TempDir::new().unwrap();
    let minted = run(src_dir.path(), &["--setup"]);
    let npub = stdout_json(&minted)["npub"].as_str().unwrap().to_string();
    let nsec_src = src_dir.path().join("exported.nsec");
    fs::copy(identity_path(src_dir.path()), &nsec_src).unwrap();

    let dest = TempDir::new().unwrap();
    let restored = run(
        dest.path(),
        &["--setup", "--nsec-file", nsec_src.to_str().unwrap()],
    );
    assert!(
        restored.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&restored.stderr)
    );
    assert_no_nsec_on_stdout(&restored);
    let body = stdout_json(&restored);
    assert_eq!(body["status"], "restored");
    assert_eq!(body["npub"], npub);
}

#[test]
fn setup_restores_from_mnemonic_file() {
    // BIP-39 test vector (12× abandon + about).
    let dir = TempDir::new().unwrap();
    let mnemonic_path = dir.path().join("seed.txt");
    fs::write(
        &mnemonic_path,
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about\n",
    )
    .unwrap();
    let data = dir.path().join("sdk");
    fs::create_dir(&data).unwrap();

    let out = run(
        &data,
        &[
            "--setup",
            "--mnemonic-file",
            mnemonic_path.to_str().unwrap(),
        ],
    );
    assert!(
        out.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_no_nsec_on_stdout(&out);
    let body = stdout_json(&out);
    assert_eq!(body["status"], "restored");
    assert_eq!(
        body["npub"],
        "npub1az708q3kd9zy6z6f44zav5ygvdwelkzspf6mtusttx47lft2z38sghk0w7"
    );
}

#[test]
fn setup_invalid_nsec_file_does_not_create_identity() {
    let dir = TempDir::new().unwrap();
    let src = dir.path().join("bad.nsec");
    fs::write(&src, "nsec1thisisnotvalid").unwrap();
    let data = dir.path().join("sdk");
    fs::create_dir(&data).unwrap();

    let out = run(&data, &["--setup", "--nsec-file", src.to_str().unwrap()]);
    assert!(!out.status.success());
    let stderr = String::from_utf8_lossy(&out.stderr);
    let err: Value = serde_json::from_str(stderr.trim()).expect("stderr json");
    assert_eq!(err["code"], "invalid_nsec");
    assert!(!identity_path(&data).exists());
    assert_no_nsec_on_stdout(&out);
}

#[test]
fn setup_invalid_mnemonic_file_does_not_create_identity() {
    let dir = TempDir::new().unwrap();
    let src = dir.path().join("bad.txt");
    fs::write(&src, "not a mnemonic").unwrap();
    let data = dir.path().join("sdk");
    fs::create_dir(&data).unwrap();

    let out = run(
        &data,
        &["--setup", "--mnemonic-file", src.to_str().unwrap()],
    );
    assert!(!out.status.success());
    let stderr = String::from_utf8_lossy(&out.stderr);
    let err: Value = serde_json::from_str(stderr.trim()).expect("stderr json");
    assert_eq!(err["code"], "invalid_mnemonic");
    assert!(!identity_path(&data).exists());
    assert_no_nsec_on_stdout(&out);
}

#[test]
fn check_invalid_nsec_does_not_rewrite() {
    let dir = TempDir::new().unwrap();
    let path = identity_path(dir.path());
    fs::write(&path, "nsec1thisisnotvalid").unwrap();
    let original = fs::read(&path).unwrap();

    let out = run(dir.path(), &["--check"]);
    assert!(!out.status.success());
    let stderr = String::from_utf8_lossy(&out.stderr);
    let err: Value = serde_json::from_str(stderr.trim()).expect("stderr json");
    assert_eq!(err["code"], "invalid_nsec");
    assert_eq!(fs::read(&path).unwrap(), original);
    assert_no_nsec_on_stdout(&out);
}

#[test]
fn setup_ignores_nsec_in_environment() {
    let minted_dir = TempDir::new().unwrap();
    let minted = run(minted_dir.path(), &["--setup"]);
    let env_npub = stdout_json(&minted)["npub"].as_str().unwrap().to_string();
    let env_nsec = fs::read_to_string(identity_path(minted_dir.path())).unwrap();

    let dir = TempDir::new().unwrap();
    let out = bin()
        .arg("--setup")
        .env("VECTOR_DATA_DIR", dir.path())
        .env("VECTOR_NSEC", env_nsec.trim())
        .output()
        .unwrap();
    assert!(out.status.success());
    assert_no_nsec_on_stdout(&out);
    let body = stdout_json(&out);
    assert_eq!(body["status"], "created");
    assert_ne!(body["npub"], env_npub);
}

#[test]
fn missing_data_dir_fails() {
    let out = bin()
        .arg("--check")
        .env_remove("VECTOR_DATA_DIR")
        .output()
        .unwrap();
    assert!(!out.status.success());
}
