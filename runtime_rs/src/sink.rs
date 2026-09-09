// Native DRM/KMS HDMI output. Scans rendered frames straight to the display via
// a mapped dumb buffer — no video encoding, no round-trip. Falls back gracefully
// (open() returns None) when there's no DRM device / connected display / the
// permission to modeset, so the runtime keeps working headless (MJPEG only).
//
// DRM is Linux-only (the `drm` crate is a target-specific dep in Cargo.toml); on
// other hosts DrmSink is a no-op stub so the runtime still builds (e.g. a macOS
// `bazel build //...`). Only the aarch64-linux deploy target uses the real one.

#[cfg(not(target_os = "linux"))]
pub struct DrmSink;
#[cfg(not(target_os = "linux"))]
impl DrmSink {
    pub fn open() -> Option<DrmSink> {
        None
    }
    pub fn present(&mut self, _rgba: &[u8], _w: usize, _h: usize) {}
}

#[cfg(target_os = "linux")]
pub use linux::DrmSink;

#[cfg(target_os = "linux")]
mod linux {
use std::fs::{File, OpenOptions};
use std::os::unix::io::{AsFd, BorrowedFd};

use drm::buffer::DrmFourcc;
use drm::control::{connector, crtc, framebuffer, Device as ControlDevice, Mode};
use drm::control::dumbbuffer::DumbBuffer;
use drm::Device;

struct Card(File);
impl AsFd for Card {
    fn as_fd(&self) -> BorrowedFd<'_> {
        self.0.as_fd()
    }
}
impl Device for Card {}
impl ControlDevice for Card {}

pub struct DrmSink {
    card: Card,
    db: DumbBuffer,
    dw: usize,
    dh: usize,
}

impl DrmSink {
    /// Open the display and set a mode on the connected HDMI connector. Returns
    /// None (headless / no perms) instead of failing the whole runtime.
    pub fn open() -> Option<DrmSink> {
        let file = OpenOptions::new().read(true).write(true).open("/dev/dri/card0").ok()?;
        let card = Card(file);
        // Be the modesetting master (needed for set_crtc). Best-effort: on a
        // dedicated appliance nothing else holds it.
        let _ = card.acquire_master_lock();

        let res = card.resource_handles().ok()?;
        // First connected connector with a mode.
        let conn = res.connectors().iter().find_map(|&h| {
            let info = card.get_connector(h, false).ok()?;
            if info.state() == connector::State::Connected && !info.modes().is_empty() {
                Some(info)
            } else {
                None
            }
        })?;
        let mode: Mode = conn.modes()[0];
        let (dw, dh) = (mode.size().0 as usize, mode.size().1 as usize);

        // A CRTC for this connector: prefer its current encoder's crtc, else the
        // first crtc the encoder can drive, else the first crtc.
        let crtc: crtc::Handle = conn
            .current_encoder()
            .and_then(|e| card.get_encoder(e).ok())
            .and_then(|e| e.crtc())
            .or_else(|| res.crtcs().first().copied())?;

        let db = card
            .create_dumb_buffer((dw as u32, dh as u32), DrmFourcc::Xrgb8888, 32)
            .ok()?;
        let fb: framebuffer::Handle = card.add_framebuffer(&db, 24, 32).ok()?;
        card.set_crtc(crtc, Some(fb), (0, 0), &[conn.handle()], Some(mode)).ok()?;

        println!("[sink] DRM/KMS HDMI {dw}x{dh} on connector {:?}", conn.interface());
        Some(DrmSink { card, db, dw, dh })
    }

    /// Blit an RGBA frame (w x h) to the display: aspect-fit + centered, nearest
    /// upscale, black bars. DRM XRGB8888 is little-endian => bytes B,G,R,X.
    pub fn present(&mut self, rgba: &[u8], w: usize, h: usize) {
        if w == 0 || h == 0 {
            return;
        }
        let (dw, dh) = (self.dw, self.dh);
        let mut map = match self.card.map_dumb_buffer(&mut self.db) {
            Ok(m) => m,
            Err(_) => return,
        };
        let buf = map.as_mut();
        for b in buf.iter_mut() {
            *b = 0;
        }
        let scale = (dw as f32 / w as f32).min(dh as f32 / h as f32);
        let vw = ((w as f32 * scale) as usize).min(dw).max(1);
        let vh = ((h as f32 * scale) as usize).min(dh).max(1);
        let ox = (dw - vw) / 2;
        let oy = (dh - vh) / 2;
        for dy in 0..vh {
            let sy = dy * h / vh;
            let drow = (oy + dy) * dw;
            let srow = sy * w;
            for dx in 0..vw {
                let sx = dx * w / vw;
                let s = (srow + sx) * 4;
                let d = (drow + ox + dx) * 4;
                buf[d] = rgba[s + 2]; // B
                buf[d + 1] = rgba[s + 1]; // G
                buf[d + 2] = rgba[s]; // R
                buf[d + 3] = 0; // X
            }
        }
    }
}
}
