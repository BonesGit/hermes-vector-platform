//! Own kind-0 profile: name, about, picture, banner (Vector `update_profile`).

use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use serde::{Deserialize, Serialize};
use vector_sdk::VectorBot;

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

/// Publish this bot's kind-0 profile via Vector `update_profile`
/// (`name`, `picture`, `banner`, `about`). Images are uploaded from local
/// files when given; otherwise the previously published URL is kept. Empty
/// strings are SDK-merge (they keep prior values) — callers that want no
/// card should skip this.
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
}
