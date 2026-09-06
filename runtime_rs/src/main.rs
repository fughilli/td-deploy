// toxc native runtime (M2). Headless EGL-surfaceless GL via Rust (khronos-egl +
// glow). Loads a compiled artifact (schedule.json + shaders + assets + native
// param-expr .so) and renders it — no Python in the loop. On the Pi the same code
// targets the V3D with GLES instead of llvmpipe.
//
//   toxc-runtime probe
//   toxc-runtime run <artifact_dir> <out.png> [t_seconds]
use glow::HasContext;
use khronos_egl as egl;
use serde::Deserialize;
use std::collections::HashMap;
use std::net::UdpSocket;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

const PLATFORM_SURFACELESS_MESA: egl::Enum = 0x31DD;
const CTX_OPENGL_PROFILE_MASK: egl::Int = 0x30FD;
const CTX_OPENGL_CORE_PROFILE_BIT: egl::Int = 0x0000_0001;

// ---------------- artifact schema ----------------
#[derive(Deserialize)]
struct Schedule {
    output: String,
    steps: Vec<Step>,
    exprs_lib: Option<String>,
}
#[derive(Deserialize)]
struct Step {
    id: String,
    #[allow(dead_code)]
    op: String,
    kind: String,
    w: i32,
    h: i32,
    #[serde(default)]
    inputs: Vec<String>,
    #[serde(default)]
    source: Option<Source>,
    #[serde(default)]
    vert: Option<String>,
    #[serde(default)]
    frag: Option<String>,
    #[serde(default)]
    sampler_array: Option<String>,
    #[serde(default)]
    uniforms: HashMap<String, Uniform>,
    #[serde(default)]
    time_uniforms: HashMap<String, TimeUniform>,
}
#[derive(Deserialize)]
struct Source {
    #[serde(rename = "type")]
    ty: String,
    #[serde(default)]
    path: Option<String>,
    #[serde(default)]
    w: i32,
    #[serde(default)]
    h: i32,
}
#[derive(Deserialize)]
struct Uniform {
    #[serde(rename = "type")]
    ty: String,
    value: serde_json::Value,
}
#[derive(Deserialize)]
struct TimeUniform {
    #[serde(rename = "fn", default)]
    func: Option<String>,
    #[serde(default)]
    inputs: Vec<String>,
    #[serde(default = "one")]
    mul: f64,
    #[serde(default)]
    interpreted: Option<String>,
}
fn one() -> f64 {
    1.0
}

#[derive(Deserialize)]
struct ServiceSpec {
    #[serde(rename = "type")]
    ty: String,
    name: String,
    #[serde(default)]
    port: u16,
}

// ---------------- I/O services (OSC) + CHOP store ----------------
type Chops = Arc<Mutex<HashMap<String, HashMap<String, f64>>>>;

fn osc_string(d: &[u8], mut i: usize) -> (String, usize) {
    let start = i;
    while i < d.len() && d[i] != 0 {
        i += 1;
    }
    let s = String::from_utf8_lossy(&d[start..i]).into_owned();
    i += 1;
    while i % 4 != 0 {
        i += 1;
    }
    (s, i)
}

fn parse_osc(d: &[u8]) -> Vec<(String, Vec<f64>)> {
    if d.len() >= 8 && &d[..8] == b"#bundle\0" {
        let mut out = vec![];
        let mut i = 16;
        while i + 4 <= d.len() {
            let sz = i32::from_be_bytes([d[i], d[i + 1], d[i + 2], d[i + 3]]) as usize;
            i += 4;
            if i + sz <= d.len() {
                out.extend(parse_osc(&d[i..i + sz]));
            }
            i += sz;
        }
        return out;
    }
    if d.is_empty() || d[0] != b'/' {
        return vec![];
    }
    let (addr, mut i) = osc_string(d, 0);
    if i >= d.len() || d[i] != b',' {
        return vec![(addr, vec![])];
    }
    let (tags, j) = osc_string(d, i);
    i = j;
    let mut args = vec![];
    for t in tags.bytes().skip(1) {
        match t {
            b'f' if i + 4 <= d.len() => {
                args.push(f32::from_be_bytes([d[i], d[i + 1], d[i + 2], d[i + 3]]) as f64);
                i += 4;
            }
            b'i' if i + 4 <= d.len() => {
                args.push(i32::from_be_bytes([d[i], d[i + 1], d[i + 2], d[i + 3]]) as f64);
                i += 4;
            }
            b'd' if i + 8 <= d.len() => {
                let mut b = [0u8; 8];
                b.copy_from_slice(&d[i..i + 8]);
                args.push(f64::from_be_bytes(b));
                i += 8;
            }
            b'T' => args.push(1.0),
            b'F' => args.push(0.0),
            b's' => {
                let (_, k) = osc_string(d, i);
                i = k;
            }
            _ => {}
        }
    }
    vec![(addr, args)]
}

