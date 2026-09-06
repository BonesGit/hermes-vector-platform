# Changelog

All notable changes to **hermes-vector-platform** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`hermes plugins install` failed on current Hermes** with
  `requires manifest_version 2, but this installer only supports up to 1`.
  Dropped the v2-only `manifest_version` / `api_version` keys. We do not use
  any v2-only fields; absent `manifest_version` is v1 and stays supported.
- **Install-time security scan blocked the plugin as dangerous.** Dummy
  path-traversal and secret-shaped fixtures in tests, plus a rustup
  pipe-to-shell install hint, scored as critical/high. Those strings are
  gone so a community install is no longer an unoverridable block.

### Changed

- **Install no longer prompts for `VECTOR_NPUB`.** That value is an output of
  `hermes gateway setup`, not a credential you paste at `hermes plugins
  install`. It moved from `requires_env` to `optional_env` so the installer
  does not ask for a bot npub that does not exist yet (people were pasting
  their personal Vector npub). `register_platform(required_env=…)` and
  `check_fn` still require it at runtime.
- README install path matches current Hermes plugin UX (one-click Desktop
  link, `hermes plugins install --enable`, opt-in `plugins.enabled`, then
  `hermes gateway setup`). New `after-install.md` is what the CLI renders
  after a git install.

## [0.4.0] — 2026-09-05

SSE replay that cannot drop DMs, `config.yaml` `vector:` as the product
config surface, a real installable package, and GitHub Release sidecars
for Linux and macOS.

### Fixed

