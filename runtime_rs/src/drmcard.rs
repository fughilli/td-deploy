// Which DRM card drives the display.
//
// On a Pi the KMS device is always /dev/dri/card0. On an x86 box it usually
// isn't: the EFI framebuffer's simpledrm registers card0 at boot, and when the
// real GPU driver (amdgpu/i915) takes over, simpledrm goes away and the GPU comes
// up as card1 — so a hardcoded card0 finds nothing, and the runtime falls back
// to MJPEG-only. Pick the card from sysfs instead: the first one with a
// *connected* connector, else the first with any connector (display off / not
// yet plugged in — scanout starts on hotplug), else the first card.
// $TOXC_DRM_CARD (e.g. /dev/dri/card1) overrides.
use std::path::PathBuf;

pub fn pick() -> PathBuf {
    if let Ok(p) = std::env::var("TOXC_DRM_CARD") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    pick_in(std::path::Path::new("/sys/class/drm"))
        .unwrap_or_else(|| PathBuf::from("/dev/dri/card0"))
}

/// The choice, from a sysfs `class/drm` directory (testable).
pub fn pick_in(sys: &std::path::Path) -> Option<PathBuf> {
    let mut cards: Vec<(u32, String)> = std::fs::read_dir(sys)
        .ok()?
        .filter_map(|e| e.ok()?.file_name().into_string().ok())
        .filter_map(|n| {
            let num = n.strip_prefix("card")?;
            num.parse::<u32>().ok().map(|k| (k, n.clone()))
        })
        .collect();
    cards.sort();
    let entries: Vec<String> = std::fs::read_dir(sys)
        .ok()?
        .filter_map(|e| e.ok()?.file_name().into_string().ok())
        .collect();
    let connectors = |card: &str| -> Vec<String> {
        let pre = format!("{card}-");
        entries
            .iter()
            .filter(|n| n.starts_with(&pre))
            .cloned()
            .collect()
    };
    let connected = |card: &str| {
        connectors(card).iter().any(|c| {
            std::fs::read_to_string(sys.join(c).join("status"))
                .map(|s| s.trim() == "connected")
                .unwrap_or(false)
        })
    };
    let dev = |card: &str| PathBuf::from("/dev/dri").join(card);
    cards
        .iter()
        .find(|(_, c)| connected(c))
        .or_else(|| cards.iter().find(|(_, c)| !connectors(c).is_empty()))
        .or_else(|| cards.first())
        .map(|(_, c)| dev(c))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn sysfs(layout: &[(&str, Option<&str>)]) -> std::path::PathBuf {
        static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let n = N.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let d = std::env::temp_dir().join(format!("drmcard-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        for (name, status) in layout {
            fs::create_dir_all(d.join(name)).unwrap();
            if let Some(s) = status {
                fs::write(d.join(name).join("status"), s).unwrap();
            }
        }
        d
    }

    #[test]
    fn amdgpu_took_over_from_simpledrm() {
        // simpledrm's card0 is gone; the GPU is card1 with HDMI connected.
        let d = sysfs(&[
            ("card1", None),
            ("card1-DP-1", Some("disconnected\n")),
            ("card1-HDMI-A-1", Some("connected\n")),
            ("renderD128", None),
        ]);
        assert_eq!(pick_in(&d), Some(PathBuf::from("/dev/dri/card1")));
    }

    #[test]
    fn prefers_the_card_with_a_connected_display() {
        let d = sysfs(&[
            ("card0", None),
            ("card0-Virtual-1", Some("disconnected\n")),
            ("card2", None),
            ("card2-HDMI-A-2", Some("connected\n")),
        ]);
        assert_eq!(pick_in(&d), Some(PathBuf::from("/dev/dri/card2")));
    }

    #[test]
    fn nothing_connected_falls_back_to_a_kms_card() {
        let d = sysfs(&[
            ("card0", None),
            ("card1", None),
            ("card1-HDMI-A-1", Some("disconnected\n")),
        ]);
        assert_eq!(pick_in(&d), Some(PathBuf::from("/dev/dri/card1")));
    }
}