fn start_osc(name: String, port: u16, store: Chops) {
    let sock = UdpSocket::bind(("0.0.0.0", port)).expect("bind osc port");
    println!("[service] oscin {name} udp:{port}");
    thread::spawn(move || {
        let mut buf = [0u8; 65536];
        while let Ok((n, _)) = sock.recv_from(&mut buf) {
            for (addr, nums) in parse_osc(&buf[..n]) {
                let base = addr.trim_start_matches('/').replace('/', "_");
                let mut m = store.lock().unwrap();
                let e = m.entry(name.clone()).or_default();
                if nums.len() == 1 {
                    e.insert(base, nums[0]);
                } else {
                    for (k, v) in nums.iter().enumerate() {
                        e.insert(format!("{base}{}", k + 1), *v);
                    }
                }
            }
        }
    });
}

fn chop_value(store: &Chops, input: &str) -> f64 {
    // input like "chop_<op>_<chan>"
    if let Some(rest) = input.strip_prefix("chop_") {
        let mut it = rest.splitn(2, '_');
        let op = it.next().unwrap_or("");
        let ch = it.next().unwrap_or("");
        return store.lock().unwrap().get(op).and_then(|m| m.get(ch)).copied().unwrap_or(0.0);
    }
    0.0
}

// ---------------- GL setup ----------------
fn make_gl() -> (egl::DynamicInstance<egl::EGL1_5>, egl::Display, glow::Context) {
    let egl = unsafe { egl::DynamicInstance::<egl::EGL1_5>::load_required() }.expect("libEGL");
    let display = unsafe {
        egl.get_platform_display(PLATFORM_SURFACELESS_MESA, egl::DEFAULT_DISPLAY, &[egl::ATTRIB_NONE])
    }
    .expect("get_platform_display");
    egl.initialize(display).expect("initialize");
    egl.bind_api(egl::OPENGL_API).expect("bind_api");
    let cfg = egl
        .choose_first_config(display, &[
            egl::SURFACE_TYPE, egl::PBUFFER_BIT, egl::RENDERABLE_TYPE, egl::OPENGL_BIT,
            egl::RED_SIZE, 8, egl::GREEN_SIZE, 8, egl::BLUE_SIZE, 8, egl::NONE,
        ])
        .expect("choose_config")
        .expect("no config");
    let ctx = egl
        .create_context(display, cfg, None, &[
            egl::CONTEXT_MAJOR_VERSION, 3, egl::CONTEXT_MINOR_VERSION, 3,
            CTX_OPENGL_PROFILE_MASK, CTX_OPENGL_CORE_PROFILE_BIT, egl::NONE,
        ])
        .expect("create_context");
    egl.make_current(display, None, None, Some(ctx)).expect("make_current");
    let gl = unsafe {
        glow::Context::from_loader_function(|s| match egl.get_proc_address(s) {
            Some(f) => f as *const std::ffi::c_void,
            None => std::ptr::null(),
        })
    };
    (egl, display, gl)
}

fn testcard(w: usize, h: usize) -> Vec<u8> {
    let mut buf = vec![0u8; w * h * 4];
    for y in 0..h {
        for x in 0..w {
            let i = (y * w + x) * 4;
            let checker = if ((x / 32) + (y / 32)) % 2 == 1 { 1.0 } else { 0.0 };
            let (mut r, mut g, mut b) = (checker, x as f32 / (w - 1) as f32, y as f32 / (h - 1) as f32);
            if y == h / 2 - 1 || y == h / 2 || x == w / 2 - 1 || x == w / 2 {
                r = 1.0; g = 1.0; b = 1.0;
            }
            buf[i] = (r * 255.0 + 0.5) as u8;
            buf[i + 1] = (g * 255.0 + 0.5) as u8;
            buf[i + 2] = (b * 255.0 + 0.5) as u8;
            buf[i + 3] = 255;
        }
    }
    buf
}

fn flip_vert(data: &[u8], w: usize, h: usize) -> Vec<u8> {
    let mut out = vec![0u8; data.len()];
    for y in 0..h {
        let s = (h - 1 - y) * w * 4;
        out[y * w * 4..(y + 1) * w * 4].copy_from_slice(&data[s..s + w * 4]);
    }
    out
}

fn compile(gl: &glow::Context, ty: u32, src: &str) -> glow::Shader {
    unsafe {
        let s = gl.create_shader(ty).unwrap();
        gl.shader_source(s, src);
        gl.compile_shader(s);
        assert!(gl.get_shader_compile_status(s), "shader error:\n{}\n{}", gl.get_shader_info_log(s), src);
        s
    }
}

