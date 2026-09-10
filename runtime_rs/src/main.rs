// toxc native runtime (M2/M4). Headless EGL-surfaceless GL via Rust (khronos-egl +
// glow). Loads a compiled artifact (schedule.json + shaders + assets + native
// param-expr .so + services) and renders it — no Python in the loop. Modes:
//   probe                         print the GL context
//   run   <dir> <out.png> [t] [wait_ms]     one-shot render (conformance)
//   stream <dir> [port] [fps]     live MJPEG (native), with OSC input
// On the Pi the same code targets the V3D with GLES; a DRM/KMS sink replaces the
// network stream for HDMI (added with the device).
use glow::HasContext;
use khronos_egl as egl;
use serde::Deserialize;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream, UdpSocket};
use std::cell::{Cell, RefCell};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

mod expr;
mod sink;
mod scanout;

// Passthrough blit (GLSL ES 1.00) for the GBM scanout path: draw the graph's
// final texture into the display-sized surface, aspect-fit + centered (black bars
// via uScale), on the GPU — no CPU readback.
const BLIT_VERT: &str = "#version 100\n\
attribute vec2 aPos;\n\
varying vec2 vUV;\n\
void main(){ vUV = aPos*0.5+0.5; gl_Position = vec4(aPos,0.0,1.0); }\n";
const BLIT_FRAG: &str = "#version 100\n\
precision mediump float;\n\
varying vec2 vUV;\n\
uniform sampler2D tex;\n\
uniform vec2 uScale;\n\
void main(){\n\
  vec2 uv = (vUV-0.5)/uScale+0.5;\n\
  if(uv.x<0.0||uv.x>1.0||uv.y<0.0||uv.y>1.0) gl_FragColor=vec4(0.0,0.0,0.0,1.0);\n\
  else gl_FragColor=texture2D(tex, uv);\n\
}\n";

const PLATFORM_SURFACELESS_MESA: egl::Enum = 0x31DD;
const CTX_OPENGL_PROFILE_MASK: egl::Int = 0x30FD;
const CTX_OPENGL_CORE_PROFILE_BIT: egl::Int = 0x0000_0001;

// ---------------- artifact schema ----------------
#[derive(Deserialize)]
struct Schedule {
    output: String,
    steps: Vec<Step>,
    exprs_lib: Option<String>,
    #[serde(default)]
    target: String,
    // The CHOP DAG feeding parameter exprs (op('name')[..]), in dependency order.
    // Evaluated per-frame into the chop store; see Renderer::eval_chops.
    #[serde(default)]
    chops: Vec<ChopDef>,
    // The whole CHOP DAG fused + compiled to one native kernel (compiler/
    // chop_lower.py), replacing the per-node fasteval loop. When present, the
    // runtime calls `chops_v` instead of interpreting `chops`. See chops_abi.
    chops_lib: Option<String>,
    #[serde(default)]
    chops_abi: Option<ChopAbi>,
}
// ABI of the compiled `chops_v(const double* in, double* out)` kernel: `in` is
// [t, dt, frame, <sources..>, <states-in..>], `out` is [<outputs..>]. Each entry
// is a (chop name, channel) pair. A Speed output IS its next-frame state.
#[derive(Deserialize, Clone, Default)]
struct ChopAbi {
    #[serde(default)]
    sources: Vec<(String, String)>,
    #[serde(default)]
    states: Vec<(String, String)>,
    #[serde(default)]
    outputs: Vec<(String, String)>,
}
// A control-rate node the importer pulled in because an expr reads it.
// constant: `channels` are per-channel exprs. speed: integrates its input over
// time. null/select/math: passthrough of channel 0 (extend as needed).
#[derive(Deserialize, Clone)]
struct ChopDef {
    name: String,
    #[serde(rename = "type")]
    ty: String,
    #[serde(default)]
    inputs: Vec<String>,
    #[serde(default)]
    channels: Vec<String>,
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
    // The raw TD expr, present when the transpiler couldn't lower it to a native
    // fn. Evaluated by the interpreter (expr.rs) each frame.
    #[serde(default)]
    interpreted: Option<String>,
    #[serde(default = "one")]
    mul: f64,
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
    // midiin: the TD device name (advisory — we open the Pi's rawmidi device).
    #[serde(default)]
    device: Option<String>,
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

// A MIDI In CHOP: read the Pi's raw ALSA MIDI byte stream (/dev/snd/midiC*D*),
// parse channel-voice messages, and feed the SAME chop store as OSC. Mirrors the
// Python MidiInService mapping: CC<n> -> "cc<n>" and note<n> -> "n<n>", both
// normalized 0..1, so `op('<name>')['cc13']` reads back as chop_<name>_cc13.
// Dependency-free (no ALSA/midir crate): rawmidi is a plain MIDI byte stream.
fn midi_set(store: &Chops, name: &str, ch: String, v: f64) {
    let mut m = store.lock().unwrap();
    m.entry(name.to_string()).or_default().insert(ch, v);
}

// The rawmidi device node for the first attached MIDI input (e.g. the Midi
// Fighter Twister). `device` (the TD device name) is advisory; the Twister is
// typically the only rawmidi device on the Pi. Returns None until one appears.
fn find_rawmidi(_device: &Option<String>) -> Option<String> {
    let mut cands: Vec<String> = std::fs::read_dir("/dev/snd")
        .ok()?
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("midiC"))
        .collect();
    cands.sort();
    cands.first().map(|n| format!("/dev/snd/{n}"))
}

