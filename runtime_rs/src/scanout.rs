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

    // fourcc 'XR24' (XRGB8888), little-endian u32.
    const GBM_FORMAT_XRGB8888: u32 = 0x3458_3252;
    const GBM_BO_USE_SCANOUT: u32 = 1 << 0;
    const GBM_BO_USE_RENDERING: u32 = 1 << 2;

    type FnCreateDevice = unsafe extern "C" fn(i32) -> *mut GbmDevice;
    type FnSurfaceCreate =
        unsafe extern "C" fn(*mut GbmDevice, u32, u32, u32, u32) -> *mut GbmSurface;
    type FnLockFront = unsafe extern "C" fn(*mut GbmSurface) -> *mut GbmBo;
    type FnReleaseBuffer = unsafe extern "C" fn(*mut GbmSurface, *mut GbmBo);
    type FnBoU32 = unsafe extern "C" fn(*mut GbmBo) -> u32;
    // gbm_bo_get_handle returns a union gbm_bo_handle (u32/ptr, 8 bytes by value).
    type FnBoHandle = unsafe extern "C" fn(*mut GbmBo) -> u64;

    struct Gbm {
        _lib: Library,
        create_device: FnCreateDevice,
        surface_create: FnSurfaceCreate,
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
                lock_front,
                release_buffer,
                bo_get_stride,
                bo_get_handle,
            })
        }
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
        crtc: crtc::Handle,
        conn: connector::Handle,
        mode: Mode,
        pub dw: u32,
        pub dh: u32,
        prev_bo: *mut GbmBo,
        fbs: HashMap<u32, framebuffer::Handle>, // gem handle -> fb (bo's are recycled)
        started: bool,
    }

    impl Scanout {
        /// Open card0, pick the connected HDMI mode, and create a gbm scanout
        /// surface. None (=> fall back to the dumb-buffer sink / MJPEG) on any
        /// failure (headless, no perms, no libgbm).
        pub fn open() -> Option<Scanout> {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .open("/dev/dri/card0")
                .ok()?;
            let card = Card(file);
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
            let (dw, dh) = (mode.size().0 as u32, mode.size().1 as u32);
            let crtc: crtc::Handle = conn
                .current_encoder()
                .and_then(|e| card.get_encoder(e).ok())
                .and_then(|e| e.crtc())
                .or_else(|| res.crtcs().first().copied())?;

            let gbm = unsafe { Gbm::load()? };
            let fd = card.0.as_raw_fd();
            let dev = unsafe { (gbm.create_device)(fd) };
            if dev.is_null() {
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
                return None;
            }
            println!(
                "[scanout] GBM zero-copy {dw}x{dh} on connector {:?}",
                conn.interface()
            );
            Some(Scanout {
                card,
                gbm,
                dev,
                surf,
                crtc,
                conn: conn.handle(),
                mode,
                dw,
                dh,
                prev_bo: std::ptr::null_mut(),
                fbs: HashMap::new(),
                started: false,
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
        pub fn init_gl(
            &self,
        ) -> (
            egl::DynamicInstance<egl::EGL1_5>,
            egl::Display,
            egl::Surface,
            glow::Context,
        ) {
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
            let cfg = egl
                .choose_first_config(dpy, &attrs)
                .expect("choose config")
                .expect("no gbm config");
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
            (egl, dpy, surface, gl)
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
            let (dw, dh, crtc, conn, mode) =
                (self.dw, self.dh, self.crtc, self.conn, self.mode);
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
            if !self.prev_bo.is_null() {
                unsafe { (self.gbm.release_buffer)(self.surf, self.prev_bo) };
            }
            self.prev_bo = bo;
        }
    }
}
