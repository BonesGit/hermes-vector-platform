# Changelog

All notable changes to **hermes-vector-platform** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Identity **create** mints a NIP-06 12-word BIP-39 mnemonic and writes it
  next to the nsec as `sdk/identity.mnemonic` (`0600`). Mnemonic import
  copies the phrase there too. Nsec-only import cannot invent a seed.
  Neither secret is written to `.env`. Identities minted before this, or
  imported from nsec only, have no seed file.
- Concord **communities** (join-first + optional bot-owned home room). Sidecar
  forwards `is_group` SSE, sends/typing via `bot.channel(id)`, and auto-accepts
  community invites only from `VECTOR_TRUSTED_INVITERS` /
  `VECTOR_ALLOWED_USERS` (`VECTOR_INVITE_POLICY=manual` parks them). Adapter
  maps joined Concord channels to `chat_type=group` and mention-gates
  (`@npub` / `@VECTOR_BOT_NAME` / reply-to-bot; `@everyone` ignored).
  Group senders: union of `VECTOR_ALLOWED_USERS` (DM list),
  `VECTOR_GROUP_ALLOWED_USERS` (group-only), or any member on channels in
  `VECTOR_GROUP_ALLOW_ALL`. The gateway learns open channels via
  `extra.group_allowed_chats` (Hermes' chat-id allowlist). Group-only and
  open-channel senders are stamped `role_authorized` so they are not dropped
  after adapter admission. Pairing stays DM-only. The Vector app does not
  show channel ids; on join the sidecar logs the full hex and the adapter DMs
  `VECTOR_HOME_CHANNEL` a copy-pasteable `channel_id:` (once per channel,
  persisted in `sdk/notified-channels.json`).
  `VECTOR_CREATE_COMMUNITY=on`
  creates a private Concord v2 community, persists
  `sdk/home-community.json`, and direct-invites allowlisted npubs. No public
  invite links. Trusted join is enough to listen — there is no
  `VECTOR_GROUP_ALLOWED_CHATS` look-gate.
- Vector **slash commands** (kind 10304): `/approve` and `/deny` only, with
  optional args (`session`, `always`, `all`, `all session`, `all always`,
  deny reason). Matched picker invocations SSE-forward as the original
  `/…` text (`is_command: true`). Groups admit those without an @mention;
  the people-gate still applies. `VECTOR_SLASH_COMMANDS=off` skips the
  public manifest. Concord admin is **not** on this surface.
- Optional public bot profile (Vector `update_profile` / kind-0 `name`,
  `about`, `picture`, `banner`): `VECTOR_BOT_NAME`, `VECTOR_BOT_ABOUT`,
  `VECTOR_BOT_AVATAR`, `VECTOR_BOT_BANNER`. Setup copies images to
  `sdk/avatar.<ext>` / `sdk/banner.<ext>`. Name/about/images stay optional
  (no default name of `Hermes`). When slash commands are on (default), a
  kind-0 with `bot: true` is published so Vector can badge the bot; that
  card is also copied to public discovery indexers. `VECTOR_SLASH_COMMANDS=off`
  and no name/about/image = no profile. `POST /profile` accepts
  `name` / `about` / `avatar_path` / `banner_path`.
- Missed DMs while the sidecar was down are **not** sent to the agent. After
  Ready, allowlisted chats get an ❌ reaction on those messages (`POST /react`,
  `Channel::react`). First boot seeds a cursor and does not react. Disable with
  `VECTOR_MISSED_REACT=off`.
- Hermes `send_message(action=react|unreact)` on Vector DMs: adapter
  `add_reaction` / `remove_reaction` with last-inbound fallback. Sidecar
  `POST /react` accepts `remove: true` (NIP-09 of our reaction rumor) and
  optional `emoji_url` for NIP-30 custom emoji. Peer 👍 on a bot message is
  dispatched as `reaction:added:<emoji>`. Processing-lifecycle 👀 → ✅/❌ is
  **off by default**; set `VECTOR_REACTIONS=on` to enable. Unreact keeps the
  reaction rumor id from `send_reaction` so 👀 is actually retracted before ✅.

### Fixed

- Vector's `/` picker stayed empty even with slash commands on: kind-10304
  reached public discovery relays, but kind-0 `bot: true` only first-ACK'd
  on auth-required Vector write relays. The sidecar now copies kind-0 to
  the same discovery indexers when slash is on (name still optional).
- Sidecar `/health` is `ready` as soon as `VectorBot::build` succeeds, not
  after `BotEvent::Ready`. Slash commands register on Ready and the
  kind-10304 picker manifest publishes in the background. Previously
  `prepare_listen` published the manifest *before* Ready (20–40s to six
  relays), which overran the 25s connect timeout and killed a live sidecar.
- Default `VECTOR_STARTUP_TIMEOUT` is 60s. `register()` floors
  `HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT` to 90s when that env is unset
  (Hermes reads the wrap *before* `connect()`, so flooring inside
  `connect()` missed the first attempt).
- Sequential file-only Vector DMs accumulate in `_pending_inbox` until the
  next text from that peer; previously each file replaced the last.

### Changed

- `DESIGN.md` is local-only (gitignored); operator docs live in the README.
- README: prerequisites before install; pip install notes that Rust is still
  required; community files / reactions / missed-❌ are documented as DM-only.
- Removed ``VECTOR_GROUP_ALLOWED_CHATS``. A trusted invite auto-join is
  enough for the bot to listen; mention/people gates still apply.

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
git clone https://github.com/BonesGit/hermes-vector-platform ~/.hermes/plugins/vector-platform
hermes plugins enable vector-platform
hermes gateway setup    # builds vector-bridge, create/import identity
hermes gateway restart
```