fn make_tex(gl: &glow::Context, w: i32, h: i32, data: Option<&[u8]>) -> glow::Texture {
    unsafe {
        let t = gl.create_texture().unwrap();
        gl.bind_texture(glow::TEXTURE_2D, Some(t));
        for (k, v) in [
            (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE), (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE),
            (glow::TEXTURE_MIN_FILTER, glow::LINEAR), (glow::TEXTURE_MAG_FILTER, glow::LINEAR),
        ] {
            gl.tex_parameter_i32(glow::TEXTURE_2D, k, v as i32);
        }
        gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::RGBA8 as i32, w, h, 0, glow::RGBA, glow::UNSIGNED_BYTE, data);
        t
    }
}

fn farr(v: &serde_json::Value) -> Vec<f32> {
    v.as_array().unwrap().iter().map(|x| x.as_f64().unwrap() as f32).collect()
}

unsafe fn call_expr(lib: &libloading::Library, name: &str, args: &[f64]) -> f64 {
    let sym = std::ffi::CString::new(name).unwrap();
    match args.len() {
        0 => { let f: libloading::Symbol<unsafe extern "C" fn() -> f64> = lib.get(sym.as_bytes_with_nul()).unwrap(); f() }
        1 => { let f: libloading::Symbol<unsafe extern "C" fn(f64) -> f64> = lib.get(sym.as_bytes_with_nul()).unwrap(); f(args[0]) }
        2 => { let f: libloading::Symbol<unsafe extern "C" fn(f64, f64) -> f64> = lib.get(sym.as_bytes_with_nul()).unwrap(); f(args[0], args[1]) }
        3 => { let f: libloading::Symbol<unsafe extern "C" fn(f64, f64, f64) -> f64> = lib.get(sym.as_bytes_with_nul()).unwrap(); f(args[0], args[1], args[2]) }
        _ => { let f: libloading::Symbol<unsafe extern "C" fn(f64, f64, f64, f64) -> f64> = lib.get(sym.as_bytes_with_nul()).unwrap(); f(args[0], args[1], args[2], args[3]) }
    }
}

