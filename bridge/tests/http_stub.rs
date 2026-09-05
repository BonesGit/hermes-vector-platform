//! HTTP stub contract: token, /live, /health, SSE, 413, stdin-EOF.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tempfile::TempDir;

const TOKEN: &str = "test-sidecar-token";
const VALID_NPUB: &str = "npub1az708q3kd9zy6z6f44zav5ygvdwelkzspf6mtusttx47lft2z38sghk0w7";

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

fn free_port() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.local_addr().unwrap().port()
}

struct Server {
    child: Child,
    port: u16,
    token: String,
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Server {
    fn url(&self, path: &str) -> String {
        format!("http://127.0.0.1:{}{path}", self.port)
    }
}

fn spawn_server(extra: &[(&str, &str)]) -> Server {
    spawn_server_stdin(extra, Stdio::null())
}

fn spawn_server_stdin(extra: &[(&str, &str)], stdin: Stdio) -> Server {
    let port = free_port();
    let mut cmd = bin();
    cmd.env("VECTOR_SIDECAR_TOKEN", TOKEN)
        .env("VECTOR_BRIDGE_HOST", "127.0.0.1")
        .env("VECTOR_BRIDGE_PORT", port.to_string())
        .env("VECTOR_STUB", "1")
        .env_remove("VECTOR_DATA_DIR")
        .env_remove("VECTOR_SIDECAR_WATCH_STDIN")
        .env_remove("VECTOR_STUB_READY_AFTER_MS")
        .env_remove("VECTOR_SSE_PING_MS")
        .stdin(stdin)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in extra {
        cmd.env(k, v);
    }
    let child = cmd.spawn().expect("spawn vector-bridge");
    let mut server = Server {
        child,
        port,
        token: TOKEN.to_string(),
    };
    wait_live(&mut server);
    server
}

fn wait_live(server: &mut Server) {
    let url = server.url("/live");
    let deadline = Instant::now() + Duration::from_secs(8);
    loop {
        if let Some(status) = server.child.try_wait().unwrap() {
            let stderr = {
                let mut buf = String::new();
                if let Some(ref mut s) = server.child.stderr {
                    let _ = s.read_to_string(&mut buf);
                }
                buf
            };
            panic!("vector-bridge exited early {status:?} stderr={stderr}");
        }
        if let Ok(resp) = agent().get(&url).set("Connection", "close").call() {
            if resp.status() == 200 {
                return;
            }
        }
        if Instant::now() > deadline {
            panic!("server on port {} did not become live", server.port);
        }
        thread::sleep(Duration::from_millis(30));
    }
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(5))
        .build()
}

fn get(server: &Server, path: &str, token: Option<&str>) -> (u16, Value) {
    let mut req = agent().get(&server.url(path)).set("Connection", "close");
    if let Some(t) = token {
        req = req.set("X-Hermes-Sidecar-Token", t);
    }
    status_json(req.call())
}

fn post(server: &Server, path: &str, token: Option<&str>, body: Value) -> (u16, Value) {
    let mut req = agent().post(&server.url(path)).set("Connection", "close");
    if let Some(t) = token {
        req = req.set("X-Hermes-Sidecar-Token", t);
    }
    status_json(req.send_json(body))
}

fn status_json(result: Result<ureq::Response, ureq::Error>) -> (u16, Value) {
    match result {
        Ok(resp) => {
            let status = resp.status();
            let body = resp.into_json().unwrap_or(json!({}));
            (status, body)
        }
        Err(ureq::Error::Status(code, resp)) => {
            let body = resp.into_json().unwrap_or(json!({}));
            (code, body)
        }
        Err(e) => panic!("http error: {e}"),
    }
}

fn stdout_json(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|_| {
        panic!(
            "stdout is not JSON: {}",
            String::from_utf8_lossy(&output.stdout)
        )
    })
}

