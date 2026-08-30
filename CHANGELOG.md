# Changelog

All notable changes to **hermes-vector-platform** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Sidecar depends on crates.io [`vector_sdk`](https://crates.io/crates/vector_sdk)
  `=0.9.0` (and `vector-core` `0.8`) instead of a sibling
  `../../Vector/crates/vector-sdk` path. GitHub Actions no longer checkouts
  [VectorPrivacy/Vector](https://github.com/VectorPrivacy/Vector). Exact pin:
  crates.io `0.10.0` was published from git SHA `7bf7d335`, which is not on
  Vector `master`; `0.9.0` was published from `b9aeb8d5`, which is.

## [0.2.0] — 2026-08-29

Encrypted file attachments (Vector Blossom) in both directions.

### Added

- Sidecar `POST /send-file` and `POST /download-attachment`; SSE includes
  `attachments` and no longer drops caption-less file messages
- Inbox: `plugin-data/vector-platform/files/inbox/{npub}/{YYYY-MM-DD}/` plus
  `.meta.json` and `files/index.jsonl`
- File-only (no caption): Vector ack, session breadcrumb (no AI turn)
- File + caption: save then `handle_message` with `media_urls` (vision/STT/docs)
- Outbound `send_image` / `send_document` / `send_video` / `send_voice` /
  `send_animation` via `/send-file` (no outbox copies)

## [0.1.0] — 2026-08-29

First release of the standalone Vector plugin for Hermes Agent. Ready for
daily-driver **live** DMs (allowlisted peer; sidecar up). Catch-up of DMs
missed while the sidecar was down is **not** in v1 — the peer retries.

### Added

- Vector gateway adapter as a Hermes **platform plugin** (`kind: platform`,
  platform name `vector`, toolset `hermes-vector`)
- Rust sidecar (`bridge/vector-bridge`) wrapping `vector-sdk`: localhost HTTP
  + SSE, spawn-time `X-Hermes-Sidecar-Token`, `InvitePolicy::Manual`
- Identity CLI: `--check` is read-only; `--setup` writes
  `<VECTOR_DATA_DIR>/identity.nsec` (`0600`). Runtime never mints.
- Python adapter: spawn/SSE/send, SIGTERM→SIGKILL teardown, stdin parent-death,
  scoped platform lock on the bot npub
- Interactive `hermes gateway setup`: create/import identity, operator npub as
  first allowlisted user, pairing, D12 display YAML (`tool_progress: off`)
- Default-deny allowlist (`VECTOR_ALLOWED_USERS`) + Hermes pairing codes
  (`VECTOR_PAIRING` default on). Cron `deliver=vector` via standalone sender.
- Hardening: runtime record `~/.hermes/runtime/vector-sidecar.json` (`0600`,
  deleted on disconnect), truncated npub logs (`npub1abcd…`), nsec redaction
  pattern, orphan `vector-bridge` reap on connect (Photon pattern)
- README operator checks (npub format, binary path, data dir, port 8096).
  **Not** `hermes doctor`.

### Notes

- Requires Hermes Agent with the platform plugin registry (current `main`)
- Requires Rust ≥ 1.75. Sidecar now depends on crates.io `vector_sdk` `=0.9.0`
  (see Unreleased); v0.1.0 originally needed a sibling Vector checkout.
- User plugins must be enabled: `hermes plugins enable vector-platform`
- Sidecar binds `127.0.0.1:8096` by default; every route except `/live` needs
  the token. Do not set `VECTOR_STUB` in the gateway.
- Cron delivery needs the gateway (and sidecar) running
- v1 is DM text + typing + `bot: true` profile only

### Install

```bash
git clone <this-repo> ~/.hermes/plugins/vector-platform
hermes plugins enable vector-platform
hermes gateway setup    # builds vector-bridge, create/import identity
hermes gateway restart
```