// Running-status MIDI byte-stream parser. Fed one byte at a time; yields a
// mapped (channel, normalized-value) pair when a CC / note message completes.
// Handles running status, note-on-velocity-0 == note-off, and interleaved
// system-realtime bytes (clock/active-sensing). Pure + unit-tested below.
#[derive(Default)]
struct MidiParser {
    status: u8,
    data: [u8; 2],
    have: usize,
}

impl MidiParser {
    // Returns (control_or_note, raw_value 0..127, is_note). RAW (not normalized):
    // TD's MIDI In CHOP is un-normalized, and exprs divide by 127 themselves
    // (e.g. `op('midiin1')[0][0]/127 - 0.5`).
    fn push(&mut self, b: u8) -> Option<(u8, f64, bool)> {
        if b >= 0xF8 {
            return None; // system realtime: single byte, ignore (may interleave)
        }
        if b >= 0x80 {
            self.status = if b >= 0xF0 { 0 } else { b }; // system common cancels running status
            self.have = 0;
            return None;
        }
        if self.status == 0 {
            return None; // data byte with no status yet
        }
        self.data[self.have] = b;
        self.have += 1;
        let hi = self.status & 0xF0;
        let need = if hi == 0xC0 || hi == 0xD0 { 1 } else { 2 };
        if self.have < need {
            return None;
        }
        self.have = 0; // keep status for running status
        match hi {
            0xB0 => Some((self.data[0], self.data[1] as f64, false)),
            // note-on with velocity 0 is a note-off
            0x90 => Some((self.data[0], if self.data[1] == 0 { 0.0 } else { self.data[1] as f64 }, true)),
            0x80 => Some((self.data[0], 0.0, true)),
            _ => None,
        }
    }
}

fn start_midi(name: String, device: Option<String>, store: Chops) {
    thread::spawn(move || loop {
        let path = match find_rawmidi(&device) {
            Some(p) => p,
            None => {
                thread::sleep(Duration::from_secs(2));
                continue;
            }
        };
        let mut f = match std::fs::File::open(&path) {
            Ok(f) => {
                println!("[service] midiin {name} dev:{path}");
                f
            }
            Err(e) => {
                eprintln!("[service] midiin {name} open {path} failed: {e}");
                thread::sleep(Duration::from_secs(2));
                continue;
            }
        };
        let mut parser = MidiParser::default();
        let mut byte = [0u8; 1];
        while let Ok(n) = f.read(&mut byte) {
            if n == 0 {
                break; // EOF: device unplugged — fall through to reopen
            }
            if let Some((ctrl, v, is_note)) = parser.push(byte[0]) {
                if is_note {
                    midi_set(&store, &name, format!("n{ctrl}"), v);
                } else {
                    midi_set(&store, &name, format!("cc{ctrl}"), v); // op('midiin1')['ccN']
                    midi_set(&store, &name, format!("{ctrl}"), v); // op('midiin1')[N] (channel = CC #)
                }
            }
        }
        println!("[service] midiin {name} stream ended; reopening");
        thread::sleep(Duration::from_secs(2));
    });
}

#[cfg(test)]
mod midi_tests {
    use super::MidiParser;

    fn drive(bytes: &[u8]) -> Vec<(u8, f64, bool)> {
        let mut p = MidiParser::default();
        bytes.iter().filter_map(|&b| p.push(b)).collect()
    }

    #[test]
    fn cc_is_raw() {
        // CC13 = 127 (raw; exprs normalize themselves via /127). false = not a note.
        assert_eq!(drive(&[0xB0, 13, 127]), vec![(13, 127.0, false)]);
    }

    #[test]
    fn running_status_repeats_cc() {
        // status byte sent once, then two data pairs (running status).
        let out = drive(&[0xB0, 1, 64, 2, 0]);
        assert_eq!(out, vec![(1, 64.0, false), (2, 0.0, false)]);
    }

    #[test]
    fn note_on_zero_velocity_is_note_off() {
        let out = drive(&[0x90, 60, 100, 0x90, 60, 0]);
        assert_eq!(out, vec![(60, 100.0, true), (60, 0.0, true)]);
    }

    #[test]
    fn realtime_clock_interleaves_without_breaking_message() {
        // 0xF8 (clock) between the CC data bytes must be ignored, not corrupt it.
        assert_eq!(drive(&[0xB0, 13, 0xF8, 100]), vec![(13, 100.0, false)]);
    }
}

fn chop_value(store: &Chops, input: &str) -> f64 {
    if let Some(rest) = input.strip_prefix("chop_") {
        let mut it = rest.splitn(2, '_');
        let op = it.next().unwrap_or("");
        let ch = it.next().unwrap_or("");
        return store.lock().unwrap().get(op).and_then(|m| m.get(ch)).copied().unwrap_or(0.0);
    }
    0.0
}

const EGL_OPENGL_ES3_BIT: egl::Int = 0x0000_0040;
const EGL_OPENGL_ES2_BIT: egl::Int = 0x0000_0004;

