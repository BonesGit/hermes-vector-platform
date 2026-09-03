//! Hermes `/approve` and `/deny` on Vector's kind-10304 picker.
//!
//! Vector's SDK consumes a matched `/command` before `BotEvent::Message`, so
//! we register these two commands and SSE-forward the original invocation
//! text. A trailing optional string swallows the rest of the line
//! (`/approve all session` stays one arg).

use vector_sdk::vector_core::bot_interface::{self, ArgSpec, ArgType, BotManifest, CommandSpec};
use vector_sdk::{IncomingMessage, VectorBot};

use crate::api::AppState;
use crate::events::map_incoming;

/// Keep in sync with adapter.py `_VECTOR_SLASH_COMMANDS`.
#[derive(Clone, Copy)]
struct SlashSpec {
    name: &'static str,
    description: &'static str,
    /// Trailing optional string: `(arg_name, arg_description)`.
    tail: Option<(&'static str, &'static str)>,
}

const HERMES_SLASH_COMMANDS: &[SlashSpec] = &[
    SlashSpec {
        name: "approve",
        description: "Approve a pending dangerous command",
        tail: Some((
            "args",
            "once (default), session, always, all, all session, all always",
        )),
    },
    SlashSpec {
        name: "deny",
        description: "Deny a pending dangerous command",
        tail: Some(("reason", "all, and/or a reason relayed to the agent")),
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

fn command_specs() -> Vec<CommandSpec> {
    HERMES_SLASH_COMMANDS
        .iter()
        .map(|spec| CommandSpec {
            name: spec.name.to_string(),
            description: spec.description.to_string(),
            args: spec
                .tail
                .map(|(name, description)| {
                    vec![ArgSpec {
                        name: name.to_string(),
                        arg_type: ArgType::String,
                        description: description.to_string(),
                        required: false,
                        choices: Vec::new(),
                    }]
                })
                .unwrap_or_default(),
        })
        .collect()
}

async fn publish_interface_manifest() -> Result<(), String> {
    use vector_sdk::vector_core::state::{self, MY_SECRET_KEY};
    use vector_sdk::DISCOVERY_RELAYS;

    let manifest = BotManifest {
        v: 1,
        commands: command_specs(),
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
    let builder = bot.command(spec.name, spec.description);
    match spec.tail {
        Some((name, description)) => {
            finish(builder.string(name, description, false), state);
        }
        None => finish(builder, state),
    }
}

fn finish(builder: vector_sdk::CommandBuilder, state: AppState) {
    builder.run(move |ctx| {
        let state = state.clone();
        async move {
            forward_slash(&state, &ctx.msg);
        }
    });
}

pub(crate) fn forward_slash(state: &AppState, incoming: &IncomingMessage) {
    let Some(mut data) = map_incoming(incoming) else {
        return;
    };
    data.is_command = true;
    eprintln!(
        "[vector-bridge] slash {} id={} group={}",
        incoming.text().trim(),
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
            if let Some((name, desc)) = spec.tail {
                assert!(name.bytes().all(|b| b.is_ascii_lowercase()
                    || b.is_ascii_digit()
                    || b == b'-'
                    || b == b'_'));
                assert!(desc.len() <= 200);
            }
        }
        let names: Vec<_> = HERMES_SLASH_COMMANDS.iter().map(|s| s.name).collect();
        assert_eq!(names, ["approve", "deny"]);
        assert!(HERMES_SLASH_COMMANDS.iter().all(|s| s.tail.is_some()));
    }

    #[test]
    fn manifest_with_optional_string_args_validates() {
        let manifest = BotManifest {
            v: 1,
            commands: command_specs(),
        };
        manifest.validate().expect("manifest");
        assert_eq!(manifest.commands.len(), 2);
        assert_eq!(manifest.commands[0].args.len(), 1);
        assert!(!manifest.commands[0].args[0].required);
        assert_eq!(manifest.commands[0].args[0].arg_type, ArgType::String);
    }

    #[test]
    fn slash_forward_keeps_typed_args() {
        let state = AppState::new("tok".into(), Duration::from_secs(30));
        let mut rx = state.events().connect();
        forward_slash(&state, &incoming("/approve session", false));
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
        forward_slash(&state, &incoming("/deny all", true));
        let item = rx.try_recv().expect("sse item");
        let payload: Value = serde_json::from_str(&item.payload).unwrap();
        assert_eq!(payload["data"]["is_command"], true);
        assert_eq!(payload["data"]["is_group"], true);
        assert_eq!(payload["data"]["chat_id"], "a".repeat(64));
        assert_eq!(payload["data"]["text"], "/deny all");
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