fn run(gl: &glow::Context, dir: &str, out: &str, t: f64, wait_ms: u64) {
    let sched: Schedule =
        serde_json::from_reader(std::fs::File::open(format!("{dir}/schedule.json")).unwrap()).unwrap();
    let exprs = sched
        .exprs_lib
        .as_ref()
        .map(|p| unsafe { libloading::Library::new(format!("{dir}/{p}")).expect("load exprs lib") });

    // I/O services (OSC) -> live CHOP store for op('..')['..'] param exprs
    let store: Chops = Arc::new(Mutex::new(HashMap::new()));
    if let Ok(txt) = std::fs::read_to_string(format!("{dir}/services.json")) {
        if let Ok(specs) = serde_json::from_str::<Vec<ServiceSpec>>(&txt) {
            for s in specs {
                if s.ty == "oscin" {
                    start_osc(s.name, s.port, store.clone());
                }
            }
        }
    }
    if wait_ms > 0 {
        thread::sleep(Duration::from_millis(wait_ms)); // let external OSC arrive (test/one-shot)
    }

    let mut tex: HashMap<String, glow::Texture> = HashMap::new();
    let mut fbo: HashMap<String, glow::Framebuffer> = HashMap::new();
    let mut prog: HashMap<String, glow::Program> = HashMap::new();
    let mut size: HashMap<String, (i32, i32)> = HashMap::new();
    unsafe {
        let vao = gl.create_vertex_array().unwrap();
        gl.bind_vertex_array(Some(vao));
    }

    // setup
    for st in &sched.steps {
        if st.kind == "source" {
            let src = st.source.as_ref().unwrap();
            let (data, w, h) = if src.ty == "image" {
                let im = image::open(format!("{dir}/{}", src.path.as_ref().unwrap())).unwrap().to_rgba8();
                let (w, h) = (im.width() as usize, im.height() as usize);
                (im.into_raw(), w, h)
            } else {
                let (w, h) = (src.w.max(1) as usize, src.h.max(1) as usize);
                (testcard(w, h), w, h)
            };
            let t2 = make_tex(gl, w as i32, h as i32, Some(&flip_vert(&data, w, h)));
            tex.insert(st.id.clone(), t2);
            size.insert(st.id.clone(), (w as i32, h as i32));
        } else if st.kind == "shader" {
            let vs = compile(gl, glow::VERTEX_SHADER, &std::fs::read_to_string(format!("{dir}/{}", st.vert.as_ref().unwrap())).unwrap());
            let fs = compile(gl, glow::FRAGMENT_SHADER, &std::fs::read_to_string(format!("{dir}/{}", st.frag.as_ref().unwrap())).unwrap());
            unsafe {
                let p = gl.create_program().unwrap();
                gl.attach_shader(p, vs);
                gl.attach_shader(p, fs);
                gl.link_program(p);
                assert!(gl.get_program_link_status(p), "link: {}", gl.get_program_info_log(p));
                prog.insert(st.id.clone(), p);
                let ot = make_tex(gl, st.w, st.h, None);
                let f = gl.create_framebuffer().unwrap();
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
                gl.framebuffer_texture_2d(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(ot), 0);
                tex.insert(st.id.clone(), ot);
                fbo.insert(st.id.clone(), f);
                size.insert(st.id.clone(), (st.w, st.h));
            }
        }
    }

    // render frame at time t
    for st in &sched.steps {
        if st.kind == "passthrough" {
            if let Some(src) = st.inputs.first() {
                let (a, b) = (tex[src], size[src]);
                tex.insert(st.id.clone(), a);
                size.insert(st.id.clone(), b);
            }
        } else if st.kind == "shader" {
            unsafe {
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo[&st.id]));
                gl.viewport(0, 0, st.w, st.h);
                let p = prog[&st.id];
                gl.use_program(Some(p));
                for (i, src) in st.inputs.iter().enumerate() {
                    gl.active_texture(glow::TEXTURE0 + i as u32);
                    gl.bind_texture(glow::TEXTURE_2D, Some(tex[src]));
                }
                if let Some(arr) = &st.sampler_array {
                    let loc = gl.get_uniform_location(p, &format!("{arr}[0]")).or_else(|| gl.get_uniform_location(p, arr));
                    if let Some(l) = loc {
                        let units: Vec<i32> = (0..st.inputs.len() as i32).collect();
                        gl.uniform_1_i32_slice(Some(&l), &units);
                    }
                } else {
                    for i in 0..st.inputs.len() {
                        if let Some(l) = gl.get_uniform_location(p, &format!("tex{i}")) {
                            gl.uniform_1_i32(Some(&l), i as i32);
                        }
                    }
                }
                for (name, u) in &st.uniforms {
                    if let Some(l) = gl.get_uniform_location(p, name) {
                        match u.ty.as_str() {
                            "vec2" => { let a = farr(&u.value); gl.uniform_2_f32(Some(&l), a[0], a[1]); }
                            "vec4" => { let a = farr(&u.value); gl.uniform_4_f32(Some(&l), a[0], a[1], a[2], a[3]); }
                            "float" => gl.uniform_1_f32(Some(&l), u.value.as_f64().unwrap() as f32),
                            "int" => gl.uniform_1_i32(Some(&l), u.value.as_i64().unwrap() as i32),
                            _ => {}
                        }
                    }
                }
                for (name, tu) in &st.time_uniforms {
                    if let Some(l) = gl.get_uniform_location(p, name) {
                        let v = if let (Some(func), Some(lib)) = (&tu.func, &exprs) {
                            let args: Vec<f64> = tu.inputs.iter().map(|n| match n.as_str() {
                                "t" => t,
                                "frame" => (t * 60.0).floor(),
                                other => chop_value(&store, other),
                            }).collect();
                            call_expr(lib, func, &args) * tu.mul
                        } else {
                            0.0 // interpreted-expr fallback lands here (TODO embed evaluator)
                        };
                        gl.uniform_1_f32(Some(&l), v as f32);
                    }
                }
                gl.draw_arrays(glow::TRIANGLES, 0, 3);
            }
        }
    }

    // readback output
    let (ow, oh) = size[&sched.output];
    unsafe {
        let f = gl.create_framebuffer().unwrap();
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
        gl.framebuffer_texture_2d(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(tex[&sched.output]), 0);
        let mut raw = vec![0u8; (ow * oh * 4) as usize];
        gl.read_pixels(0, 0, ow, oh, glow::RGBA, glow::UNSIGNED_BYTE, glow::PixelPackData::Slice(&mut raw));
        let top = flip_vert(&raw, ow as usize, oh as usize);
        image::save_buffer(out, &top, ow as u32, oh as u32, image::ExtendedColorType::Rgba8).unwrap();
    }
    println!("[rust] rendered {} @ t={t} -> {out}", sched.output);
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let (_egl, _dpy, gl) = make_gl();
    match args.get(1).map(|s| s.as_str()) {
        Some("run") => {
            let dir = &args[2];
            let out = args.get(3).map(|s| s.as_str()).unwrap_or("out.png");
            let t = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(0.0);
            let wait_ms = args.get(5).and_then(|s| s.parse().ok()).unwrap_or(0);
            run(&gl, dir, out, t, wait_ms);
        }
        _ => unsafe {
            println!("OK GL_RENDERER {}", gl.get_parameter_string(glow::RENDERER));
        },
    }
}