fn artifact_target(dir: &str) -> String {
    std::fs::read_to_string(format!("{dir}/schedule.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|v| v.get("target").and_then(|x| x.as_str()).map(|s| s.to_string()))
        .unwrap_or_else(|| "desktop_gl".to_string())
}

// ---------------- GL helpers ----------------
// target: "gles2" -> GLES 2.0 (Pi3/VC4), "gles" -> GLES 3.1 (Pi4/5 V3D),
// else desktop GL 3.3 core (host/llvmpipe). Artifact shaders must match.
fn make_gl(target: &str) -> (egl::DynamicInstance<egl::EGL1_5>, egl::Display, glow::Context) {
    let egl = unsafe { egl::DynamicInstance::<egl::EGL1_5>::load_required() }.expect("libEGL");
    let display = unsafe {
        egl.get_platform_display(PLATFORM_SURFACELESS_MESA, egl::DEFAULT_DISPLAY, &[egl::ATTRIB_NONE])
    }
    .expect("get_platform_display");
    egl.initialize(display).expect("initialize");
    let (api, renderable, ctx_attribs): (egl::Enum, egl::Int, Vec<egl::Int>) = match target {
        "gles2" => (egl::OPENGL_ES_API, EGL_OPENGL_ES2_BIT,
                    vec![egl::CONTEXT_MAJOR_VERSION, 2, egl::NONE]),
        "gles" => (egl::OPENGL_ES_API, EGL_OPENGL_ES3_BIT,
                   vec![egl::CONTEXT_MAJOR_VERSION, 3, egl::CONTEXT_MINOR_VERSION, 1, egl::NONE]),
        _ => (egl::OPENGL_API, egl::OPENGL_BIT,
              vec![egl::CONTEXT_MAJOR_VERSION, 3, egl::CONTEXT_MINOR_VERSION, 3,
                   CTX_OPENGL_PROFILE_MASK, CTX_OPENGL_CORE_PROFILE_BIT, egl::NONE]),
    };
    egl.bind_api(api).expect("bind_api");
    let cfg = egl
        .choose_first_config(display, &[
            egl::SURFACE_TYPE, egl::PBUFFER_BIT, egl::RENDERABLE_TYPE, renderable,
            egl::RED_SIZE, 8, egl::GREEN_SIZE, 8, egl::BLUE_SIZE, 8, egl::NONE,
        ])
        .expect("choose_config")
        .expect("no config");
    let ctx = egl.create_context(display, cfg, None, &ctx_attribs).expect("create_context");
    egl.make_current(display, None, None, Some(ctx)).expect("make_current");
    let gl = unsafe {
        glow::Context::from_loader_function(|s| match egl.get_proc_address(s) {
            Some(f) => f as *const std::ffi::c_void,
            None => std::ptr::null(),
        })
    };
    // Report the GL renderer so we can tell VC4 (hardware) from llvmpipe
    // (software) on the target from the journal. Surfaceless EGL uses the
    // GPU's DRM render node unless LIBGL_ALWAYS_SOFTWARE=1 forces llvmpipe.
    unsafe {
        let rend = gl.get_parameter_string(glow::RENDERER);
        let ver = gl.get_parameter_string(glow::VERSION);
        let sw = rend.contains("llvmpipe") || rend.contains("softpipe") || rend.contains("swrast");
        eprintln!("[gl] target={target} renderer={rend:?} version={ver:?} {}",
                  if sw { "(SOFTWARE)" } else { "(hardware)" });
    }
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

fn make_tex(gl: &glow::Context, w: i32, h: i32, data: Option<&[u8]>, gles2: bool) -> glow::Texture {
    // ES2 uses the unsized internalformat GL_RGBA; desktop/ES3 use sized GL_RGBA8.
    let internal = if gles2 { glow::RGBA as i32 } else { glow::RGBA8 as i32 };
    unsafe {
        let t = gl.create_texture().unwrap();
        gl.bind_texture(glow::TEXTURE_2D, Some(t));
        for (k, v) in [
            (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE), (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE),
            (glow::TEXTURE_MIN_FILTER, glow::LINEAR), (glow::TEXTURE_MAG_FILTER, glow::LINEAR),
        ] {
            gl.tex_parameter_i32(glow::TEXTURE_2D, k, v as i32);
        }
        gl.tex_image_2d(glow::TEXTURE_2D, 0, internal, w, h, 0, glow::RGBA, glow::UNSIGNED_BYTE, data);
        t
    }
}

fn farr(v: &serde_json::Value) -> Vec<f32> {
    v.as_array().unwrap().iter().map(|x| x.as_f64().unwrap() as f32).collect()
}

unsafe fn call_expr(lib: &libloading::Library, name: &str, a: &[f64]) -> f64 {
    let s = std::ffi::CString::new(name).unwrap();
    let n = s.as_bytes_with_nul();
    match a.len() {
        0 => { let f: libloading::Symbol<unsafe extern "C" fn() -> f64> = lib.get(n).unwrap(); f() }
        1 => { let f: libloading::Symbol<unsafe extern "C" fn(f64) -> f64> = lib.get(n).unwrap(); f(a[0]) }
        2 => { let f: libloading::Symbol<unsafe extern "C" fn(f64, f64) -> f64> = lib.get(n).unwrap(); f(a[0], a[1]) }
        3 => { let f: libloading::Symbol<unsafe extern "C" fn(f64, f64, f64) -> f64> = lib.get(n).unwrap(); f(a[0], a[1], a[2]) }
        _ => { let f: libloading::Symbol<unsafe extern "C" fn(f64, f64, f64, f64) -> f64> = lib.get(n).unwrap(); f(a[0], a[1], a[2], a[3]) }
    }
}

// ---------------- renderer ----------------
struct Renderer<'a> {
    gl: &'a glow::Context,
    dir: String,
    sched: Schedule,
    exprs: Option<libloading::Library>,
    store: Chops,
    tex: HashMap<String, glow::Texture>,
    fbo: HashMap<String, glow::Framebuffer>,
    prog: HashMap<String, glow::Program>,
    size: HashMap<String, (i32, i32)>,
    gles2: bool,
    quad: Option<glow::Buffer>,
    blit_prog: Option<glow::Program>,   // GBM scanout: final-texture -> display surface
    // Control-rate (CHOP) evaluation: the pre-compiled DAG + integrator state.
    chop_progs: Vec<ChopProg>,
    // The compiled CHOP kernel (chops_v), if the DAG was fully lowered. Preferred
    // over chop_progs; the fasteval loop is the fallback for unlowerable DAGs.
    chops_lib: Option<libloading::Library>,
    chops_abi: Option<ChopAbi>,
    chop_state: RefCell<Vec<f64>>,      // carried Speed accumulators (abi.states)
    chop_state_idx: Vec<usize>,         // each state's position in abi.outputs
    // Pre-compiled interpreted uniforms, keyed by (step index, uniform name).
    uniform_progs: HashMap<(usize, String), expr::Program>,
    speed_state: RefCell<HashMap<(String, usize), f64>>,
    last_t: Cell<f64>,
    prof: Prof,
    profile_gpu: bool,
}

// The fused CHOP kernel's stable C ABI (compiler/chop_lower.py `chops_v`).
type ChopsVFn = unsafe extern "C" fn(*const f64, *mut f64);

// A CHOP with its constant channel exprs pre-compiled (fasteval).
struct ChopProg {
    name: String,
    ty: String,
    inputs: Vec<String>,
    chans: Vec<expr::Program>,
}

fn chop_get(store: &Chops, name: &str, chan: &str) -> f64 {
    store.lock().unwrap().get(name).and_then(|m| m.get(chan)).copied().unwrap_or(0.0)
}
fn chop_set(store: &Chops, name: &str, chan: &str, v: f64) {
    store.lock().unwrap().entry(name.to_string()).or_default().insert(chan.to_string(), v);
}

// ---------------- performance counters ----------------
// Per-compute-node timing, accumulated on the target so we can see where the
// frame budget goes (and later do profile-guided fusion). Labels: "chop:eval",
// "top:<node>", "readback", "encode", "present", "frame". Exposed at /stats
// (JSON) and logged periodically. GPU work on llvmpipe lands mostly at readback
// unless TOXC_PROFILE forces a glFinish per step for accurate per-step attribution.
type Prof = Arc<Mutex<std::collections::BTreeMap<String, (f64, u64)>>>;

fn prof_add(p: &Prof, label: &str, secs: f64) {
    let mut m = p.lock().unwrap();
    let e = m.entry(label.to_string()).or_insert((0.0, 0));
    e.0 += secs * 1000.0; // store milliseconds
    e.1 += 1;
}

fn prof_json(p: &Prof) -> String {
    let m = p.lock().unwrap();
    let mut out = String::from("{\n");
    let n = m.len();
    for (i, (k, (tot, cnt))) in m.iter().enumerate() {
        let avg = if *cnt > 0 { tot / *cnt as f64 } else { 0.0 };
        out.push_str(&format!(
            "  \"{k}\": {{\"avg_ms\": {avg:.3}, \"count\": {cnt}, \"total_ms\": {tot:.1}}}{}\n",
            if i + 1 < n { "," } else { "" }
        ));
    }
    out.push('}');
    out
}

fn prof_summary(p: &Prof) -> String {
    let m = p.lock().unwrap();
    let mut rows: Vec<(String, f64, u64)> = m.iter().map(|(k, (t, c))| (k.clone(), *t, *c)).collect();
    rows.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    let mut s = String::from("[profile]");
    for (k, tot, cnt) in rows.iter().take(8) {
        let avg = if *cnt > 0 { tot / *cnt as f64 } else { 0.0 };
        s.push_str(&format!(" {k}={avg:.2}ms"));
    }
    s
}

// Resolve a shader path, redirecting to the translated ES1.00 set on gles2.
fn resolve_shader(dir: &str, gles2: bool, p: &str) -> String {
    let p = if gles2 { p.replacen("shaders/", "shaders_gles/", 1) } else { p.to_string() };
    format!("{dir}/{p}")
}

impl<'a> Renderer<'a> {
    fn new(gl: &'a glow::Context, dir: &str) -> Self {
        let sched: Schedule =
            serde_json::from_reader(std::fs::File::open(format!("{dir}/schedule.json")).unwrap()).unwrap();
        let exprs = sched.exprs_lib.as_ref().map(|p| unsafe {
            libloading::Library::new(format!("{dir}/{p}")).expect("load exprs lib")
        });
        // The fused CHOP kernel, if the DAG was fully lowered at compile time.
        let chops_lib = sched.chops_lib.as_ref().map(|p| unsafe {
            libloading::Library::new(format!("{dir}/{p}")).expect("load chops lib")
        });
        let chops_abi = sched.chops_abi.clone();
        let (chop_state, chop_state_idx) = match &chops_abi {
            Some(abi) => {
                let idx = abi
                    .states
                    .iter()
                    .map(|s| abi.outputs.iter().position(|o| o == s).unwrap_or(0))
                    .collect();
                (RefCell::new(vec![0.0; abi.states.len()]), idx)
            }
            None => (RefCell::new(Vec::new()), Vec::new()),
        };
        let store: Chops = Arc::new(Mutex::new(HashMap::new()));
        if let Ok(txt) = std::fs::read_to_string(format!("{dir}/services.json")) {
            if let Ok(specs) = serde_json::from_str::<Vec<ServiceSpec>>(&txt) {
                for s in specs {
                    if s.ty == "oscin" {
                        start_osc(s.name, s.port, store.clone());
                    } else if s.ty == "midiin" {
                        start_midi(s.name, s.device, store.clone());
                    }
                }
            }
        }
        let gles2 = sched.target == "gles2";
        // Pre-compile the CHOP channel exprs + interpreted uniforms once.
        let chop_progs: Vec<ChopProg> = sched
            .chops
            .iter()
            .map(|c| ChopProg {
                name: c.name.clone(),
                ty: c.ty.clone(),
                inputs: c.inputs.clone(),
                chans: c.channels.iter().map(|e| expr::Program::compile(e)).collect(),
            })
            .collect();
        let mut uniform_progs = HashMap::new();
        for (si, st) in sched.steps.iter().enumerate() {
            for (name, tu) in &st.time_uniforms {
                if let Some(s) = &tu.interpreted {
                    uniform_progs.insert((si, name.clone()), expr::Program::compile(s));
                }
            }
        }
        let mut r = Renderer {
            gl, dir: dir.to_string(), sched, exprs, store,
            tex: HashMap::new(), fbo: HashMap::new(), prog: HashMap::new(), size: HashMap::new(),
            gles2, quad: None, blit_prog: None,
            chop_progs, chops_lib, chops_abi, chop_state, chop_state_idx, uniform_progs,
            speed_state: RefCell::new(HashMap::new()), last_t: Cell::new(0.0),
            prof: Arc::new(Mutex::new(Default::default())),
            profile_gpu: std::env::var("TOXC_PROFILE").is_ok(),
        };
        r.setup();
        r
    }

    fn prof(&self) -> Prof {
        self.prof.clone()
    }

    // Evaluate the control-rate CHOP DAG (dependency order) into the store, so
    // interpreted uniforms like op('speed1')[0] resolve. Constant = its expr;
    // speed = time-integral of its input; others pass channel 0 through.
    fn eval_chops(&self, t: f64) {
        // Preferred path: the whole DAG fused + compiled to one native kernel.
        if let (Some(lib), Some(abi)) = (&self.chops_lib, &self.chops_abi) {
            let dt = (t - self.last_t.get()).max(0.0).min(1.0);
            self.last_t.set(t);
            let frame = (t * 60.0).floor();
            let mut input = Vec::with_capacity(3 + abi.sources.len() + abi.states.len());
            input.push(t);
            input.push(dt);
            input.push(frame);
            for (n, c) in &abi.sources {
                input.push(chop_get(&self.store, n, c));
            }
            input.extend_from_slice(&self.chop_state.borrow());
            let mut out = vec![0.0f64; abi.outputs.len()];
            unsafe {
                let f: libloading::Symbol<ChopsVFn> = lib.get(b"chops_v").expect("chops_v");
                f(input.as_ptr(), out.as_mut_ptr());
            }
            for (i, (n, c)) in abi.outputs.iter().enumerate() {
                chop_set(&self.store, n, c, out[i]);
            }
            let mut st = self.chop_state.borrow_mut();
            for (j, &idx) in self.chop_state_idx.iter().enumerate() {
                st[j] = out[idx];
            }
            return;
        }
        if self.chop_progs.is_empty() {
            return;
        }
        let dt = (t - self.last_t.get()).max(0.0).min(1.0);
        self.last_t.set(t);
        let frame = (t * 60.0).floor();
        for cp in &self.chop_progs {
            match cp.ty.as_str() {
                "constant" => {
                    for (i, prog) in cp.chans.iter().enumerate() {
                        let store = self.store.clone();
                        let v = prog.eval(t, frame, &|n, ch| chop_get(&store, n, ch));
                        chop_set(&self.store, &cp.name, &i.to_string(), v);
                    }
                }
                "speed" => {
                    if let Some(inp) = cp.inputs.first() {
                        let iv = chop_get(&self.store, inp, "0");
                        let val = {
                            let mut ss = self.speed_state.borrow_mut();
                            let acc = ss.entry((cp.name.clone(), 0)).or_insert(0.0);
                            *acc += iv * dt;
                            *acc
                        };
                        chop_set(&self.store, &cp.name, "0", val);
                    }
                }
                _ => {
                    // null / select / passthrough: copy channel 0 of the input.
                    if let Some(inp) = cp.inputs.first() {
                        let iv = chop_get(&self.store, inp, "0");
                        chop_set(&self.store, &cp.name, "0", iv);
                    }
                }
            }
        }
    }

    fn setup(&mut self) {
        let gl = self.gl;
        unsafe {
            if self.gles2 {
                // ES2 has no gl_VertexID: draw a fullscreen quad from an attribute VBO.
                let verts: [f32; 12] = [-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, -1.0, 1.0, 1.0, -1.0, 1.0, 1.0];
                let bytes = std::slice::from_raw_parts(verts.as_ptr() as *const u8, 48);
                let b = gl.create_buffer().unwrap();
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(b));
                gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
                self.quad = Some(b);
                // Passthrough blit program for the GBM scanout present.
                let vs = compile(gl, glow::VERTEX_SHADER, BLIT_VERT);
                let fs = compile(gl, glow::FRAGMENT_SHADER, BLIT_FRAG);
                let p = gl.create_program().unwrap();
                gl.attach_shader(p, vs);
                gl.attach_shader(p, fs);
                gl.link_program(p);
                assert!(gl.get_program_link_status(p), "blit link: {}", gl.get_program_info_log(p));
                self.blit_prog = Some(p);
            } else {
                let vao = gl.create_vertex_array().unwrap();
                gl.bind_vertex_array(Some(vao));
            }
        }
        let gles2 = self.gles2;
        let dir = self.dir.clone();
        for st in &self.sched.steps {
            if st.kind == "source" {
                let src = st.source.as_ref().unwrap();
                let (data, w, h) = if src.ty == "image" {
                    let im = image::open(format!("{}/{}", dir, src.path.as_ref().unwrap()))
                        .unwrap().to_rgba8();
                    let (w, h) = (im.width() as usize, im.height() as usize);
                    (im.into_raw(), w, h)
                } else {
                    let (w, h) = (src.w.max(1) as usize, src.h.max(1) as usize);
                    (testcard(w, h), w, h)
                };
                let t = make_tex(gl, w as i32, h as i32, Some(&flip_vert(&data, w, h)), gles2);
                self.tex.insert(st.id.clone(), t);
                self.size.insert(st.id.clone(), (w as i32, h as i32));
            } else if st.kind == "shader" {
                let vs = compile(gl, glow::VERTEX_SHADER,
                    &std::fs::read_to_string(resolve_shader(&dir, gles2, st.vert.as_ref().unwrap())).unwrap());
                let fs = compile(gl, glow::FRAGMENT_SHADER,
                    &std::fs::read_to_string(resolve_shader(&dir, gles2, st.frag.as_ref().unwrap())).unwrap());
                unsafe {
                    let p = gl.create_program().unwrap();
                    gl.attach_shader(p, vs);
                    gl.attach_shader(p, fs);
                    gl.link_program(p);
                    assert!(gl.get_program_link_status(p), "link: {}", gl.get_program_info_log(p));
                    let ot = make_tex(gl, st.w, st.h, None, gles2);
                    let f = gl.create_framebuffer().unwrap();
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
                    gl.framebuffer_texture_2d(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(ot), 0);
                    self.prog.insert(st.id.clone(), p);
                    self.tex.insert(st.id.clone(), ot);
                    self.fbo.insert(st.id.clone(), f);
                    self.size.insert(st.id.clone(), (st.w, st.h));
                }
            }
        }
    }

    // Render the graph's TOP passes into their FBOs (no readback). Final output
    // lands in self.tex[output].
    fn cook(&mut self, t: f64) {
        let gl = self.gl;
        let tc = Instant::now();
        self.eval_chops(t); // control-rate pass -> store, before the shader uniforms read it
        prof_add(&self.prof, "chop:eval", tc.elapsed().as_secs_f64());
        for (si, st) in self.sched.steps.iter().enumerate() {
            if st.kind == "passthrough" {
                if let Some(src) = st.inputs.first() {
                    let (a, b) = (self.tex[src], self.size[src]);
                    self.tex.insert(st.id.clone(), a);
                    self.size.insert(st.id.clone(), b);
                }
            } else if st.kind == "shader" {
                let ts = Instant::now();
                unsafe {
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.fbo[&st.id]));
                    gl.viewport(0, 0, st.w, st.h);
                    let p = self.prog[&st.id];
                    gl.use_program(Some(p));
                    for (i, src) in st.inputs.iter().enumerate() {
                        gl.active_texture(glow::TEXTURE0 + i as u32);
                        gl.bind_texture(glow::TEXTURE_2D, Some(self.tex[src]));
                    }
                    if let Some(arr) = &st.sampler_array {
                        let loc = gl.get_uniform_location(p, &format!("{arr}[0]"))
                            .or_else(|| gl.get_uniform_location(p, arr));
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
                            let v = if let (Some(func), Some(lib)) = (&tu.func, &self.exprs) {
                                let args: Vec<f64> = tu.inputs.iter().map(|n| match n.as_str() {
                                    "t" => t,
                                    "frame" => (t * 60.0).floor(),
                                    other => chop_value(&self.store, other),
                                }).collect();
                                call_expr(lib, func, &args) * tu.mul
                            } else if let Some(prog) = self.uniform_progs.get(&(si, name.clone())) {
                                // Interpreted expr (op('name')[i], absTime, math) via fasteval.
                                let store = self.store.clone();
                                let frame = (t * 60.0).floor();
                                prog.eval(t, frame, &|n, ch| chop_get(&store, n, ch)) * tu.mul
                            } else {
                                0.0
                            };
                            gl.uniform_1_f32(Some(&l), v as f32);
                        }
                    }
                    if self.gles2 {
                        gl.bind_buffer(glow::ARRAY_BUFFER, self.quad);
                        if let Some(loc) = gl.get_attrib_location(p, "aPos") {
                            gl.enable_vertex_attrib_array(loc);
                            gl.vertex_attrib_pointer_f32(loc, 2, glow::FLOAT, false, 8, 0);
                        }
                        gl.draw_arrays(glow::TRIANGLES, 0, 6);
                    } else {
                        gl.draw_arrays(glow::TRIANGLES, 0, 3);
                    }
                    // Accurate per-step GPU time needs a finish (opt-in — it stalls
                    // the pipeline); otherwise the deferred work lands at readback.
                    if self.profile_gpu {
                        gl.finish();
                    }
                }
                prof_add(&self.prof, &format!("top:{}", st.id), ts.elapsed().as_secs_f64());
            }
        }
    }

    // GPU->CPU readback of the final output (for MJPEG / the CPU sink / snapshots).
    fn readback(&self) -> (Vec<u8>, i32, i32) {
        let gl = self.gl;
        let (ow, oh) = self.size[&self.sched.output];
        let mut raw = vec![0u8; (ow * oh * 4) as usize];
        let tr = Instant::now();
        unsafe {
            let f = gl.create_framebuffer().unwrap();
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
            gl.framebuffer_texture_2d(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D,
                                      Some(self.tex[&self.sched.output]), 0);
            gl.read_pixels(0, 0, ow, oh, glow::RGBA, glow::UNSIGNED_BYTE, glow::PixelPackData::Slice(&mut raw));
            gl.delete_framebuffer(f);
        }
        prof_add(&self.prof, "readback", tr.elapsed().as_secs_f64());
        (flip_vert(&raw, ow as usize, oh as usize), ow, oh)
    }

    fn render(&mut self, t: f64) -> (Vec<u8>, i32, i32) {
        self.cook(t);
        self.readback()
    }

    // GBM scanout present: draw the final texture into the display surface
    // (aspect-fit, centered, black bars) on the GPU. No readback. The caller then
    // eglSwapBuffers + page-flips.
    fn present_scanout(&self, dw: i32, dh: i32) {
        let gl = self.gl;
        let (ow, oh) = self.size[&self.sched.output];
        let p = match self.blit_prog {
            Some(p) => p,
            None => return,
        };
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, None); // default FB = the gbm surface
            gl.viewport(0, 0, dw, dh);
            gl.clear_color(0.0, 0.0, 0.0, 1.0);
            gl.clear(glow::COLOR_BUFFER_BIT);
            gl.use_program(Some(p));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(self.tex[&self.sched.output]));
            if let Some(l) = gl.get_uniform_location(p, "tex") {
                gl.uniform_1_i32(Some(&l), 0);
            }
            let (ia, da) = (ow as f32 / oh as f32, dw as f32 / dh as f32);
            let (sx, sy) = if ia < da { (ia / da, 1.0) } else { (1.0, da / ia) };
            if let Some(l) = gl.get_uniform_location(p, "uScale") {
                gl.uniform_2_f32(Some(&l), sx, sy);
            }
            gl.bind_buffer(glow::ARRAY_BUFFER, self.quad);
            if let Some(loc) = gl.get_attrib_location(p, "aPos") {
                gl.enable_vertex_attrib_array(loc);
                gl.vertex_attrib_pointer_f32(loc, 2, glow::FLOAT, false, 8, 0);
            }
            gl.draw_arrays(glow::TRIANGLES, 0, 6);
        }
    }
}

