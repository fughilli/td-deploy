// Zero-copy GPU->HDMI scanout via GBM. The VC4 renders straight into a gbm
// surface's buffer objects, which we page-flip on the DRM CRTC — no readback, no
// CPU copy (unlike sink.rs, the dumb-buffer fallback). Standard KMS+GBM+GLES path.
//
// libgbm is dlopen'd (like libEGL via khronos-egl "dynamic"), so nothing links it
// at build time — the cross toolchain needs no aarch64 libgbm, and the image's
// Mesa provides it at run time.

#[cfg(not(target_os = "linux"))]
pub struct Scanout;
#[cfg(not(target_os = "linux"))]
impl Scanout {
    pub fn open() -> Option<Scanout> {
        None
    }
}

#[cfg(target_os = "linux")]
pub use linux::Scanout;

#[cfg(target_os = "linux")]
mod linux {
    use std::collections::HashMap;
    use std::ffi::c_void;
    use std::fs::{File, OpenOptions};
    use std::os::unix::io::{AsFd, AsRawFd, BorrowedFd};

    use drm::buffer::{Buffer, DrmFourcc, Handle as BufHandle};
    use drm::control::{
        connector, crtc, framebuffer, Device as ControlDevice, Event, Mode, PageFlipFlags,
        RawResourceHandle,
    };
    use drm::Device;
    use glow::HasContext;
    use khronos_egl as egl;
    use libloading::Library;

