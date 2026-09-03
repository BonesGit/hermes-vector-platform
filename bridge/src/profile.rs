//! Own kind-0 profile: name, about, picture, banner (Vector `update_profile`).
//!
//! Vector's `/` picker only fetches kind-10304 for contacts whose kind-0 has
//! `bot: true`. `update_profile` first-ACKs on the write pool (often an
//! auth-required Vector relay that other clients cannot query for a stranger
//! author), so we also copy the signed kind-0 to public discovery indexers.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use nostr::event::{EventBuilder, Kind, Tag};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use vector_sdk::vector_core::state;
use vector_sdk::vector_core::{sign_builder, ClientRelayExt};
use vector_sdk::{DISCOVERY_RELAYS, VectorBot};

#[derive(Clone, Copy)]
enum ImageSlot {
    Avatar,
    Banner,
}

impl ImageSlot {
    fn cache_file(self) -> &'static str {
        match self {
            ImageSlot::Avatar => "avatar.cache.json",
            ImageSlot::Banner => "banner.cache.json",
        }
    }

    fn label(self) -> &'static str {
        match self {
            ImageSlot::Avatar => "avatar",
            ImageSlot::Banner => "banner",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct ImageCache {
    pub path: String,
    pub len: u64,
    pub mtime_secs: u64,
    pub url: String,
}

pub(crate) fn image_cache_path(data_dir: &Path, slot: &str) -> PathBuf {
    let file = match slot {
        "banner" => ImageSlot::Banner.cache_file(),
        _ => ImageSlot::Avatar.cache_file(),
    };
    data_dir.join(file)
}

pub(crate) fn file_fingerprint(path: &Path) -> Option<(u64, u64)> {
    let meta = path.metadata().ok()?;
    let mtime = meta
        .modified()
        .ok()?
        .duration_since(UNIX_EPOCH)
        .ok()?
        .as_secs();
    Some((meta.len(), mtime))
}

pub(crate) fn cache_hit(cache: &ImageCache, path: &Path) -> bool {
    if cache.url.is_empty() {
        return false;
    }
    let Ok(canon) = path.canonicalize() else {
        return false;
    };
    if cache.path != canon.to_string_lossy() {
        return false;
    }
    match file_fingerprint(path) {
        Some((len, mtime)) => cache.len == len && cache.mtime_secs == mtime,
        None => false,
    }
}

pub(crate) fn read_image_cache(data_dir: &Path, slot: &str) -> Option<ImageCache> {
    let raw = fs::read_to_string(image_cache_path(data_dir, slot)).ok()?;
    serde_json::from_str(&raw).ok()
}

pub(crate) fn write_image_cache(data_dir: &Path, slot: &str, cache: &ImageCache) {
    if let Ok(raw) = serde_json::to_string(cache) {
        let _ = fs::write(image_cache_path(data_dir, slot), raw);
    }
}

fn is_usable_file(path: &Path) -> bool {
    path.is_absolute() && path.is_file()
}

async fn existing_image_url(bot: &VectorBot, slot: ImageSlot) -> String {
    let Some(p) = bot.cached_profile(&bot.npub()).await else {
        return String::new();
    };
    match slot {
        ImageSlot::Avatar => p.avatar,
        ImageSlot::Banner => p.banner,
    }
}

async fn resolve_public_image_url(
    bot: &VectorBot,
    image_path: Option<&Path>,
    data_dir: Option<&Path>,
    slot: ImageSlot,
) -> String {
    let Some(path) = image_path else {
        return existing_image_url(bot, slot).await;
    };
    if !is_usable_file(path) {
        eprintln!(
            "[vector-bridge] {} path is not an existing absolute file: {}",
            slot.label(),
            path.display()
        );
        return existing_image_url(bot, slot).await;
    }
    if let Some(dir) = data_dir {
        if let Some(cache) = read_image_cache(dir, slot.label()) {
            if cache_hit(&cache, path) {
                return cache.url;
            }
        }
    }
    match bot.upload_image(path).await {
        Ok(url) => {
            if let Some(dir) = data_dir {
                if let (Ok(canon), Some((len, mtime))) =
                    (path.canonicalize(), file_fingerprint(path))
                {
                    write_image_cache(
                        dir,
                        slot.label(),
                        &ImageCache {
                            path: canon.to_string_lossy().into_owned(),
                            len,
                            mtime_secs: mtime,
                            url: url.clone(),
                        },
                    );
                }
            }
            url
        }
        Err(err) => {
            eprintln!(
                "[vector-bridge] upload_image ({}) failed: {err}",
                slot.label()
            );
            existing_image_url(bot, slot).await
        }
    }
}

/// True when sidecar-boot should publish a public kind-0.
/// Slash commands need `bot: true` on discovery relays even with no display
/// name. A name/about/image still publishes when slash is off.
pub(crate) fn should_publish_own_profile(
    slash_commands: bool,
    name: &str,
    about: &str,
    avatar_path: Option<&Path>,
    banner_path: Option<&Path>,
) -> bool {
    slash_commands
        || !name.trim().is_empty()
        || !about.trim().is_empty()
        || avatar_path.is_some()
        || banner_path.is_some()
}

pub(crate) fn kind0_content(name: &str, about: &str, picture: &str, banner: &str) -> String {
    let mut m = Map::new();
    m.insert("bot".into(), Value::Bool(true));
    if !name.is_empty() {
        m.insert("name".into(), json!(name));
    }
    if !about.is_empty() {
        m.insert("about".into(), json!(about));
    }
    if !picture.is_empty() {
        m.insert("picture".into(), json!(picture));
    }
    if !banner.is_empty() {
        m.insert("banner".into(), json!(banner));
    }
    Value::Object(m).to_string()
}

fn indexer_relays() -> Vec<String> {
    let mut relays: Vec<String> = DISCOVERY_RELAYS.iter().map(|s| (*s).to_string()).collect();
    relays.extend(state::DISCOVERY_RELAYS.iter().map(|s| (*s).to_string()));
    relays.sort();
    relays.dedup();
    relays
}

/// Kind-0 indexers Vector clients actually query (union of the two SDK lists).
async fn discovery_kind0_relays() -> Vec<String> {
    let mut relays = indexer_relays();
    if let Some(client) = state::nostr_client() {
        relays.extend(client.relays().await.keys().map(|r| r.to_string()));
    }
    relays.sort();
    relays.dedup();
    relays
}

async fn publish_kind0_to_discovery(content: String) -> Result<usize, String> {
    let builder = EventBuilder::new(Kind::Metadata, content)
        .tag(Tag::custom("client", vec!["vector"]));
    let event = sign_builder(builder).await?;
    let client = state::nostr_client().ok_or_else(|| "no client connected".to_string())?;
    let relays = discovery_kind0_relays().await;
    if relays.is_empty() {
        return Err("no discovery relays".into());
    }
    for r in &relays {
        let _ = client.add_managed_relay(r.as_str()).await;
    }
    client.connect().await;
    eprintln!(
        "[vector-bridge] publishing kind-0 (bot: true) to {} relay(s) (background)",
        relays.len()
    );
    let out = client
        .send_event(&event)
        .to(relays)
        .await
        .map_err(|e| e.to_string())?;
    Ok(out.success.len())
}

fn spawn_kind0_discovery(bot: VectorBot) {
    tokio::spawn(async move {
        let (name, about, picture, banner) = match bot.cached_profile(&bot.npub()).await {
            Some(p) => (p.name, p.about, p.avatar, p.banner),
            None => (String::new(), String::new(), String::new(), String::new()),
        };
        match publish_kind0_to_discovery(kind0_content(&name, &about, &picture, &banner)).await {
            Ok(n) => eprintln!("[vector-bridge] kind-0 stored on {n} relay(s)"),
            Err(err) => eprintln!("[vector-bridge] kind-0 discovery publish failed: {err}"),
        }
    });
}

/// Publish this bot's kind-0 profile via Vector `update_profile`
/// (`name`, `picture`, `banner`, `about`). Images are uploaded from local
/// files when given; otherwise the previously published URL is kept. Empty
/// strings are SDK-merge (they keep prior values) — callers that want no
/// card should skip this. After a pool first-ACK, the same card is copied to
/// public discovery indexers so Vector `/` pickers can see `bot: true`.
pub(crate) async fn apply_own_profile(
    bot: &VectorBot,
    name: &str,
    about: &str,
    avatar_path: Option<&Path>,
    banner_path: Option<&Path>,
    data_dir: Option<&Path>,
) -> bool {
    let avatar = resolve_public_image_url(bot, avatar_path, data_dir, ImageSlot::Avatar).await;
    let banner = resolve_public_image_url(bot, banner_path, data_dir, ImageSlot::Banner).await;
    if !bot.update_profile(name, &avatar, &banner, about).await {
        eprintln!("[vector-bridge] update_profile failed");
        return false;
    }
    eprintln!("[vector-bridge] update_profile ok (bot: true)");
    spawn_kind0_discovery(bot.clone());
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn cache_hit_requires_same_path_len_mtime_and_url() {
        let dir = tempfile::tempdir().unwrap();
        let img = dir.path().join("pic.png");
        fs::write(&img, b"png").unwrap();
        let (len, mtime) = file_fingerprint(&img).unwrap();
        let canon = img.canonicalize().unwrap();
        let cache = ImageCache {
            path: canon.to_string_lossy().into_owned(),
            len,
            mtime_secs: mtime,
            url: "https://blossom.example/pic".into(),
        };
        assert!(cache_hit(&cache, &img));

        let mut empty_url = cache.clone();
        empty_url.url.clear();
        assert!(!cache_hit(&empty_url, &img));

        let other = dir.path().join("other.png");
        fs::write(&other, b"png").unwrap();
        assert!(!cache_hit(&cache, &other));
    }

    #[test]
    fn cache_roundtrip_per_slot() {
        let dir = tempfile::tempdir().unwrap();
        let cache = ImageCache {
            path: "/abs/banner.png".into(),
            len: 12,
            mtime_secs: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
            url: "https://example/b".into(),
        };
        write_image_cache(dir.path(), "banner", &cache);
        assert_eq!(
            read_image_cache(dir.path(), "banner").as_ref(),
            Some(&cache)
        );
        assert!(read_image_cache(dir.path(), "avatar").is_none());
    }

    #[test]
    fn kind0_always_sets_bot_true() {
        let v: Value = serde_json::from_str(&kind0_content("", "", "", "")).unwrap();
        assert_eq!(v["bot"], true);
        assert!(v.get("name").is_none());
        let named: Value =
            serde_json::from_str(&kind0_content("Hermes", "about", "https://x/a", "")).unwrap();
        assert_eq!(named["name"], "Hermes");
        assert_eq!(named["about"], "about");
        assert_eq!(named["picture"], "https://x/a");
        assert!(named.get("banner").is_none());
    }

    #[test]
    fn slash_on_publishes_profile_without_a_name() {
        assert!(should_publish_own_profile(true, "", "", None, None));
        assert!(!should_publish_own_profile(false, "", "", None, None));
        assert!(should_publish_own_profile(
            false,
            "Hermes",
            "",
            None,
            None
        ));
    }
}