fn run(gl: &glow::Context, dir: &str, out: &str, t: f64, wait_ms: u64) {
    let mut r = Renderer::new(gl, dir);
    if wait_ms > 0 {
        thread::sleep(Duration::from_millis(wait_ms));
    }
    let (buf, w, h) = r.render(t);
    image::save_buffer(out, &buf, w as u32, h as u32, image::ExtendedColorType::Rgba8).unwrap();
    println!("[rust] rendered {} @ t={t} -> {out}", r.sched.output);
}

const PAGE: &[u8] = b"<!doctype html><html><body style='margin:0;background:#111;display:flex;\
align-items:center;justify-content:center;height:100vh'>\
<img src='/stream' style='max-width:100vw;max-height:100vh;image-rendering:pixelated'></body></html>";

// Count of clients currently pulling frames. The render loop only renders +
// encodes while this is > 0, so an idle box (no viewer) spends no CPU/GPU.
fn serve_http(port: u16, latest: Arc<Mutex<Vec<u8>>>, clients: Arc<AtomicUsize>, prof: Prof) {
    let l = TcpListener::bind(("0.0.0.0", port)).expect("bind http");
    println!("[stream] native MJPEG on http://0.0.0.0:{port}/  (perf counters at /stats)");
    for c in l.incoming().flatten() {
        let latest = latest.clone();
        let clients = clients.clone();
        let prof = prof.clone();
        thread::spawn(move || handle_conn(c, latest, clients, prof));
    }
}

