//! Hermes approve/deny on Vector's kind-10304 picker.
//!
//! Vector's SDK consumes a matched `/command` before `BotEvent::Message`.
//! Each picker entry is argument-free; we rewrite it to the Hermes text
//! (`/approve-session` → `/approve session`) and SSE-forward that.

use vector_sdk::{IncomingMessage, VectorBot};

use crate::api::AppState;
use crate::events::map_incoming;

/// Keep in sync with adapter.py `_VECTOR_SLASH_COMMANDS`.
#[derive(Clone, Copy)]
struct SlashSpec {
    name: &'static str,
    description: &'static str,
    /// Text Hermes' slash handlers expect.
    hermes: &'static str,
}

const HERMES_SLASH_COMMANDS: &[SlashSpec] = &[
    SlashSpec {
        name: "approve",
        description: "Approve the oldest pending command once",
        hermes: "/approve",
    },
    SlashSpec {
        name: "approve-session",
        description: "Approve oldest and remember for this session",
        hermes: "/approve session",
    },
    SlashSpec {
        name: "approve-always",
        description: "Approve oldest and add to the permanent allowlist",
        hermes: "/approve always",
    },
    SlashSpec {
        name: "approve-all",
        description: "Approve every pending command once",
        hermes: "/approve all",
    },
    SlashSpec {
        name: "approve-all-session",
        description: "Approve every pending command for this session",
        hermes: "/approve all session",
    },
    SlashSpec {
        name: "approve-all-always",
        description: "Approve every pending command permanently",
        hermes: "/approve all always",
    },
    SlashSpec {
        name: "deny",
        description: "Deny the oldest pending command",
        hermes: "/deny",
    },
    SlashSpec {
        name: "deny-all",
        description: "Deny every pending command",
        hermes: "/deny all",
    },
];

pub(crate) fn slash_commands_enabled() -> bool {
    parse_slash_enabled(std::env::var("VECTOR_SLASH_COMMANDS").ok().as_deref())
}

fn parse_slash_enabled(raw: Option<&str>) -> bool {
    let v = raw.unwrap_or("").trim().to_ascii_lowercase();
    if v.is_empty() {
        return true;
    }
    !matches!(v.as_str(), "off" | "0" | "false" | "no" | "disabled")
}

/// Register kind-10304 commands on a live bot. Call this from `BotEvent::Ready`,
/// **not** before `on_event`: the SDK's `prepare_listen` publishes the picker
/// manifest to discovery relays first, which takes 20–40s and delayed Ready
/// past Hermes' connect timeout.
pub(crate) fn register_hermes_commands(bot: &VectorBot, state: &AppState) {
    if !slash_commands_enabled() {
        eprintln!("[vector-bridge] slash commands off (VECTOR_SLASH_COMMANDS)");
        return;
    }
    for spec in HERMES_SLASH_COMMANDS {
        attach(bot, spec, state.clone());
    }
    eprintln!(
        "[vector-bridge] registered {} Vector slash command(s)",
        HERMES_SLASH_COMMANDS.len()
    );
}

/// Publish the picker manifest off the listen path. `vector_sdk` only does
/// this inside `prepare_listen` (before Ready); we do it after inbound is live.
pub(crate) fn spawn_manifest_publish() {
    if !slash_commands_enabled() {
        return;
    }
    tokio::spawn(async {
        if let Err(err) = publish_interface_manifest().await {
            eprintln!("[vector-bridge] slash manifest publish failed: {err}");
        }
    });
}

async fn publish_interface_manifest() -> Result<(), String> {
    use vector_sdk::vector_core::bot_interface::{self, BotManifest, CommandSpec};
    use vector_sdk::vector_core::state::{self, MY_SECRET_KEY};
    use vector_sdk::DISCOVERY_RELAYS;

    let manifest = BotManifest {
        v: 1,
        commands: HERMES_SLASH_COMMANDS
            .iter()
            .map(|spec| CommandSpec {
                name: spec.name.to_string(),
                description: spec.description.to_string(),
                args: Vec::new(),
            })
            .collect(),
    };
    manifest.validate()?;
    let keys = MY_SECRET_KEY
        .to_keys()
        .ok_or_else(|| "no local keys (cannot publish kind-10304)".to_string())?;
    let mut relays: Vec<String> = DISCOVERY_RELAYS.iter().map(|s| (*s).to_string()).collect();
    if let Some(client) = state::nostr_client() {
        relays.extend(client.relays().await.keys().map(|r| r.to_string()));
    }
    for id in vector_sdk::vector_core::db::community::list_community_ids().unwrap_or_default() {
        if let Ok(Some(c)) = vector_sdk::vector_core::db::community::load_community_v2(&id) {
            relays.extend(c.relays.clone());
        } else if let Ok(Some(c)) = vector_sdk::vector_core::db::community::load_community(&id) {
            relays.extend(c.relays.clone());
        }
    }
    relays.sort();
    relays.dedup();
    eprintln!(
        "[vector-bridge] publishing slash manifest to {} relay(s) (background)",
        relays.len()
    );
    match bot_interface::publish_manifest(&manifest, &keys, &relays).await {
        Ok(n) => {
            eprintln!("[vector-bridge] slash manifest stored on {n} relay(s)");
            Ok(())
        }
        Err(e) => Err(e),
    }
}