    struct Card(File);
    impl AsFd for Card {
        fn as_fd(&self) -> BorrowedFd<'_> {
            self.0.as_fd()
        }
    }
    impl Device for Card {}
    impl ControlDevice for Card {}

    // --- gbm C ABI (dlopen'd) -------------------------------------------------
    #[repr(C)]
    struct GbmDevice {
        _p: [u8; 0],
    }
    #[repr(C)]
    struct GbmSurface {
        _p: [u8; 0],
    }
    #[repr(C)]
    struct GbmBo {
        _p: [u8; 0],
    }

    // fourcc 'XR24' (XRGB8888) = 'X'|'R'<<8|'2'<<16|'4'<<24.
    const GBM_FORMAT_XRGB8888: u32 = 0x3432_5258;
    const GBM_BO_USE_SCANOUT: u32 = 1 << 0;
    const GBM_BO_USE_RENDERING: u32 = 1 << 2;

    type FnCreateDevice = unsafe extern "C" fn(i32) -> *mut GbmDevice;
    type FnSurfaceCreate =
        unsafe extern "C" fn(*mut GbmDevice, u32, u32, u32, u32) -> *mut GbmSurface;
    type FnSurfaceDestroy = unsafe extern "C" fn(*mut GbmSurface);
    type FnLockFront = unsafe extern "C" fn(*mut GbmSurface) -> *mut GbmBo;
    type FnReleaseBuffer = unsafe extern "C" fn(*mut GbmSurface, *mut GbmBo);
    type FnBoU32 = unsafe extern "C" fn(*mut GbmBo) -> u32;
    // gbm_bo_get_handle returns a union gbm_bo_handle (u32/ptr, 8 bytes by value).
    type FnBoHandle = unsafe extern "C" fn(*mut GbmBo) -> u64;

    struct Gbm {
        _lib: Library,
        create_device: FnCreateDevice,
        surface_create: FnSurfaceCreate,
        surface_destroy: FnSurfaceDestroy,
        lock_front: FnLockFront,
        release_buffer: FnReleaseBuffer,
        bo_get_stride: FnBoU32,
        bo_get_handle: FnBoHandle,
    }

    impl Gbm {
        unsafe fn load() -> Option<Gbm> {
            let lib = Library::new("libgbm.so.1")
                .or_else(|_| Library::new("libgbm.so"))
                .ok()?;
            let create_device = *lib.get::<FnCreateDevice>(b"gbm_create_device\0").ok()?;
            let surface_create = *lib.get::<FnSurfaceCreate>(b"gbm_surface_create\0").ok()?;
            let surface_destroy =
                *lib.get::<FnSurfaceDestroy>(b"gbm_surface_destroy\0").ok()?;
            let lock_front =
                *lib.get::<FnLockFront>(b"gbm_surface_lock_front_buffer\0").ok()?;
            let release_buffer =
                *lib.get::<FnReleaseBuffer>(b"gbm_surface_release_buffer\0").ok()?;
            let bo_get_stride = *lib.get::<FnBoU32>(b"gbm_bo_get_stride\0").ok()?;
            let bo_get_handle = *lib.get::<FnBoHandle>(b"gbm_bo_get_handle\0").ok()?;
            Some(Gbm {
                _lib: lib,
                create_device,
                surface_create,
                surface_destroy,
                lock_front,
                release_buffer,
                bo_get_stride,
                bo_get_handle,
            })
        }
    }

    // The EGL state bound to the current gbm surface. Recreated on a mode change.
    struct GlState {
        egl: egl::DynamicInstance<egl::EGL1_5>,
        dpy: egl::Display,
        ctx: egl::Context,
        cfg: egl::Config,
        esurf: egl::Surface,
    }

    // A gbm bo described to drm's add_framebuffer.
    struct BoFb {
        size: (u32, u32),
        pitch: u32,
        handle: BufHandle,
    }
    impl Buffer for BoFb {
        fn size(&self) -> (u32, u32) {
            self.size
        }
        fn format(&self) -> DrmFourcc {
            DrmFourcc::Xrgb8888
        }
        fn pitch(&self) -> u32 {
            self.pitch
        }
        fn handle(&self) -> BufHandle {
            self.handle
        }
    }

    pub struct Scanout {
        card: Card,
        gbm: Gbm,
        dev: *mut GbmDevice,
        surf: *mut GbmSurface,
        // None until an HDMI display is present (headless / awaiting hotplug).
        crtc: Option<crtc::Handle>,
        conn: Option<connector::Handle>,
        mode: Option<Mode>,
        pub dw: u32,
        pub dh: u32,
        prev_bo: *mut GbmBo,
        fbs: HashMap<u32, framebuffer::Handle>, // gem handle -> fb (bo's are recycled)
        started: bool,
        probe_ctr: u32, // hotplug re-probe throttle
        gl: Option<GlState>, // EGL bound to `surf` (set by init_gl; swapped on hotplug)
    }

    // Force-probe connectors for a connected display with a usable mode + crtc.
    // Retries for up to wait_ms (0 = single shot).
    fn probe_connector(
        card: &Card,
        res: &drm::control::ResourceHandles,
        wait_ms: u64,
    ) -> Option<(crtc::Handle, connector::Handle, Mode)> {
        let mut waited = 0u64;
        loop {
            let found = res.connectors().iter().find_map(|&h| {
                let info = card.get_connector(h, true).ok()?; // force EDID probe
                if info.state() == connector::State::Connected && !info.modes().is_empty() {
                    let mode = info.modes()[0];
                    let crtc = info
                        .current_encoder()
                        .and_then(|e| card.get_encoder(e).ok())
                        .and_then(|e| e.crtc())
                        .or_else(|| res.crtcs().first().copied())?;
                    Some((crtc, info.handle(), mode))
                } else {
                    None
                }
            });
            if found.is_some() {
                return found;
            }
            if waited >= wait_ms {
                return None;
            }
            std::thread::sleep(std::time::Duration::from_millis(250));
            waited += 250;
        }
    }

    impl Scanout {
        /// Open card0, pick the connected HDMI mode, and create a gbm scanout
        /// surface. None (=> fall back to the dumb-buffer sink / MJPEG) on any
        /// failure (headless, no perms, no libgbm).
        pub fn open() -> Option<Scanout> {
            eprintln!("[scanout] probing /dev/dri/card0 for GBM scanout");
            let file = match OpenOptions::new().read(true).write(true).open("/dev/dri/card0") {
                Ok(f) => f,
                Err(e) => {
                    eprintln!("[scanout] open card0 failed: {e}");
                    return None;
                }
            };
            let card = Card(file);
            if let Err(e) = card.acquire_master_lock() {
                eprintln!("[scanout] acquire_master_lock: {e} (continuing)");
            }

            let res = match card.resource_handles() {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("[scanout] resource_handles failed: {e}");
                    return None;
                }
            };
            // Find a connected display now (short forced probe). If none (HDMI off
            // or unplugged), come up HEADLESS at a default mode and start scanning
            // out when the display appears — flip() re-probes for hotplug.
            let disp = probe_connector(&card, &res, 1500);
            let (dw, dh) = disp
                .map(|(_, _, m)| (m.size().0 as u32, m.size().1 as u32))
                .unwrap_or((1280, 720));
            if disp.is_none() {
                eprintln!(
                    "[scanout] no HDMI display yet — headless GBM {dw}x{dh}, will scan out on hotplug"
                );
            }
            eprintln!("[scanout] card0 ok {dw}x{dh}; loading libgbm");

            let gbm = match unsafe { Gbm::load() } {
                Some(g) => g,
                None => {
                    eprintln!("[scanout] libgbm dlopen failed (not on LD_LIBRARY_PATH?)");
                    return None;
                }
            };
            let fd = card.0.as_raw_fd();
            let dev = unsafe { (gbm.create_device)(fd) };
            if dev.is_null() {
                eprintln!("[scanout] gbm_create_device failed (fd={fd})");
                return None;
            }
            let surf = unsafe {
                (gbm.surface_create)(
                    dev,
                    dw,
                    dh,
                    GBM_FORMAT_XRGB8888,
                    GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING,
                )
            };
            if surf.is_null() {
                eprintln!("[scanout] gbm_surface_create {dw}x{dh} XR24 failed");
                return None;
            }
            println!(
                "[scanout] GBM zero-copy {dw}x{dh} ({})",
                if disp.is_some() {
                    "display attached"
                } else {
                    "headless, awaiting HDMI hotplug"
                }
            );
            Some(Scanout {
                card,
                gbm,
                dev,
                surf,
                crtc: disp.map(|(c, _, _)| c),
                conn: disp.map(|(_, c, _)| c),
                mode: disp.map(|(_, _, m)| m),
                dw,
                dh,
                prev_bo: std::ptr::null_mut(),
                fbs: HashMap::new(),
                started: false,
                probe_ctr: 0,
                gl: None,
            })
        }

        pub fn gbm_device_ptr(&self) -> *mut c_void {
            self.dev as *mut c_void
        }
        pub fn gbm_surface_ptr(&self) -> *mut c_void {
            self.surf as *mut c_void
        }

        /// Build the EGL context on the GBM platform + a window surface on our gbm
        /// surface, so GL renders straight into scanout-capable buffers. Returns
        /// the loaded glow context + the EGL handles (kept alive by the caller).
        pub fn init_gl(&mut self) -> glow::Context {
            const EGL_PLATFORM_GBM_KHR: egl::Enum = 0x31D7;
            const EGL_OPENGL_ES2_BIT: egl::Int = 0x0004;
            let egl =
                unsafe { egl::DynamicInstance::<egl::EGL1_5>::load_required() }.expect("libEGL");
            let dpy = unsafe {
                egl.get_platform_display(
                    EGL_PLATFORM_GBM_KHR,
                    self.gbm_device_ptr(),
                    &[egl::ATTRIB_NONE],
                )
            }
            .expect("gbm platform display");
            egl.initialize(dpy).expect("egl init");
            egl.bind_api(egl::OPENGL_ES_API).expect("bind es");
            // Pick a window-capable ES2 config whose native visual matches XR24, so
            // eglCreateWindowSurface accepts the gbm surface.
            let attrs = [
                egl::SURFACE_TYPE,
                egl::WINDOW_BIT,
                egl::RENDERABLE_TYPE,
                EGL_OPENGL_ES2_BIT,
                egl::RED_SIZE,
                8,
                egl::GREEN_SIZE,
                8,
                egl::BLUE_SIZE,
                8,
                egl::ALPHA_SIZE,
                0,
                egl::NONE,
            ];
            // eglCreateWindowSurface(GBM) needs a config whose EGL_NATIVE_VISUAL_ID
            // equals the gbm surface's fourcc, else EGL_BAD_MATCH. Pick that one.
            let mut configs: Vec<egl::Config> = Vec::with_capacity(64);
            egl.choose_config(dpy, &attrs, &mut configs).expect("choose_config");
            let want = GBM_FORMAT_XRGB8888 as egl::Int;
            let cfg = configs
                .iter()
                .copied()
                .find(|&c| {
                    egl.get_config_attrib(dpy, c, egl::NATIVE_VISUAL_ID).ok() == Some(want)
                })
                .or_else(|| configs.first().copied())
                .expect("no EGL config for the gbm surface");
            eprintln!(
                "[scanout] egl config visual=0x{:x} (want 0x{:x})",
                egl.get_config_attrib(dpy, cfg, egl::NATIVE_VISUAL_ID).unwrap_or(0),
                want
            );
            let ctx = egl
                .create_context(
                    dpy,
                    cfg,
                    None,
                    &[egl::CONTEXT_MAJOR_VERSION, 2, egl::NONE],
                )
                .expect("create ctx");
            let surface = unsafe {
                egl.create_window_surface(
                    dpy,
                    cfg,
                    self.gbm_surface_ptr() as egl::NativeWindowType,
                    None,
                )
            }
            .expect("create window surface");
            egl.make_current(dpy, Some(surface), Some(surface), Some(ctx))
                .expect("make_current");
            let gl = unsafe {
                glow::Context::from_loader_function(|s| match egl.get_proc_address(s) {
                    Some(f) => f as *const c_void,
                    None => std::ptr::null(),
                })
            };
            unsafe {
                let r = gl.get_parameter_string(glow::RENDERER);
                let sw = r.contains("llvmpipe") || r.contains("swrast") || r.contains("softpipe");
                eprintln!(
                    "[scanout] gl renderer={r:?} {}",
                    if sw { "(SOFTWARE)" } else { "(hardware)" }
                );
            }
            self.gl = Some(GlState { egl, dpy, ctx, cfg, esurf: surface });
            gl
        }

        /// eglSwapBuffers the rendered frame onto the gbm surface's back buffer.
        pub fn swap(&self) {
            if let Some(g) = &self.gl {
                let _ = g.egl.swap_buffers(g.dpy, g.esurf);
            }
        }

        /// Re-probe for a display and (re)configure scanout for it — including
        /// recreating the gbm + EGL surface at the display's mode if it differs
        /// from the current one. Makes HDMI hotplug (and a mode change) work
        /// without a restart. Throttled; call once per frame.
        pub fn poll_hotplug(&mut self) {
            self.probe_ctr = self.probe_ctr.wrapping_add(1);
            if let Some(co) = self.conn {
                // Connected: cheap state check (no EDID force) for a disconnect.
                if self.probe_ctr % 120 != 0 {
                    return;
                }
                if let Ok(info) = self.card.get_connector(co, false) {
                    if info.state() != connector::State::Connected {
                        eprintln!("[scanout] HDMI disconnected");
                        self.conn = None;
                        self.crtc = None;
                        self.mode = None;
                        self.started = false;
                    }
                }
                return;
            }
            // Headless: look for a newly-attached display (force EDID probe).
            if self.probe_ctr % 30 != 0 {
                return;
            }
            let res = match self.card.resource_handles() {
                Ok(r) => r,
                Err(_) => return,
            };
            if let Some((cr, co, m)) = probe_connector(&self.card, &res, 0) {
                let (mw, mh) = (m.size().0 as u32, m.size().1 as u32);
                if mw != self.dw || mh != self.dh {
                    eprintln!("[scanout] HDMI {}x{} -> recreating surface", mw, mh);
                    self.recreate_surface(mw, mh);
                } else {
                    eprintln!("[scanout] HDMI connected — scanning out {}x{}", mw, mh);
                }
                self.crtc = Some(cr);
                self.conn = Some(co);
                self.mode = Some(m);
                self.started = false; // force a fresh set_crtc
            }
        }

        // Tear down the current gbm + EGL window surface and build new ones at
        // (dw,dh), rebinding the GL context. The EGL context/config/display stay.
        fn recreate_surface(&mut self, dw: u32, dh: u32) {
            let g = match self.gl.as_mut() {
                Some(g) => g,
                None => return,
            };
            // Release any buffer we still hold on the old surface, then unbind it.
            if !self.prev_bo.is_null() {
                unsafe { (self.gbm.release_buffer)(self.surf, self.prev_bo) };
                self.prev_bo = std::ptr::null_mut();
            }
            let _ = g.egl.make_current(g.dpy, None, None, Some(g.ctx));
            let _ = g.egl.destroy_surface(g.dpy, g.esurf);
            unsafe { (self.gbm.surface_destroy)(self.surf) };
            // Stale framebuffers referenced the old bos; drop the cache.
            for (_h, fb) in self.fbs.drain() {
                let _ = self.card.destroy_framebuffer(fb);
            }
            let surf = unsafe {
                (self.gbm.surface_create)(
                    self.dev,
                    dw,
                    dh,
                    GBM_FORMAT_XRGB8888,
                    GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING,
                )
            };
            if surf.is_null() {
                eprintln!("[scanout] gbm_surface_create {dw}x{dh} failed on hotplug");
                return;
            }
            self.surf = surf;
            let esurf = unsafe {
                g.egl.create_window_surface(g.dpy, g.cfg, surf as egl::NativeWindowType, None)
            };
            match esurf {
                Ok(s) => {
                    g.esurf = s;
                    let _ = g.egl.make_current(g.dpy, Some(s), Some(s), Some(g.ctx));
                    self.dw = dw;
                    self.dh = dh;
                }
                Err(e) => eprintln!("[scanout] create_window_surface on hotplug failed: {e:?}"),
            }
        }

        /// After the caller has eglSwapBuffers'd, present the freshly rendered
        /// buffer: lock it, page-flip it on the CRTC, block for vblank, release the
        /// previous one. This is the whole "present" cost now (no CPU copy).
        pub fn flip(&mut self) {
            let bo = unsafe { (self.gbm.lock_front)(self.surf) };
            if bo.is_null() {
                return;
            }
            let handle = (unsafe { (self.gbm.bo_get_handle)(bo) } & 0xffff_ffff) as u32;
            let pitch = unsafe { (self.gbm.bo_get_stride)(bo) };
            let (dw, dh) = (self.dw, self.dh);
            let card = &self.card;
            let fb = *self.fbs.entry(handle).or_insert_with(|| {
                let bh = BufHandle::from(RawResourceHandle::new(handle).expect("gem handle"));
                let wrap = BoFb {
                    size: (dw, dh),
                    pitch,
                    handle: bh,
                };
                card.add_framebuffer(&wrap, 24, 32).expect("add_framebuffer")
            });
            // Present only when a display is attached; otherwise just recycle the
            // buffer (keeps eglSwapBuffers flowing so we can measure the GPU path).
            if let (Some(crtc), Some(conn), Some(mode)) = (self.crtc, self.conn, self.mode) {
                if !self.started {
                    let _ = self
                        .card
                        .set_crtc(crtc, Some(fb), (0, 0), &[conn], Some(mode));
                    self.started = true;
                } else if self
                    .card
                    .page_flip(crtc, fb, PageFlipFlags::EVENT, None)
                    .is_ok()
                {
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
            }
            if !self.prev_bo.is_null() {
                unsafe { (self.gbm.release_buffer)(self.surf, self.prev_bo) };
            }
            self.prev_bo = bo;
        }
    }
}