// Decrement the client count when a viewer's handler returns, on every path.
struct ClientGuard(Arc<AtomicUsize>);
impl Drop for ClientGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}

fn handle_conn(mut s: TcpStream, latest: Arc<Mutex<Vec<u8>>>, clients: Arc<AtomicUsize>, prof: Prof) {
    let mut buf = [0u8; 2048];
    let n = s.read(&mut buf).unwrap_or(0);
    let req = String::from_utf8_lossy(&buf[..n]);
    let path = req.split_whitespace().nth(1).unwrap_or("/");
    if path.starts_with("/stats") {
        // Per-node performance counters (JSON) — where the frame budget goes.
        let body = prof_json(&prof);
        let hdr = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nAccess-Control-Allow-Origin: *\r\n\r\n", body.len());
        let _ = s.write_all(hdr.as_bytes());
        let _ = s.write_all(body.as_bytes());
    } else if path.starts_with("/stream") {
        clients.fetch_add(1, Ordering::Relaxed);
        let _guard = ClientGuard(clients.clone()); // wakes the render loop; drop pauses it
        if s.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\nCache-Control: no-cache\r\n\r\n").is_err() {
            return;
        }
        loop {
            let jpg = latest.lock().unwrap().clone();
            if !jpg.is_empty() {
                let hdr = format!("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n", jpg.len());
                if s.write_all(hdr.as_bytes()).is_err() || s.write_all(&jpg).is_err() || s.write_all(b"\r\n").is_err() {
                    return;
                }
            }
            thread::sleep(Duration::from_millis(33));
        }
    } else if path.starts_with("/frame.jpg") {
        // One-shot: count as a client briefly so the loop renders a fresh frame.
        clients.fetch_add(1, Ordering::Relaxed);
        let _guard = ClientGuard(clients.clone());
        thread::sleep(Duration::from_millis(150));
        let jpg = latest.lock().unwrap().clone();
        let hdr = format!("HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n", jpg.len());
        let _ = s.write_all(hdr.as_bytes());
        let _ = s.write_all(&jpg);
    } else {
        let hdr = format!("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\n\r\n", PAGE.len());
        let _ = s.write_all(hdr.as_bytes());
        let _ = s.write_all(PAGE);
    }
}