#[test]
fn empty_token_exits_1_and_does_not_bind() {
    for token in [None, Some(""), Some("   ")] {
        let port = free_port();
        let mut cmd = bin();
        cmd.env("VECTOR_BRIDGE_PORT", port.to_string())
            .env("VECTOR_BRIDGE_HOST", "127.0.0.1")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        match token {
            None => {
                cmd.env_remove("VECTOR_SIDECAR_TOKEN");
            }
            Some(t) => {
                cmd.env("VECTOR_SIDECAR_TOKEN", t);
            }
        }
        let out = cmd.output().unwrap();
        assert_eq!(
            out.status.code(),
            Some(1),
            "token={token:?} stderr={}",
            String::from_utf8_lossy(&out.stderr)
        );
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(stderr.contains("VECTOR_SIDECAR_TOKEN"), "stderr={stderr}");
        assert!(
            ureq::get(&format!("http://127.0.0.1:{port}/live"))
                .timeout(Duration::from_millis(200))
                .call()
                .is_err(),
            "must not bind with empty token"
        );
    }
}

#[test]
fn live_works_without_token() {
    let server = spawn_server(&[]);
    let (status, body) = get(&server, "/live", None);
    assert_eq!(status, 200);
    assert_eq!(body, json!({ "ok": true }));
}

#[test]
fn health_401_without_token() {
    let server = spawn_server(&[]);
    let (status, body) = get(&server, "/health", None);
    assert_eq!(status, 401);
    assert_eq!(body["code"], "unauthorized");
    assert!(body["error"].is_string());

    let (status, body) = get(&server, "/health", Some("wrong-token"));
    assert_eq!(status, 401);
    assert_eq!(body["code"], "unauthorized");
}

#[test]
fn health_starting_then_ready_via_test_hook() {
    let server = spawn_server(&[]);
    let (status, body) = get(&server, "/health", Some(&server.token));
    assert_eq!(status, 200);
    assert_eq!(body["status"], "starting");
    assert!(body.get("npub").is_none());

    let (status, _) = get(&server, "/npub", Some(&server.token));
    assert_eq!(status, 503);

    let (status, body) = post(&server, "/__test/ready", Some(&server.token), json!({}));
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);

    let (status, body) = get(&server, "/health", Some(&server.token));
    assert_eq!(status, 200);
    assert_eq!(body["status"], "ready");
    assert_eq!(body["npub"], "npub1stub");

    let (status, body) = get(&server, "/npub", Some(&server.token));
    assert_eq!(status, 200);
    assert_eq!(body["npub"], "npub1stub");
}

#[test]
fn health_ready_after_timer() {
    let server = spawn_server(&[("VECTOR_STUB_READY_AFTER_MS", "80")]);
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let (status, body) = get(&server, "/health", Some(&server.token));
        assert_eq!(status, 200);
        if body["status"] == "ready" {
            assert_eq!(body["npub"], "npub1stub");
            return;
        }
        if Instant::now() > deadline {
            panic!("never became ready: {body}");
        }
        thread::sleep(Duration::from_millis(30));
    }
}