- **Inbound DMs could be lost silently during an SSE gap.** The sidecar
  published each inbound event with `try_send` and discarded the result, and it
  marked the message seen in `missed-seen.json` *before* publishing. Any gap —
  a reconnect, the shutdown window, or a full client queue — therefore dropped
  the message with no log, and the advanced cursor stopped the next catch-up
  from ❌-ing it. The peer saw "delivered" and never got a reply. Now:
  - `/events` honors `Last-Event-ID`. The sidecar retains the last 256 items
    and replays everything after the client's resume point, so a reconnect
    recovers the gap instead of losing it. An unknown id replays the whole
    retained window (the adapter's inbound LRU absorbs the overlap); a fresh
    connect with no resume point replays nothing, which keeps gateway restarts
    from re-running old turns.
  - The missed-DM cursor advances only when an item is actually handed to a
    live client, including on replay. Anything the ring cannot cover stays
    unseen and is ❌'d by the next catch-up as designed.
  - Undelivered events are logged with their id and counted. `GET /health` now
    reports `sse_dropped` and `sse_retained`.
  - Replay is bounded so recovering a gap cannot turn into a wall of agent
    turns. All but the newest message per chat is flagged `superseded` and
    filed as session context instead of starting a turn. Tunable in
    `config.yaml` under `vector.replay`: `max_messages` (default 5, `0`
    disables replay entirely) caps items per reconnect, and `max_age_secs`
    (default 600, `0` for no limit) skips stale messages. Skipped messages keep
    their cursor untouched, so they surface as ❌ catch-up marks rather than
    disappearing. `VECTOR_SSE_REPLAY_MAX` /
    `VECTOR_SSE_REPLAY_MAX_AGE_SECS` still override, same as the other
    legacy `VECTOR_*` keys.
- **`pip install` (non-editable) shipped only `adapter.py`.** `package-data`
  was inert under `py-modules = ["adapter"]`, so a wheel or `pip install .`
  had no `plugin.yaml` and no sidecar sources — `hermes gateway setup`
  could not `cargo build`. The install is now the `hermes_vector_platform`
  package (repo-root mapped), which includes `plugin.yaml` and `bridge/src`.
  The entry point is `hermes_vector_platform:register` instead of a bare
  `adapter:register` that collided in site-packages. Git-clone into
  `~/.hermes/plugins/vector-platform` is unchanged.

### Removed

- `VECTOR_ALLOW_ALL_USERS` (and Hermes `allow_all_env`). Open the inbox with
  pairing + `VECTOR_ALLOWED_USERS`, or a listed channel in
  `vector.communities.open_channels`.
- `VECTOR_HOME_CHANNEL_NAME`. Home is a DM; status label is always `Home`.
- Wizard persistence of `VECTOR_NSEC` / `VECTOR_MNEMONIC` / profile /
  pairing / community / data-dir defaults into `.env`. Import still uses a
  temp `0600` file. Leftover secret env vars are warned, then ignored by
  the sidecar.

### Changed

- `plugin.yaml` now declares only `VECTOR_NPUB`, `VECTOR_ALLOWED_USERS`, and
  `VECTOR_HOME_CHANNEL`. Profile, communities, reactions, slash commands,
  replay, pairing, and prebuilt sidecar fetch live in `config.yaml` `vector:`
  (`apply_yaml_config_fn`). Env still wins if a legacy `VECTOR_*` key is set.
- Pairing off is `vector.unauthorized_dm_behavior: ignore` (Hermes shared
  key). Adapter pre-filter still honors leftover `VECTOR_PAIRING=off`.
- Bot avatar/banner are discovered from `sdk/avatar.*` / `sdk/banner.*`
  when no path is configured.
- Setup writes `vector:` (bot name/about, communities, pairing) next to
  `display.platforms.vector`.
- Missed Concord messages stay silent. Catch-up ❌ is **DM-only**; group
  chatter that arrived while the sidecar was down is ignored (no reaction,
  no Hermes turn).

### Added

- **Prebuilt `vector-bridge` from GitHub Releases.** `hermes gateway setup`
  downloads `vector-bridge-<triple>` plus `SHA256SUMS` for Linux and macOS
  (`x86_64` and `aarch64`) matching the plugin version, verifies the hash,
  and installs under `plugin-data/vector-platform/bin/`. Rust is only
  needed when no asset exists, the OS is unsupported, or
  `vector.prebuilt.download` is `false`. Optional `vector.prebuilt.repo`
  / `tag` pick a different Release (`tag` defaults to `v` + plugin
  version). Tag a `v*` release (or `workflow_dispatch`) to build assets
  via `.github/workflows/release-sidecar.yml`.
- Message **edit**. Sidecar `POST /edit` `{to, message_id, body}` calls
  `Channel::edit` (Concord) or `edit_dm` (DMs, so we keep the kind-16
  `edit_id`). Adapter `edit_message` always returns the **original** rumor
  id so Hermes tool-progress / streaming can keep one bubble; the kind-16
  id is marked seen so our own echo is not dispatched. Setup now writes
  `display.platforms.vector.tool_progress: new` (streaming extras stay
  off, including per-platform `streaming: false`). Re-run
  `hermes gateway setup` to refresh an older `off` override.
- Peer **profiles**. `GET /profile?npub=` calls `VectorBot::fetch_profile`
  (kind-0 `name` / `display_name` / about / picture). `get_chat_info` uses
  that for DM titles; inbound `user_name` uses the cached label. Empty
  card still truncates the npub. Concord rooms use the **community** name
  (Vector's list title). Default channel `general` is omitted; extra rooms
  show `Community · channel`.
- DM **block list**. Sidecar `POST /block` `{npub, unblock?}` /
  `GET /block` wrap `VectorBot::block` / `unblock` / `blocked_users`
  (mute, not Concord kick/ban). Adapter drops muted DMs before pairing or
  a turn. Only `VECTOR_HOME_CHANNEL` can type `/block <npub>`,
  `/unblock <npub>`, `/blocked` in that DM (not on the Vector `/` picker;
  other allowlisted users cannot).
- Message **delete**. Sidecar `POST /delete` `{to, message_id}` calls
  `Channel::delete`. Adapter `delete_message` retracts a bot bubble (Hermes
  ephemeral TTL / stream-preview cleanup). Inbound `BotEvent::Delete` is
  SSE `message_delete`: forget local pointers, no Hermes turn.
- Parked **community invites**. Untrusted / `VECTOR_INVITE_POLICY=manual`
  invites stay silent (no home DM). Only `VECTOR_HOME_CHANNEL` can type
  `/invites`, `/join <community_id>`, `/decline <community_id>` in that DM
  (not the Vector `/` picker). Sidecar `GET /invites`,
  `POST /invites/accept`, `POST /invites/decline`. Accepting does not add
  the inviter to `VECTOR_ALLOWED_USERS`.

## [0.3.0] — 2026-09-04

Communities, slash commands, profile, reactions, mnemonic-on-disk, and
community file attachments.

### Added

- Concord **community file attachments** (same Blossom path as DMs). The Vector
  app sends group files with empty caption (no @mention on that event). Default:
  stash metadata; download when a people-gated member **replies to that file**
  and @mentions the bot. Mention-only reply = silent store + session breadcrumb
  (no turn). Mention + extra text = turn with `media_urls`.
  `VECTOR_COMMUNITY_DOWNLOAD_ALL=on` downloads every group file on arrival.
  Inbox: `files/inbox/{channel-id}/{npub}/{date}/`. Outbound `send_image` /
  `send_document` / video / voice target a 64-hex channel via `POST /send-file`
  (`bot.channel().send_file`).
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
  required. Community files ship; reactions and missed-❌ stay DM-only.
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
  (see 0.3.0); v0.1.0 originally needed a sibling Vector checkout.
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