fn stream(dir: &str, port: u16, fps: f64, target: &str) {
    // Prefer zero-copy GBM scanout (the GPU renders straight into the scanned-out
    // buffer — no readback). Fall back to surfaceless GL + the dumb-buffer sink,
    // else MJPEG only.
    let mut sc = scanout::Scanout::open();
    let egl;
    let dpy;
    let gl;
    let surf;
    let mut drm;
    if let Some(s) = &sc {
        let (e, d, sf, g) = s.init_gl();
        egl = e;
        dpy = d;
        gl = g;
        surf = Some(sf);
        drm = None;
    } else {
        println!("[scanout] no GBM/HDMI — surfaceless GL + dumb-buffer sink / MJPEG");
        let (e, d, g) = make_gl(target);
        egl = e;
        dpy = d;
        gl = g;
        surf = None;
        drm = sink::DrmSink::open();
        if drm.is_none() {
            println!("[sink] no DRM/HDMI output (headless or no permission) — MJPEG only");
        }
    }
    let hdmi = surf.is_some() || drm.is_some();

    let mut r = Renderer::new(&gl, dir);
    let prof = r.prof();
    let latest: Arc<Mutex<Vec<u8>>> = Arc::new(Mutex::new(Vec::new()));
    let clients: Arc<AtomicUsize> = Arc::new(AtomicUsize::new(0));
    {
        let latest = latest.clone();
        let clients = clients.clone();
        let prof = prof.clone();
        thread::spawn(move || serve_http(port, latest, clients, prof));
    }
    let start = Instant::now();
    let period = Duration::from_secs_f64(1.0 / fps.max(1.0));
    let mut last_log = Instant::now();
    loop {
        let has_client = clients.load(Ordering::Relaxed) > 0;
        // Render when the HDMI display is attached OR a web client is watching.
        if !hdmi && !has_client {
            thread::sleep(Duration::from_millis(100));
            continue;
        }
        let t = start.elapsed().as_secs_f64();
        let tf = Instant::now();
        r.cook(t); // graph passes into FBOs (no readback)

        // HDMI, zero-copy: GPU-blit the final texture into the scanout surface.
        if let (Some(sf), Some(s)) = (&surf, &mut sc) {
            let tp = Instant::now();
            r.present_scanout(s.dw as i32, s.dh as i32);
            let _ = egl.swap_buffers(dpy, *sf);
            let tc = Instant::now();
            prof_add(&prof, "present:blit", tp.elapsed().as_secs_f64());
            s.flip(); // page-flip the freshly rendered bo + vblank wait
            prof_add(&prof, "present:flip", tc.elapsed().as_secs_f64());
            prof_add(&prof, "present", tp.elapsed().as_secs_f64());
        }

        // Read back only when the dumb-buffer sink or a web client needs pixels.
        let need_rb = drm.is_some() || has_client;
        let rb = if need_rb { Some(r.readback()) } else { None };
        if let (Some(d), Some((buf, w, h))) = (&mut drm, &rb) {
            let tp = Instant::now();
            d.compose(buf, *w as usize, *h as usize);
            let tc = Instant::now();
            prof_add(&prof, "present:compose", tp.elapsed().as_secs_f64());
            d.flip();
            prof_add(&prof, "present:flip", tc.elapsed().as_secs_f64());
            prof_add(&prof, "present", tp.elapsed().as_secs_f64());
        }
        if has_client {
            if let Some((buf, w, h)) = &rb {
                let te = Instant::now();
                let mut rgb = Vec::with_capacity((w * h * 3) as usize);
                for px in buf.chunks_exact(4) {
                    rgb.extend_from_slice(&px[..3]);
                }
                let mut jpg = Vec::new();
                image::codecs::jpeg::JpegEncoder::new_with_quality(&mut jpg, 80)
                    .encode(&rgb, *w as u32, *h as u32, image::ExtendedColorType::Rgb8)
                    .unwrap();
                *latest.lock().unwrap() = jpg;
                prof_add(&prof, "encode", te.elapsed().as_secs_f64());
            }
        }
        prof_add(&prof, "frame", tf.elapsed().as_secs_f64());
        if last_log.elapsed().as_secs_f64() >= 5.0 {
            println!("{}", prof_summary(&prof));
            last_log = Instant::now();
        }
        let ft = start.elapsed().as_secs_f64() - t;
        if let Some(s) = period.checked_sub(Duration::from_secs_f64(ft.max(0.0))) {
            thread::sleep(s);
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).map(|s| s.as_str());
    match mode {
        Some("run") => {
            let dir = &args[2];
            let out = args.get(3).map(|s| s.as_str()).unwrap_or("out.png");
            let t = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(0.0);
            let wait_ms = args.get(5).and_then(|s| s.parse().ok()).unwrap_or(0);
            let (_egl, _dpy, gl) = make_gl(&artifact_target(dir));
            run(&gl, dir, out, t, wait_ms);
        }
        Some("stream") => {
            let dir = &args[2];
            let port = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(8788);
            let fps = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(30.0);
            // stream() creates its own context (GBM scanout, else surfaceless).
            stream(dir, port, fps, &artifact_target(dir));
        }
        _ => {
            let (_egl, _dpy, gl) = make_gl("desktop_gl");
            unsafe {
                println!("OK GL_RENDERER {}", gl.get_parameter_string(glow::RENDERER));
            }
        }
    }
}
