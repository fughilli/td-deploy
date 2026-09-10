// Native DRM/KMS HDMI output. Scans rendered frames straight to the display via
// mapped dumb buffers — no video encoding, no round-trip. Falls back gracefully
// (open() returns None) when there's no DRM device / connected display / the
// permission to modeset, so the runtime keeps working headless (MJPEG only).
//
// DRM is Linux-only (the `drm` crate is a target-specific dep in Cargo.toml); on
// other hosts DrmSink is a no-op stub so the runtime still builds (e.g. a macOS
// `bazel build //...`). Only the aarch64-linux deploy target uses the real one.
//
// Double-buffered + page-flipped on vblank: we draw into the buffer that ISN'T
// on screen, then flip on the next vblank, so a frame is never torn.

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

use drm::buffer::{Buffer, DrmFourcc};   // Buffer trait -> .pitch()
use drm::control::dumbbuffer::DumbBuffer;
use drm::control::{connector, crtc, framebuffer, Device as ControlDevice, Event, Mode, PageFlipFlags};
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
    crtc: crtc::Handle,
    conn: connector::Handle,
    mode: Mode,
    bufs: [DumbBuffer; 2],
    fbs: [framebuffer::Handle; 2],
    back: usize, // buffer index to draw into next (the off-screen one)
    dw: usize,
    dh: usize,
    started: bool,
    // Cached staging frame (XRGB8888 u32/px). This dumb-buffer path is the
    // FALLBACK (headless / web-only / no GBM); the primary HDMI path is
    // zero-copy GBM scanout (see scanout.rs / make_gl). We compose here in cached
    // RAM then bulk-copy row-by-row into the mapped dumb buffer (writes to it are
    // ~uncached, so sequential + minimal is the best we can do on the CPU).
    staging: Vec<u32>,
}

impl DrmSink {
    /// Open the display and set a mode on the connected HDMI connector. Returns
    /// None (headless / no perms) instead of failing the whole runtime.
    pub fn open() -> Option<DrmSink> {
        let file = OpenOptions::new().read(true).write(true).open("/dev/dri/card0").ok()?;
        let card = Card(file);
        // Be the modesetting master (needed for set_crtc / page_flip).
        let _ = card.acquire_master_lock();

        let res = card.resource_handles().ok()?;
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
        let crtc: crtc::Handle = conn
            .current_encoder()
            .and_then(|e| card.get_encoder(e).ok())
            .and_then(|e| e.crtc())
            .or_else(|| res.crtcs().first().copied())?;

        let mk = || -> Option<(DumbBuffer, framebuffer::Handle)> {
            let db = card
                .create_dumb_buffer((dw as u32, dh as u32), DrmFourcc::Xrgb8888, 32)
                .ok()?;
            let fb = card.add_framebuffer(&db, 24, 32).ok()?;
            Some((db, fb))
        };
        let (b0, f0) = mk()?;
        let (b1, f1) = mk()?;

        println!("[sink] DRM/KMS HDMI {dw}x{dh} on connector {:?}", conn.interface());
        Some(DrmSink {
            card,
            crtc,
            conn: conn.handle(),
            mode,
            bufs: [b0, b1],
            fbs: [f0, f1],
            back: 0,
            dw,
            dh,
            started: false,
            staging: vec![0u32; dw * dh],
        })
    }

    /// Present = compose() then flip(); kept for callers that don't want the split.
    pub fn present(&mut self, rgba: &[u8], w: usize, h: usize) {
        self.compose(rgba, w, h);
        self.flip();
    }

    /// Compose an RGBA frame (w x h) into the back dumb buffer (aspect-fit +
    /// centered, nearest upscale, black bars). CPU cost = staging fill + the copy
    /// into the (write-combined) dumb buffer.
    pub fn compose(&mut self, rgba: &[u8], w: usize, h: usize) {
        if w == 0 || h == 0 {
            return;
        }
        let (dw, dh) = (self.dw, self.dh);
        let idx = self.back;
        // 1) Compose into the cached staging buffer (fast RAM). Bars stay black
        //    (set once at init); we only rewrite the centered visible region,
        //    which is the same rect every frame, so nothing stale leaks out.
        let scale = (dw as f32 / w as f32).min(dh as f32 / h as f32);
        let vw = ((w as f32 * scale) as usize).min(dw).max(1);
        let vh = ((h as f32 * scale) as usize).min(dh).max(1);
        let ox = (dw - vw) / 2;
        let oy = (dh - vh) / 2;
        for dy in 0..vh {
            let sy = dy * h / vh;
            let drow = (oy + dy) * dw + ox;
            let srow = sy * w;
            for dx in 0..vw {
                let s = (srow + dx * w / vw) * 4;
                // XRGB8888 (LE) = 0x00RRGGBB
                self.staging[drow + dx] = ((rgba[s] as u32) << 16)
                    | ((rgba[s + 1] as u32) << 8)
                    | (rgba[s + 2] as u32);
            }
        }
        // 2) Bulk-copy staging -> the write-combined dumb buffer one row at a time
        //    (respecting pitch); sequential writes let write-combining coalesce.
        let pitch = self.bufs[idx].pitch() as usize;
        {
            let mut map = match self.card.map_dumb_buffer(&mut self.bufs[idx]) {
                Ok(m) => m,
                Err(_) => return,
            };
            let buf = map.as_mut();
            let row_bytes = dw * 4;
            let src: &[u8] = unsafe {
                std::slice::from_raw_parts(self.staging.as_ptr() as *const u8, dw * dh * 4)
            };
            for y in 0..dh {
                let d0 = y * pitch;
                if d0 + row_bytes <= buf.len() {
                    buf[d0..d0 + row_bytes]
                        .copy_from_slice(&src[y * row_bytes..y * row_bytes + row_bytes]);
                }
            }
        }

    }

    /// Scan out the composed back buffer and block until the vblank page-flip
    /// lands (so the buffer we just left the screen is free to draw next frame).
    pub fn flip(&mut self) {
        let idx = self.back;
        let fb = self.fbs[idx];
        if !self.started {
            let _ = self
                .card
                .set_crtc(self.crtc, Some(fb), (0, 0), &[self.conn], Some(self.mode));
            self.started = true;
        } else if self.card.page_flip(self.crtc, fb, PageFlipFlags::EVENT, None).is_ok() {
            'wait: loop {
                match self.card.receive_events() {
                    Ok(events) => {
                        for ev in events {
                            if let Event::PageFlip(_) = ev {
                                break 'wait;
                            }
                        }
                    }
                    Err(_) => break,
                }
            }
        }
        self.back ^= 1;
    }
}
}
