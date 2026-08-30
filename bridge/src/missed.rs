//! Ack DMs that arrived while the sidecar was down: react ❌, do not
//! dispatch them to Hermes. First boot seeds the cursor and does not react.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use vector_sdk::{Message, VectorBot};

use crate::api::parse_npub;

const SEEN_FILE: &str = "missed-seen.json";
const HISTORY_LIMIT: usize = 80;
const DEFAULT_EMOJI: &str = "❌";

static FILE_LOCK: Mutex<()> = Mutex::new(());

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SeenMark {
    pub at_ms: u64,
    pub id: String,
}

impl SeenMark {
    fn from_message(msg: &Message) -> Self {
        Self {
            at_ms: msg.at,
            id: msg.id.clone(),
        }
    }

    fn precedes(&self, msg: &Message) -> bool {
        (self.at_ms, self.id.as_str()) < (msg.at, msg.id.as_str())
    }
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct SeenFile {
    #[serde(default)]
    chats: BTreeMap<String, SeenMark>,
}

fn seen_path(data_dir: &Path) -> PathBuf {
    data_dir.join(SEEN_FILE)
}

fn load_seen(data_dir: &Path) -> SeenFile {
    let Ok(raw) = fs::read_to_string(seen_path(data_dir)) else {
        return SeenFile::default();
    };
    serde_json::from_str(&raw).unwrap_or_default()
}

fn save_seen(data_dir: &Path, seen: &SeenFile) {
    if let Ok(raw) = serde_json::to_string_pretty(seen) {
        let _ = fs::write(seen_path(data_dir), raw);
    }
}

fn bump_locked(data_dir: &Path, chat_id: &str, mark: SeenMark) {
    let _g = FILE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut seen = load_seen(data_dir);
    let replace = match seen.chats.get(chat_id) {
        None => true,
        Some(old) => (old.at_ms, old.id.as_str()) < (mark.at_ms, mark.id.as_str()),
    };
    if replace {
        seen.chats.insert(chat_id.to_string(), mark);
        save_seen(data_dir, &seen);
    }
}

/// Record a live inbound DM so a later restart will not ❌ it.
pub fn note_live(data_dir: &Path, chat_id: &str, at_ms: u64, id: &str) {
    if chat_id.is_empty() || id.is_empty() {
        return;
    }
    bump_locked(
        data_dir,
        chat_id,
        SeenMark {
            at_ms,
            id: id.to_string(),
        },
    );
}

pub fn missed_react_enabled() -> bool {
    match std::env::var("VECTOR_MISSED_REACT") {
        Ok(v) => {
            let v = v.trim().to_ascii_lowercase();
            !matches!(v.as_str(), "0" | "false" | "off" | "no")
        }
        Err(_) => true,
    }
}

fn react_emoji() -> String {
    let raw = std::env::var("VECTOR_MISSED_REACT_EMOJI").unwrap_or_default();
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        DEFAULT_EMOJI.to_string()
    } else {
        trimmed.to_string()
    }
}

fn allowlisted_npubs() -> Vec<String> {
    let mut out = Vec::new();
    for key in ["VECTOR_ALLOWED_USERS", "VECTOR_HOME_CHANNEL"] {
        let Ok(val) = std::env::var(key) else {
            continue;
        };
        for part in val.split(',') {
            let part = part.trim();
            if part.is_empty() {
                continue;
            }
            if let Ok(npub) = parse_npub(part) {
                if !out.contains(&npub) {
                    out.push(npub);
                }
            }
        }
    }
    out
}

/// Inbound messages after `last` and strictly before `ready_at_ms`.
pub fn missed_inbound<'a>(
    msgs: impl IntoIterator<Item = &'a Message>,
    last: Option<&SeenMark>,
    ready_at_ms: u64,
) -> Vec<&'a Message> {
    let Some(last) = last else {
        return Vec::new();
    };
    msgs.into_iter()
        .filter(|m| !m.mine && !m.id.is_empty() && m.at < ready_at_ms && last.precedes(m))
        .collect()
}

pub fn newest_inbound(msgs: &[Message]) -> Option<SeenMark> {
    msgs.iter()
        .filter(|m| !m.mine && !m.id.is_empty())
        .max_by_key(|m| (m.at, m.id.as_str()))
        .map(SeenMark::from_message)
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// After `Ready`: ❌ DMs stored while we were down. Never SSE them.
pub async fn ack_missed_while_down(bot: &VectorBot, data_dir: &Path) {
    if !missed_react_enabled() {
        return;
    }
    let ready_at_ms = now_ms();
    let emoji = react_emoji();
    let peers = allowlisted_npubs();
    if peers.is_empty() {
        return;
    }
    let mut local = {
        let _g = FILE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        load_seen(data_dir)
    };
    for npub in peers {
        let history = bot.dm(&npub).history(HISTORY_LIMIT).await;
        match local.chats.get(&npub).cloned() {
            None => {
                let seed = newest_inbound(&history).unwrap_or(SeenMark {
                    at_ms: ready_at_ms,
                    id: String::new(),
                });
                local.chats.insert(npub, seed);
            }
            Some(last) => {
                let missed = missed_inbound(&history, Some(&last), ready_at_ms);
                let mut advanced = last;
                for msg in missed {
                    if let Err(err) = bot.dm(&npub).react(&msg.id, &emoji).await {
                        eprintln!(
                            "[vector-bridge] missed-react failed chat={} id={}: {err}",
                            &npub[..npub.len().min(12)],
                            &msg.id[..msg.id.len().min(12)]
                        );
                        continue;
                    }
                    eprintln!(
                        "[vector-bridge] missed-react {} on id={}",
                        emoji,
                        &msg.id[..msg.id.len().min(16)]
                    );
                    advanced = SeenMark::from_message(msg);
                }
                local.chats.insert(npub, advanced);
            }
        }
    }
    {
        let _g = FILE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let mut live = load_seen(data_dir);
        for (chat, mark) in local.chats {
            let replace = match live.chats.get(&chat) {
                None => true,
                Some(old) => (old.at_ms, old.id.as_str()) < (mark.at_ms, mark.id.as_str()),
            };
            if replace {
                live.chats.insert(chat, mark);
            }
        }
        save_seen(data_dir, &live);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(id: &str, at: u64, mine: bool) -> Message {
        Message {
            id: id.into(),
            at,
            mine,
            ..Default::default()
        }
    }

    #[test]
    fn cold_start_selects_nothing() {
        let msgs = vec![msg("a", 10, false), msg("b", 20, false)];
        assert!(missed_inbound(&msgs, None, 100).is_empty());
    }

    #[test]
    fn selects_only_after_cursor_and_before_ready() {
        let msgs = vec![
            msg("a", 10, false),
            msg("b", 20, false),
            msg("c", 30, true),
            msg("d", 40, false),
            msg("e", 90, false),
        ];
        let last = SeenMark {
            at_ms: 10,
            id: "a".into(),
        };
        let hit: Vec<&str> = missed_inbound(&msgs, Some(&last), 50)
            .into_iter()
            .map(|m| m.id.as_str())
            .collect();
        assert_eq!(hit, vec!["b", "d"]);
    }

    #[test]
    fn newest_inbound_skips_ours() {
        let msgs = vec![msg("a", 10, false), msg("b", 50, true), msg("c", 20, false)];
        let n = newest_inbound(&msgs).unwrap();
        assert_eq!(n.id, "c");
        assert_eq!(n.at_ms, 20);
    }
}