#[test]
fn send_validates_npub_and_requires_ready() {
    let server = spawn_server(&[]);
    let (status, body) = post(
        &server,
        "/send",
        Some(&server.token),
        json!({ "to": VALID_NPUB, "body": "hi" }),
    );
    assert_eq!(status, 503);
    assert_eq!(body["code"], "not_ready");

    let (status, body) = post(
        &server,
        "/send",
        Some(&server.token),
        json!({ "to": "not-an-npub", "body": "hi" }),
    );
    assert_eq!(status, 400);
    assert_eq!(body["code"], "invalid_npub");

    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/send",
        Some(&server.token),
        json!({ "to": VALID_NPUB, "body": "hi", "reply_to": "abc" }),
    );
    assert_eq!(status, 200, "{body}");
    let id = body["id"].as_str().expect("id");
    assert_eq!(id.len(), 64);

    let channel = "a".repeat(64);
    let (status, body) = post(
        &server,
        "/send",
        Some(&server.token),
        json!({ "to": channel, "body": "hi group" }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["id"].as_str().expect("id").len(), 64);

    let (status, body) = post(
        &server,
        "/typing",
        Some(&server.token),
        json!({ "to": channel }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);

    let (status, body) = post(
        &server,
        "/typing",
        Some(&server.token),
        json!({ "to": VALID_NPUB }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
}

#[test]
fn edit_validates_and_returns_original_id() {
    let server = spawn_server(&[]);
    let (status, body) = post(
        &server,
        "/edit",
        Some(&server.token),
        json!({ "to": VALID_NPUB, "message_id": "deadbeef", "body": "hi" }),
    );
    assert_eq!(status, 503);
    assert_eq!(body["code"], "not_ready");

    let (status, body) = post(
        &server,
        "/edit",
        Some(&server.token),
        json!({ "to": "not-an-npub", "message_id": "deadbeef", "body": "hi" }),
    );
    assert_eq!(status, 400);
    assert_eq!(body["code"], "invalid_npub");

    post(&server, "/__test/ready", Some(&server.token), json!({}));

    let (status, body) = post(
        &server,
        "/edit",
        Some(&server.token),
        json!({ "to": VALID_NPUB, "body": "hi" }),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");

    let (status, body) = post(
        &server,
        "/edit",
        Some(&server.token),
        json!({ "to": VALID_NPUB, "message_id": "deadbeef", "body": "" }),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");

    let (status, body) = post(
        &server,
        "/edit",
        Some(&server.token),
        json!({ "to": VALID_NPUB, "message_id": "deadbeef", "body": "edited" }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["id"], "deadbeef");
    assert_eq!(body["edit_id"].as_str().expect("edit_id").len(), 64);

    let channel = "a".repeat(64);
    let (status, body) = post(
        &server,
        "/edit",
        Some(&server.token),
        json!({ "to": channel, "message_id": "cafebabe", "body": "group edit" }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["id"], "cafebabe");
}

#[test]
fn payload_over_64kib_is_413() {
    let server = spawn_server(&[]);
    let too_big = "x".repeat(64 * 1024 + 1);
    let result = agent()
        .post(&server.url("/send"))
        .set("X-Hermes-Sidecar-Token", &server.token)
        .set("Content-Type", "application/json")
        .send_string(&too_big);
    let (status, body) = status_json(result);
    assert_eq!(status, 413, "{body}");
    assert_eq!(body["code"], "payload_too_large");
}

#[test]
fn post_block_requires_npub_and_ready() {
    let server = spawn_server(&[]);
    let (status, body) = post(&server, "/block", Some(&server.token), json!({}));
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");

    let (status, body) = post(
        &server,
        "/block",
        Some(&server.token),
        json!({"npub": "npub1abc"}),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "invalid_npub");

    let (status, body) = post(
        &server,
        "/block",
        Some(&server.token),
        json!({"npub": VALID_NPUB}),
    );
    assert_eq!(status, 503, "{body}");
    assert_eq!(body["code"], "not_ready");

    let (status, body) = get(&server, "/block", Some(&server.token));
    assert_eq!(status, 503, "{body}");
    assert_eq!(body["code"], "not_ready");

    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/block",
        Some(&server.token),
        json!({"npub": VALID_NPUB}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["npub"], VALID_NPUB);
    assert_eq!(body["blocked"], true);

    let (status, body) = post(
        &server,
        "/block",
        Some(&server.token),
        json!({"npub": VALID_NPUB, "unblock": true}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["blocked"], false);

    let (status, body) = get(&server, "/block", Some(&server.token));
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["blocked"], json!([]));

    let (status, body) = post(&server, "/block", None, json!({"npub": VALID_NPUB}));
    assert_eq!(status, 401, "{body}");
    assert_eq!(body["code"], "unauthorized");
}

#[test]
fn parked_invite_routes_require_ready_and_community_id() {
    let server = spawn_server(&[]);
    let cid = "aa".repeat(32);

    let (status, body) = get(&server, "/invites", Some(&server.token));
    assert_eq!(status, 503, "{body}");
    assert_eq!(body["code"], "not_ready");

    let (status, body) = post(&server, "/invites/accept", Some(&server.token), json!({}));
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");

    let (status, body) = post(
        &server,
        "/invites/accept",
        Some(&server.token),
        json!({"community_id": "not-hex"}),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");

    let (status, body) = post(
        &server,
        "/invites/accept",
        Some(&server.token),
        json!({"community_id": cid}),
    );
    assert_eq!(status, 503, "{body}");
    assert_eq!(body["code"], "not_ready");

    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = get(&server, "/invites", Some(&server.token));
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["invites"], json!([]));

    let (status, body) = post(
        &server,
        "/invites/accept",
        Some(&server.token),
        json!({"community_id": cid}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["community_id"], cid);
    assert_eq!(body["channels"], json!([]));

    let (status, body) = post(
        &server,
        "/invites/decline",
        Some(&server.token),
        json!({"community_id": cid}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["community_id"], cid);

    let (status, body) = get(&server, "/invites", None);
    assert_eq!(status, 401, "{body}");
    assert_eq!(body["code"], "unauthorized");
}

#[test]
fn post_delete_requires_target_and_ready() {
    let server = spawn_server(&[]);
    let (status, body) = post(
        &server,
        "/delete",
        Some(&server.token),
        json!({"to": VALID_NPUB, "message_id": "deadbeef"}),
    );
    assert_eq!(status, 503, "{body}");
    assert_eq!(body["code"], "not_ready");

    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/delete",
        Some(&server.token),
        json!({"to": VALID_NPUB}),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");

    let (status, body) = post(
        &server,
        "/delete",
        Some(&server.token),
        json!({"to": VALID_NPUB, "message_id": "deadbeef"}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["id"], "deadbeef");

    let channel = "a".repeat(64);
    let (status, body) = post(
        &server,
        "/delete",
        Some(&server.token),
        json!({"to": channel, "message_id": "cafebabe"}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["id"], "cafebabe");
}

#[test]
fn get_profile_requires_npub_and_ready() {
    let server = spawn_server(&[]);
    let (status, body) = get(&server, "/profile", Some(&server.token));
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");

    let (status, body) = get(&server, "/profile?npub=npub1abc", Some(&server.token));
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "invalid_npub");

    let (status, body) = get(
        &server,
        &format!("/profile?npub={VALID_NPUB}"),
        Some(&server.token),
    );
    assert_eq!(status, 503, "{body}");
    assert_eq!(body["code"], "not_ready");

    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = get(
        &server,
        &format!("/profile?npub={VALID_NPUB}"),
        Some(&server.token),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["npub"], VALID_NPUB);
    assert_eq!(body["name"], "");
    assert_eq!(body["display_name"], "");
    assert_eq!(body["about"], "");
    assert_eq!(body["bot"], false);

    let (status, body) = get(&server, "/profile?npub=npub1abc", None);
    assert_eq!(status, 401, "{body}");
    assert_eq!(body["code"], "unauthorized");
}

#[test]
fn post_profile_rejects_relative_avatar_path() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/profile",
        Some(&server.token),
        json!({"name": "Hermes", "avatar_path": "relative.png"}),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");
}

#[test]
fn post_profile_rejects_relative_banner_path() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/profile",
        Some(&server.token),
        json!({"banner_path": "relative.png"}),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");
}

#[test]
fn post_react_requires_message_id() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/react",
        Some(&server.token),
        json!({"to": VALID_NPUB, "emoji": "❌"}),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");
}

#[test]
fn post_react_stub_ok() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/react",
        Some(&server.token),
        json!({"to": VALID_NPUB, "message_id": "deadbeef", "emoji": "❌"}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
}

#[test]
fn post_react_remove_without_emoji_is_ok_in_stub() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/react",
        Some(&server.token),
        json!({"to": VALID_NPUB, "message_id": "deadbeef", "remove": true}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
}

#[test]
fn post_react_custom_emoji_url_ok_in_stub() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/react",
        Some(&server.token),
        json!({
            "to": VALID_NPUB,
            "message_id": "deadbeef",
            "emoji": ":wave:",
            "emoji_url": "https://example.com/wave.png"
        }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
}

#[test]
fn post_profile_name_only_is_ok_in_stub() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/profile",
        Some(&server.token),
        json!({"name": "Hermes"}),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
}

#[test]
fn send_file_stub_requires_absolute_file() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let (status, body) = post(
        &server,
        "/send-file",
        Some(&server.token),
        json!({"to": VALID_NPUB, "path": "relative.bin"}),
    );
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "bad_request");

    let tmp = tempfile::NamedTempFile::new().unwrap();
    let (status, body) = post(
        &server,
        "/send-file",
        Some(&server.token),
        json!({"to": VALID_NPUB, "path": tmp.path().to_string_lossy()}),
    );
    assert_eq!(status, 200, "{body}");
    assert!(body["id"].as_str().unwrap().len() >= 8);

    let channel = "a".repeat(64);
    let tmp_ch = tempfile::NamedTempFile::new().unwrap();
    let (status, body) = post(
        &server,
        "/send-file",
        Some(&server.token),
        json!({"to": channel, "path": tmp_ch.path().to_string_lossy()}),
    );
    assert_eq!(status, 200, "{body}");
    assert!(body["id"].as_str().unwrap().len() >= 8);
}

#[test]
fn download_attachment_stub_writes_dest() {
    let server = spawn_server(&[]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let dir = tempfile::TempDir::new().unwrap();
    let dest = dir.path().join("inbox").join("notes.pdf");
    let (status, body) = post(
        &server,
        "/download-attachment",
        Some(&server.token),
        json!({
            "dest": dest.to_string_lossy(),
            "author_npub": VALID_NPUB,
            "attachment": {
                "id": "att1",
                "key": "",
                "nonce": "",
                "extension": "pdf",
                "name": "notes.pdf",
                "url": "",
                "path": "",
                "size": 1,
                "downloading": false,
                "downloaded": true
            }
        }),
    );
    assert_eq!(status, 200, "{body}");
    assert!(dest.is_file(), "stub should create dest");
}

#[test]
fn sse_ping_and_fake_inject() {
    let server = spawn_server(&[("VECTOR_SSE_PING_MS", "150")]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));

    let mut stream = open_sse(server.port, &server.token);
    let (status, body) = post(
        &server,
        "/__test/inject",
        Some(&server.token),
        json!({
            "id": "deadbeef",
            "chat_id": VALID_NPUB,
            "npub": VALID_NPUB,
            "is_group": false,
            "is_mine": false,
            "is_file": false,
            "text": "hello",
            "reply_to": "",
            "reply_to_text": null,
            "at_ms": 1785979414499u64
        }),
    );
    assert_eq!(status, 200, "{body}");

    let buf = read_sse_until(
        &mut stream,
        |s| s.contains(": ping") && s.contains("\"type\":\"message\""),
        Duration::from_secs(3),
    );
    assert!(buf.contains(": ping"), "{buf}");
    assert!(buf.contains("id: deadbeef"), "{buf}");
    assert!(buf.contains("\"text\":\"hello\""), "{buf}");
    assert!(buf.contains("\"type\":\"message\""), "{buf}");
}

#[test]
fn sse_inject_group_message() {
    let server = spawn_server(&[("VECTOR_SSE_PING_MS", "5000")]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));
    let mut stream = open_sse(server.port, &server.token);
    let channel = "a".repeat(64);
    let (status, body) = post(
        &server,
        "/__test/inject",
        Some(&server.token),
        json!({
            "id": "group-1",
            "chat_id": channel,
            "npub": VALID_NPUB,
            "is_group": true,
            "is_mine": false,
            "text": "hello group",
            "community_id": "c".repeat(64),
        }),
    );
    assert_eq!(status, 200, "{body}");
    let buf = read_sse_until(
        &mut stream,
        |s| s.contains("group-1") && s.contains("hello group"),
        Duration::from_secs(3),
    );
    assert!(buf.contains("\"is_group\":true"), "{buf}");
    assert!(buf.contains(&channel), "{buf}");
}

#[test]
fn communities_stub_create_list_invite() {
    let dir = TempDir::new().unwrap();
    let server = spawn_server(&[("VECTOR_DATA_DIR", dir.path().to_str().unwrap())]);
    post(&server, "/__test/ready", Some(&server.token), json!({}));

    let (status, body) = get(&server, "/communities", Some(&server.token));
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["communities"], json!([]));

    let (status, body) = post(
        &server,
        "/communities",
        Some(&server.token),
        json!({ "name": "Home" }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["created"], true);
    assert_eq!(body["name"], "Home");
    let community_id = body["community_id"].as_str().unwrap().to_string();
    let channel_id = body["channel_id"].as_str().unwrap().to_string();
    assert_eq!(community_id.len(), 64);
    assert_eq!(channel_id.len(), 64);

    let (status, body) = post(
        &server,
        "/communities",
        Some(&server.token),
        json!({ "name": "Other" }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["created"], false);
    assert_eq!(body["community_id"], community_id);
    assert_eq!(body["name"], "Home");

    let (status, body) = post(
        &server,
        "/communities/invite",
        Some(&server.token),
        json!({ "npub": VALID_NPUB }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["community_id"], community_id);
}

#[test]
fn sse_last_writer_wins() {
    let server = spawn_server(&[("VECTOR_SSE_PING_MS", "5000")]);
    let mut first = open_sse(server.port, &server.token);
    let mut second = open_sse(server.port, &server.token);
    let (status, body) = post(
        &server,
        "/__test/inject",
        Some(&server.token),
        json!({
            "id": "only-second",
            "chat_id": VALID_NPUB,
            "npub": VALID_NPUB,
            "text": "later"
        }),
    );
    assert_eq!(status, 200, "{body}");
    let second_buf = read_sse_until(
        &mut second,
        |s| s.contains("only-second"),
        Duration::from_secs(3),
    );
    assert!(second_buf.contains("only-second"), "{second_buf}");

    first
        .stream
        .set_read_timeout(Some(Duration::from_millis(200)))
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut tmp = [0u8; 256];
    loop {
        match first.stream.read(&mut tmp) {
            Ok(0) => {
                let got = String::from_utf8_lossy(&first.buf);
                assert!(
                    !got.contains("only-second"),
                    "replaced client received later event: {got}"
                );
                return;
            }
            Ok(n) => first.buf.extend_from_slice(&tmp[..n]),
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                if Instant::now() > deadline {
                    panic!(
                        "first SSE client still connected after replacement: {}",
                        String::from_utf8_lossy(&first.buf)
                    );
                }
            }
            Err(_) => {
                let got = String::from_utf8_lossy(&first.buf);
                assert!(!got.contains("only-second"), "{got}");
                return;
            }
        }
    }
}

#[test]
fn stdin_eof_shuts_down() {
    let mut server = spawn_server_stdin(&[("VECTOR_SIDECAR_WATCH_STDIN", "1")], Stdio::piped());
    let (status, _) = get(&server, "/live", None);
    assert_eq!(status, 200);
    drop(server.child.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = server.child.try_wait().unwrap() {
            assert!(status.success(), "exit={status:?}");
            return;
        }
        if Instant::now() > deadline {
            panic!("process did not exit after stdin EOF");
        }
        thread::sleep(Duration::from_millis(30));
    }
}

#[test]
fn serve_without_stub_requires_data_dir() {
    let port = free_port();
    let mut cmd = bin();
    cmd.env("VECTOR_SIDECAR_TOKEN", TOKEN)
        .env("VECTOR_BRIDGE_HOST", "127.0.0.1")
        .env("VECTOR_BRIDGE_PORT", port.to_string())
        .env_remove("VECTOR_STUB")
        .env_remove("VECTOR_DATA_DIR")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let out = cmd.output().unwrap();
    assert_eq!(
        out.status.code(),
        Some(1),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(stderr.contains("VECTOR_DATA_DIR"), "stderr={stderr}");
    assert!(
        ureq::get(&format!("http://127.0.0.1:{port}/live"))
            .timeout(Duration::from_millis(200))
            .call()
            .is_err(),
        "must not bind without VECTOR_DATA_DIR"
    );
}

#[test]
fn serve_missing_identity_does_not_mint() {
    let dir = TempDir::new().unwrap();
    let port = free_port();
    let mut cmd = bin();
    cmd.env("VECTOR_SIDECAR_TOKEN", TOKEN)
        .env("VECTOR_BRIDGE_HOST", "127.0.0.1")
        .env("VECTOR_BRIDGE_PORT", port.to_string())
        .env("VECTOR_DATA_DIR", dir.path())
        .env_remove("VECTOR_STUB")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let out = cmd.output().unwrap();
    assert_eq!(
        out.status.code(),
        Some(1),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("identity.nsec") || stderr.contains("will not mint"),
        "stderr={stderr}"
    );
    assert!(!dir.path().join("identity.nsec").exists());
}

#[test]
fn setup_and_check_still_work_without_token() {
    let dir = TempDir::new().unwrap();
    let created = bin()
        .args(["--setup"])
        .env("VECTOR_DATA_DIR", dir.path())
        .env_remove("VECTOR_SIDECAR_TOKEN")
        .output()
        .unwrap();
    assert!(
        created.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&created.stderr)
    );
    let created_json = stdout_json(&created);
    assert_eq!(created_json["status"], "created");
    let npub = created_json["npub"].as_str().unwrap();

    let checked = bin()
        .args(["--check"])
        .env("VECTOR_DATA_DIR", dir.path())
        .env_remove("VECTOR_SIDECAR_TOKEN")
        .output()
        .unwrap();
    assert!(checked.status.success());
    let checked_json = stdout_json(&checked);
    assert_eq!(checked_json["status"], "existing");
    assert_eq!(checked_json["npub"], npub);
}

struct SseClient {
    stream: TcpStream,
    buf: Vec<u8>,
}

fn open_sse(port: u16, token: &str) -> SseClient {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let req = format!(
        "GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Hermes-Sidecar-Token: {token}\r\nAccept: text/event-stream\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).unwrap();
    let mut buf = Vec::new();
    let mut tmp = [0u8; 1024];
    let start = Instant::now();
    loop {
        let n = match stream.read(&mut tmp) {
            Ok(0) => panic!(
                "SSE closed before headers; got {}",
                String::from_utf8_lossy(&buf)
            ),
            Ok(n) => n,
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                if start.elapsed() > Duration::from_secs(3) {
                    panic!(
                        "timeout reading SSE headers: {}",
                        String::from_utf8_lossy(&buf)
                    );
                }
                continue;
            }
            Err(e) => panic!("{e}"),
        };
        buf.extend_from_slice(&tmp[..n]);
        if let Some(pos) = find_double_crlf(&buf) {
            let headers = String::from_utf8_lossy(&buf[..pos]);
            assert!(
                headers.contains("200"),
                "expected 200 SSE, headers={headers}"
            );
            return SseClient {
                stream,
                buf: buf[pos..].to_vec(),
            };
        }
        if start.elapsed() > Duration::from_secs(3) {
            panic!(
                "timeout reading SSE headers: {}",
                String::from_utf8_lossy(&buf)
            );
        }
    }
}

fn find_double_crlf(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 4)
}

fn read_sse_until(
    client: &mut SseClient,
    pred: impl Fn(&str) -> bool,
    timeout: Duration,
) -> String {
    client
        .stream
        .set_read_timeout(Some(Duration::from_millis(200)))
        .ok();
    let mut tmp = [0u8; 1024];
    let start = Instant::now();
    loop {
        let s = String::from_utf8_lossy(&client.buf);
        if pred(&s) {
            return s.into_owned();
        }
        if start.elapsed() > timeout {
            panic!("timeout, got: {}", String::from_utf8_lossy(&client.buf));
        }
        match client.stream.read(&mut tmp) {
            Ok(0) => panic!(
                "SSE closed early, got: {}",
                String::from_utf8_lossy(&client.buf)
            ),
            Ok(n) => client.buf.extend_from_slice(&tmp[..n]),
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut => {}
            Err(e) => panic!("{e}"),
        }
    }
}