fn attach(bot: &VectorBot, spec: &SlashSpec, state: AppState) {
    let name = spec.name;
    let hermes = spec.hermes;
    bot.command(spec.name, spec.description).run(move |ctx| {
        let state = state.clone();
        async move {
            forward_slash(&state, &ctx.msg, name, hermes);
        }
    });
}

fn hermes_text(vector_name: &str, hermes: &str, incoming: &IncomingMessage) -> String {
    let raw = incoming.text().trim();
    let first = raw
        .trim_start_matches('/')
        .split_whitespace()
        .next()
        .unwrap_or("");
    // Bare `/approve` and `/deny` keep typed extras (`/approve session`).
    // Hyphenated picker names always rewrite (`/approve-session` → `/approve session`).
    if first.eq_ignore_ascii_case(vector_name) && !vector_name.contains('-') {
        raw.to_string()
    } else {
        hermes.to_string()
    }
}

pub(crate) fn forward_slash(
    state: &AppState,
    incoming: &IncomingMessage,
    vector_name: &str,
    hermes: &str,
) {
    let Some(mut data) = map_incoming(incoming) else {
        return;
    };
    data.is_command = true;
    data.text = hermes_text(vector_name, hermes, incoming);
    eprintln!(
        "[vector-bridge] slash {} → {} id={} group={}",
        incoming.text().trim(),
        data.text,
        data.id,
        data.is_group
    );
    state.events().publish(data.sse_item());
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::time::Duration;
    use vector_sdk::Message;

    fn incoming(text: &str, group: bool) -> IncomingMessage {
        IncomingMessage {
            chat_id: if group {
                "a".repeat(64)
            } else {
                "npub1peer".into()
            },
            is_group: group,
            is_file: false,
            message: Message {
                id: "cmd-1".into(),
                content: text.into(),
                mine: false,
                npub: Some("npub1from".into()),
                at: 1_785_979_414_499,
                ..Default::default()
            },
        }
    }

    #[test]
    fn names_are_valid_slugs() {
        for spec in HERMES_SLASH_COMMANDS {
            assert!(
                spec.name.bytes().all(|b| b.is_ascii_lowercase()
                    || b.is_ascii_digit()
                    || b == b'-'
                    || b == b'_'),
                "bad command name {}",
                spec.name
            );
            assert!(!spec.name.is_empty() && spec.name.len() <= 32);
            assert!(spec.description.len() <= 200);
            assert!(
                spec.hermes.starts_with("/approve") || spec.hermes.starts_with("/deny"),
                "unexpected hermes text {}",
                spec.hermes
            );
        }
        let names: Vec<_> = HERMES_SLASH_COMMANDS.iter().map(|s| s.name).collect();
        assert_eq!(
            names,
            [
                "approve",
                "approve-session",
                "approve-always",
                "approve-all",
                "approve-all-session",
                "approve-all-always",
                "deny",
                "deny-all",
            ]
        );
    }

    #[test]
    fn slash_forward_rewrites_picker_name_to_hermes_text() {
        let state = AppState::new("tok".into(), Duration::from_secs(30));
        let mut rx = state.events().connect();
        forward_slash(
            &state,
            &incoming("/approve-session", false),
            "approve-session",
            "/approve session",
        );
        let item = rx.try_recv().expect("sse item");
        let payload: Value = serde_json::from_str(&item.payload).unwrap();
        assert_eq!(payload["type"], "message");
        assert_eq!(payload["data"]["text"], "/approve session");
        assert_eq!(payload["data"]["is_command"], true);
        assert_eq!(payload["data"]["is_group"], false);
        assert_eq!(payload["data"]["npub"], "npub1from");
    }

    #[test]
    fn slash_forward_keeps_group_chat_id() {
        let state = AppState::new("tok".into(), Duration::from_secs(30));
        let mut rx = state.events().connect();
        forward_slash(
            &state,
            &incoming("/deny-all", true),
            "deny-all",
            "/deny all",
        );
        let item = rx.try_recv().expect("sse item");
        let payload: Value = serde_json::from_str(&item.payload).unwrap();
        assert_eq!(payload["data"]["is_command"], true);
        assert_eq!(payload["data"]["is_group"], true);
        assert_eq!(payload["data"]["chat_id"], "a".repeat(64));
        assert_eq!(payload["data"]["text"], "/deny all");
    }

    #[test]
    fn typed_approve_extras_are_kept() {
        assert_eq!(
            hermes_text("approve", "/approve", &incoming("/approve session", false)),
            "/approve session"
        );
        assert_eq!(
            hermes_text("deny", "/deny", &incoming("/deny all because no", false)),
            "/deny all because no"
        );
        assert_eq!(
            hermes_text(
                "approve-always",
                "/approve always",
                &incoming("/approve-always", false)
            ),
            "/approve always"
        );
    }

    #[test]
    fn enabled_by_default() {
        assert!(parse_slash_enabled(None));
        assert!(parse_slash_enabled(Some("")));
        assert!(parse_slash_enabled(Some("on")));
        assert!(!parse_slash_enabled(Some("off")));
        assert!(!parse_slash_enabled(Some("false")));
        assert!(!parse_slash_enabled(Some("  NO  ")));
    }
}
