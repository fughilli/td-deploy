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
use std::collections::{HashMap, HashSet};
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
// time. null/select/math: passthrough of every channel of its input.
#[derive(Deserialize, Clone)]
struct ChopDef {
    name: String,
    #[serde(rename = "type")]
    ty: String,
    #[serde(default)]
    inputs: Vec<String>,
    #[serde(default)]
    channels: Vec<String>,
    /// Channel names. Expressions reference channels by name at least as often
    /// as by index (`op('spin1')['rx']`), so the store is keyed by both.
    #[serde(default)]
    names: Vec<String>,
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
    /// vec4 user uniforms from a GLSL TOP's "Vectors" page: name -> 4 per-frame
    /// components, each evaluated like a scalar time uniform.
    #[serde(default)]
    vec_uniforms: HashMap<String, Vec<TimeUniform>>,
    /// kind == "feedback": the step whose PREVIOUS frame this buffer holds.
    #[serde(default)]
    feedback_from: Option<String>,
    /// kind == "render3d": baked geometry and its material texture.
    #[serde(default)]
    mesh: Option<MeshRef>,
    #[serde(default)]
    texture: Option<String>,
}

/// Interleaved vertex buffer (pos3, nrm3, uv2 -> 32-byte stride) plus indices.
#[derive(Deserialize)]
struct MeshRef {
    vtx: String,
    idx: String,
    indices: i32,
    stride: i32,
    #[serde(default)]
    index_type: String,
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
    // Optional periodic wrap (fmod), applied in f64 after `mul` and before the
    // 32-bit uniform upload. Rotation is emitted with mod=2pi (lowering/lower.py):
    // an unbounded angle would lose its per-frame step to the f32 ULP once it
    // grows large (days of uptime) and the rotation cogs. None = no wrap.
    #[serde(rename = "mod", default)]
    modulo: Option<f64>,
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
    // Returns (channel 0-15, control_or_note, raw_value 0..127, is_note). RAW (not
    // normalized): TD's MIDI In CHOP is un-normalized, and exprs divide by 127
    // themselves (e.g. `op('midiin1')[0][0]/127 - 0.5`). The channel lets us name
    // the store the way TD does (chNctrlM), N = 1-based MIDI channel.
    fn push(&mut self, b: u8) -> Option<(u8, u8, f64, bool)> {
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
        let chan = self.status & 0x0F; // MIDI channel 0-15
        let need = if hi == 0xC0 || hi == 0xD0 { 1 } else { 2 };
        if self.have < need {
            return None;
        }
        self.have = 0; // keep status for running status
        match hi {
            0xB0 => Some((chan, self.data[0], self.data[1] as f64, false)),
            // note-on with velocity 0 is a note-off
            0x90 => Some((chan, self.data[0], if self.data[1] == 0 { 0.0 } else { self.data[1] as f64 }, true)),
            0x80 => Some((chan, self.data[0], 0.0, true)),
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
            if let Some((chan, ctrl, v, is_note)) = parser.push(byte[0]) {
                // TD's MIDI In CHOP is 1-based in BOTH parts of chNctrlM: channel N
                // = wire channel + 1, and controller M = raw CC + 1 (verified on an
                // Akai MidiMix — wire CC 16/20 show as ch1ctrl17/ch1ctrl21 in TD).
                let td_ch = chan + 1;
                let td_num = ctrl + 1;
                if is_note {
                    midi_set(&store, &name, format!("ch{td_ch}note{td_num}"), v); // TD chNnoteM
                    midi_set(&store, &name, format!("n{ctrl}"), v); // legacy (raw note #)
                } else {
                    midi_set(&store, &name, format!("ch{td_ch}ctrl{td_num}"), v); // TD chNctrlM
                    midi_set(&store, &name, format!("cc{ctrl}"), v); // legacy op('midiin1')['ccN'] (raw CC #)
                    midi_set(&store, &name, format!("{ctrl}"), v); // legacy op('midiin1')[N] (raw CC #)
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

    // (channel, control_or_note, raw_value, is_note)
    fn drive(bytes: &[u8]) -> Vec<(u8, u8, f64, bool)> {
        let mut p = MidiParser::default();
        bytes.iter().filter_map(|&b| p.push(b)).collect()
    }

    #[test]
    fn cc_is_raw() {
        // CC13 = 127 (raw; exprs normalize themselves via /127). false = not a note.
        // 0xB0 = CC on channel 0 -> TD ch1.
        assert_eq!(drive(&[0xB0, 13, 127]), vec![(0, 13, 127.0, false)]);
    }

    #[test]
    fn cc_channel_is_captured() {
        // 0xB1 = CC on channel 1 (TD ch2). The MidiMix's ch1ctrl17/ch1ctrl21 come
        // in on channel 0 (0xB0); this checks a non-zero channel isn't masked off.
        assert_eq!(drive(&[0xB1, 17, 100]), vec![(1, 17, 100.0, false)]);
    }

    #[test]
    fn running_status_repeats_cc() {
        // status byte sent once, then two data pairs (running status).
        let out = drive(&[0xB0, 1, 64, 2, 0]);
        assert_eq!(out, vec![(0, 1, 64.0, false), (0, 2, 0.0, false)]);
    }

    #[test]
    fn note_on_zero_velocity_is_note_off() {
        let out = drive(&[0x90, 60, 100, 0x90, 60, 0]);
        assert_eq!(out, vec![(0, 60, 100.0, true), (0, 60, 0.0, true)]);
    }

    #[test]
    fn realtime_clock_interleaves_without_breaking_message() {
        // 0xF8 (clock) between the CC data bytes must be ignored, not corrupt it.
        assert_eq!(drive(&[0xB0, 13, 0xF8, 100]), vec![(0, 13, 100.0, false)]);
    }
}

/// Resolve a compiled expression's `chop_<name>_<channel>` argument.
///
/// That encoding is AMBIGUOUS as soon as the CHOP's own name contains an
/// underscore: `chop_in_sat_sat` is (in_sat, sat) but splitting at the first
/// separator reads it as (in, sat_sat), which matches nothing and silently
/// evaluates to 0 — a knob wired through a COMP inlet named `in_sat` simply
/// stopped working with no error anywhere. Try every split, longest CHOP name
/// first so the most specific match wins, and take the one the store actually
/// holds.
fn chop_value(store: &Chops, input: &str) -> f64 {
    let rest = match input.strip_prefix("chop_") {
        Some(r) => r,
        None => return 0.0,
    };
    let g = store.lock().unwrap();
    let cuts: Vec<usize> = rest.match_indices('_').map(|(i, _)| i).collect();
    for i in cuts.iter().rev() {
        if let Some(v) = g.get(&rest[..*i]).and_then(|m| m.get(&rest[i + 1..])) {
            return *v;
        }
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

/// Copy `src_tex` into `dst_tex` through a scratch FBO.
///
/// glCopyTexSubImage2D is core in both GL 2.0 and GLES 2.0, so the Feedback TOP
/// needs no target-specific blit shader. Both textures live in the same bottom-up
/// GL space, so this is a straight 1:1 copy with no filtering.
fn capture_tex(
    gl: &glow::Context,
    scratch: glow::Framebuffer,
    src_tex: glow::Texture,
    dst_tex: glow::Texture,
    w: i32,
    h: i32,
) {
    if w <= 0 || h <= 0 {
        return;
    }
    unsafe {
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(scratch));
        gl.framebuffer_texture_2d(
            glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(src_tex), 0);
        gl.bind_texture(glow::TEXTURE_2D, Some(dst_tex));
        gl.copy_tex_sub_image_2d(glow::TEXTURE_2D, 0, 0, 0, 0, 0, w, h);
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
    // Feedback TOPs: a scratch FBO used to copy a target's frame into the
    // feedback buffer, and the set of buffers already seeded from their input.
    capture_fbo: Option<glow::Framebuffer>,
    fb_seeded: HashSet<String>,
    // Incremental cook. `step_sig` is the last evaluated per-frame uniform vector
    // for each step, `last_dirty` which steps actually redrew on the previous
    // frame. A step whose inputs and uniforms are unchanged still holds last
    // frame's pixels in its own FBO, so the draw can simply be skipped.
    // Mesh passes: geometry buffers, a depth attachment and the material texture,
    // none of which a full-screen fragment pass ever needs.
    vbo: HashMap<String, glow::Buffer>,
    ebo: HashMap<String, glow::Buffer>,
    mesh_tex: HashMap<String, glow::Texture>,
    step_sig: HashMap<String, Vec<f64>>,
    last_dirty: HashMap<String, bool>,
    cooked_once: bool,
    // Control-rate (CHOP) evaluation: the pre-compiled DAG + integrator state.
    chop_progs: Vec<ChopProg>,
    // The compiled CHOP kernel (chops_v), if the DAG was fully lowered. Preferred
    // over chop_progs; the fasteval loop is the fallback for unlowerable DAGs.
    chops_lib: Option<libloading::Library>,
    chops_abi: Option<ChopAbi>,
    chop_state: RefCell<Vec<f64>>,      // carried Speed accumulators (abi.states)
    chop_state_idx: Vec<usize>,         // each state's position in abi.outputs
    chop_names: HashMap<String, Vec<String>>,  // chop -> channel names, for either path
    // Pre-compiled interpreted uniforms, keyed by (step index, uniform name).
    uniform_progs: HashMap<(usize, String), expr::Program>,
    speed_state: RefCell<HashMap<(String, String), f64>>,
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
    names: Vec<String>,
}

/// Evaluate one per-frame uniform: the compiled expression when the artifact
/// carries one, else the interpreted fallback, else zero.
fn eval_tu(
    tu: &TimeUniform,
    exprs: &Option<libloading::Library>,
    interp: Option<&expr::Program>,
    store: &Chops,
    t: f64,
    frame: f64,
) -> f64 {
    let v = if let (Some(func), Some(lib)) = (&tu.func, exprs) {
        let args: Vec<f64> = tu
            .inputs
            .iter()
            .map(|n| match n.as_str() {
                "t" => t,
                "frame" => frame,
                other => chop_value(store, other),
            })
            .collect();
        (unsafe { call_expr(lib, func, &args) }) * tu.mul
    } else if let Some(prog) = interp {
        let st = store.clone();
        prog.eval(t, frame, &|n, ch| chop_get(&st, n, ch)) * tu.mul
    } else {
        0.0
    };
    match tu.modulo {
        Some(m) if m != 0.0 => v % m,
        _ => v,
    }
}

/// Write a channel under BOTH its index and its name, so `op('x')[0]` and
/// `op('x')['rx']` resolve to the same value.
fn chop_put(store: &Chops, chop: &str, i: usize, names: &[String], v: f64) {
    chop_set(store, chop, &i.to_string(), v);
    if let Some(n) = names.get(i) {
        if !n.is_empty() {
            chop_set(store, chop, n, v);
        }
    }
}

/// How many channels a CHOP published, counted by its numeric keys.
///
/// Only meaningful for a CHOP the DAG itself wrote, because `chop_put` gives
/// those a dense 0..n. A live MIDI/OSC service is keyed by NAME (`ch1ctrl5`,
/// `cc4`) and has no such indexing — and MIDI also writes the raw CC number as
/// a key, so counting from 0 there returns whatever CC numbers happen to be in
/// use rather than a channel count. Use `chop_keys` for those.
fn chop_width(store: &Chops, name: &str) -> usize {
    let g = store.lock().unwrap();
    let m = match g.get(name) {
        Some(m) => m,
        None => return 0,
    };
    (0..).take_while(|i| m.contains_key(&i.to_string())).count()
}

/// Every channel key a CHOP published, numeric keys first and in numeric order
/// so the ordering is stable across frames (HashMap iteration is not).
fn chop_keys(store: &Chops, name: &str) -> Vec<String> {
    let g = store.lock().unwrap();
    let mut ks: Vec<String> = match g.get(name) {
        Some(m) => m.keys().cloned().collect(),
        None => return Vec::new(),
    };
    ks.sort_by(|a, b| match (a.parse::<u64>(), b.parse::<u64>()) {
        (Ok(x), Ok(y)) => x.cmp(&y),
        (Ok(_), Err(_)) => std::cmp::Ordering::Less,
        (Err(_), Ok(_)) => std::cmp::Ordering::Greater,
        (Err(_), Err(_)) => a.cmp(b),
    });
    ks
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
/// Read a shader the schedule references, naming the file if it is missing —
/// an artifact that was emitted without its translated `shaders_gles/` is an easy
/// mistake to make and an opaque unwrap panic is a poor way to find out.
fn read_shader(dir: &str, gles2: bool, rel: &str) -> String {
    let path = resolve_shader(dir, gles2, rel);
    match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) => panic!(
            "shader {path} not found ({e}). The artifact may be missing its \
             translated shaders_gles/ — re-run the finish step before deploying."
        ),
    }
}

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
                names: c.names.clone(),
            })
            .collect();
        let chop_names: HashMap<String, Vec<String>> = sched
            .chops
            .iter()
            .map(|c| (c.name.clone(), c.names.clone()))
            .collect();
        let mut uniform_progs = HashMap::new();
        for (si, st) in sched.steps.iter().enumerate() {
            for (name, tu) in &st.time_uniforms {
                if let Some(s) = &tu.interpreted {
                    uniform_progs.insert((si, name.clone()), expr::Program::compile(s));
                }
            }
            // vec4 components are keyed "<name>[<component>]" so they share the
            // same interpreted-program table as scalars.
            for (name, comps) in &st.vec_uniforms {
                for (k, tu) in comps.iter().take(4).enumerate() {
                    if let Some(s) = &tu.interpreted {
                        uniform_progs
                            .insert((si, format!("{name}[{k}]")), expr::Program::compile(s));
                    }
                }
            }
        }
        let mut r = Renderer {
            gl, dir: dir.to_string(), sched, exprs, store,
            tex: HashMap::new(), fbo: HashMap::new(), prog: HashMap::new(), size: HashMap::new(),
            gles2, quad: None, blit_prog: None,
            capture_fbo: None, fb_seeded: HashSet::new(),
            vbo: HashMap::new(), ebo: HashMap::new(), mesh_tex: HashMap::new(),
            step_sig: HashMap::new(), last_dirty: HashMap::new(), cooked_once: false,
            chop_progs, chops_lib, chops_abi, chop_state, chop_state_idx, uniform_progs,
            chop_names,
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
    // speed = time-integral of its input, per channel; others pass every channel.
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
                // The fused kernel addresses channels by INDEX; expressions may
                // name them, so mirror each value under its channel name too.
                if let Ok(ci) = c.parse::<usize>() {
                    if let Some(nm) = self.chop_names.get(n).and_then(|v| v.get(ci)) {
                        if !nm.is_empty() {
                            chop_set(&self.store, n, nm, out[i]);
                        }
                    }
                }
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
        // Names the DAG itself defines. Anything else appearing in `inputs` is a
        // live MIDI/OSC service: the importer keeps it wired as an input but
        // leaves it out of the DAG, since it has no expression to evaluate.
        let defined: HashSet<&str> = self.chop_progs.iter().map(|c| c.name.as_str()).collect();
        // A CHOP carrying a live service cannot be walked as 0..n — the service
        // publishes NAMED channels. Such a CHOP copies by key instead, and since
        // it then has no dense indexing of its own, everything downstream of it
        // has to copy by key too.
        let mut by_key: HashSet<&str> = HashSet::new();
        for cp in &self.chop_progs {
            match cp.ty.as_str() {
                "constant" => {
                    for (i, prog) in cp.chans.iter().enumerate() {
                        let store = self.store.clone();
                        let v = prog.eval(t, frame, &|n, ch| chop_get(&store, n, ch));
                        chop_put(&self.store, &cp.name, i, &cp.names, v);
                    }
                }
                ty => {
                    let inp = match cp.inputs.first() {
                        Some(i) => i,
                        None => continue,
                    };
                    let keyed = !defined.contains(inp.as_str()) || by_key.contains(inp.as_str());
                    if keyed {
                        by_key.insert(cp.name.as_str());
                    }
                    // Index-walk what this runtime wrote itself; key-walk a live
                    // source (or anything carrying one).
                    let chans: Vec<String> = if keyed {
                        chop_keys(&self.store, inp)
                    } else {
                        (0..chop_width(&self.store, inp).max(1)).map(|i| i.to_string()).collect()
                    };
                    for (i, ch) in chans.iter().enumerate() {
                        let iv = chop_get(&self.store, inp, ch);
                        // A Speed integrates each channel independently.
                        let v = if ty == "speed" {
                            let mut ss = self.speed_state.borrow_mut();
                            let acc = ss.entry((cp.name.clone(), ch.clone())).or_insert(0.0);
                            *acc += iv * dt;
                            *acc
                        } else {
                            iv
                        };
                        if keyed {
                            // Keep the source's own channel names; there is no
                            // index for them to land on.
                            chop_set(&self.store, &cp.name, ch, v);
                        } else {
                            chop_put(&self.store, &cp.name, i, &cp.names, v);
                        }
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
                    &read_shader(&dir, gles2, st.vert.as_ref().unwrap()));
                let fs = compile(gl, glow::FRAGMENT_SHADER,
                    &read_shader(&dir, gles2, st.frag.as_ref().unwrap()));
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
            } else if st.kind == "render3d" {
                let vs = compile(gl, glow::VERTEX_SHADER,
                    &read_shader(&dir, gles2, st.vert.as_ref().unwrap()));
                let fs = compile(gl, glow::FRAGMENT_SHADER,
                    &read_shader(&dir, gles2, st.frag.as_ref().unwrap()));
                unsafe {
                    let p = gl.create_program().unwrap();
                    gl.attach_shader(p, vs);
                    gl.attach_shader(p, fs);
                    gl.link_program(p);
                    assert!(gl.get_program_link_status(p), "link: {}", gl.get_program_info_log(p));
                    let ot = make_tex(gl, st.w, st.h, None, gles2);
                    let f = gl.create_framebuffer().unwrap();
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
                    gl.framebuffer_texture_2d(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0,
                        glow::TEXTURE_2D, Some(ot), 0);
                    // Unlike a full-screen pass, a mesh needs depth or far
                    // triangles paint over near ones.
                    let rb = gl.create_renderbuffer().unwrap();
                    gl.bind_renderbuffer(glow::RENDERBUFFER, Some(rb));
                    gl.renderbuffer_storage(glow::RENDERBUFFER, glow::DEPTH_COMPONENT16, st.w, st.h);
                    gl.framebuffer_renderbuffer(glow::FRAMEBUFFER, glow::DEPTH_ATTACHMENT,
                        glow::RENDERBUFFER, Some(rb));
                    self.prog.insert(st.id.clone(), p);
                    self.tex.insert(st.id.clone(), ot);
                    self.fbo.insert(st.id.clone(), f);
                    self.size.insert(st.id.clone(), (st.w, st.h));

                    if let Some(m) = &st.mesh {
                        let vdata = std::fs::read(format!("{}/{}", dir, m.vtx)).unwrap();
                        let idata = std::fs::read(format!("{}/{}", dir, m.idx)).unwrap();
                        let vb = gl.create_buffer().unwrap();
                        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vb));
                        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, &vdata, glow::STATIC_DRAW);
                        let ib = gl.create_buffer().unwrap();
                        gl.bind_buffer(glow::ELEMENT_ARRAY_BUFFER, Some(ib));
                        gl.buffer_data_u8_slice(glow::ELEMENT_ARRAY_BUFFER, &idata, glow::STATIC_DRAW);
                        self.vbo.insert(st.id.clone(), vb);
                        self.ebo.insert(st.id.clone(), ib);
                        println!("[mesh] {} {} indices ({} bytes vtx)", st.id, m.indices, vdata.len());
                    } else {
                        eprintln!("[mesh] {} has no geometry — nothing to draw", st.id);
                    }
                    if let Some(tp) = &st.texture {
                        let im = image::open(format!("{}/{}", dir, tp)).unwrap().to_rgba8();
                        let (tw, th) = (im.width() as usize, im.height() as usize);
                        let data = im.into_raw();
                        let t = make_tex(gl, tw as i32, th as i32,
                                         Some(&flip_vert(&data, tw, th)), gles2);
                        self.mesh_tex.insert(st.id.clone(), t);
                    }
                }
            } else if st.kind == "feedback" {
                // A Feedback TOP owns a texture that PERSISTS between frames; it
                // has no shader. Start it cleared so frame 0 has no garbage.
                unsafe {
                    let ot = make_tex(gl, st.w, st.h, None, gles2);
                    let f = gl.create_framebuffer().unwrap();
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
                    gl.framebuffer_texture_2d(
                        glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(ot), 0);
                    gl.viewport(0, 0, st.w, st.h);
                    gl.clear_color(0.0, 0.0, 0.0, 0.0);
                    gl.clear(glow::COLOR_BUFFER_BIT);
                    self.tex.insert(st.id.clone(), ot);
                    self.fbo.insert(st.id.clone(), f);
                    self.size.insert(st.id.clone(), (st.w, st.h));
                }
            }
        }
        if self.sched.steps.iter().any(|s| s.kind == "feedback") && self.capture_fbo.is_none() {
            unsafe { self.capture_fbo = gl.create_framebuffer().ok(); }
        }
    }

    // Render the graph's TOP passes into their FBOs (no readback). Final output
    // lands in self.tex[output].
    fn cook(&mut self, t: f64) {
        let gl = self.gl;
        let tc = Instant::now();
        self.eval_chops(t); // control-rate pass -> store, before the shader uniforms read it
        prof_add(&self.prof, "chop:eval", tc.elapsed().as_secs_f64());
        let scratch = self.capture_fbo;
        // Which steps produce new pixels this frame. A step is dirty when it has
        // never cooked, when anything it samples is dirty, or when one of its
        // per-frame uniforms actually changed value — so a static subgraph (a
        // Noise TOP with constant parameters, say) draws once and is then
        // replayed from its own texture for free. This is decided from the
        // evaluated values rather than from compile-time analysis, so it keeps
        // working however the graph is rewritten upstream.
        let first = !self.cooked_once;
        let mut dirty: HashMap<String, bool> = HashMap::new();
        for (si, st) in self.sched.steps.iter().enumerate() {
            if st.kind == "feedback" {
                // The buffer already holds the previous frame's target — exactly
                // what downstream should sample, so there is nothing to draw.
                // Seed it once from input 0 (TD's reset image); that edge is
                // delay-0, so it has already cooked by the time we get here.
                if !self.fb_seeded.contains(&st.id) {
                    if let (Some(scratch), Some(src)) = (scratch, st.inputs.first()) {
                        if let (Some(&sx), Some(&dx)) = (self.tex.get(src), self.tex.get(&st.id)) {
                            let (dw, dh) = self.size[&st.id];
                            let (sw, sh) = self.size[src];
                            capture_tex(gl, scratch, sx, dx, dw.min(sw), dh.min(sh));
                        }
                    }
                    self.fb_seeded.insert(st.id.clone());
                }
                // The buffer's pixels changed if the target redrew LAST frame —
                // that is when the end-of-frame capture copied new content in.
                let tgt_moved = st.feedback_from.as_ref()
                    .map(|f| *self.last_dirty.get(f).unwrap_or(&true))
                    .unwrap_or(false);
                dirty.insert(st.id.clone(), first || tgt_moved);
            } else if st.kind == "render3d" {
                // Same signature gate as a shader pass: a scene whose transform
                // expressions are unchanged does not need redrawing.
                let frame = (t * 60.0).floor();
                let mut names: Vec<&String> = Vec::with_capacity(st.time_uniforms.len());
                let mut sig: Vec<f64> = Vec::with_capacity(st.time_uniforms.len());
                for (name, tu) in &st.time_uniforms {
                    let v = if let (Some(func), Some(lib)) = (&tu.func, &self.exprs) {
                        let args: Vec<f64> = tu.inputs.iter().map(|n| match n.as_str() {
                            "t" => t,
                            "frame" => frame,
                            other => chop_value(&self.store, other),
                        }).collect();
                        (unsafe { call_expr(lib, func, &args) }) * tu.mul
                    } else if let Some(prog) = self.uniform_progs.get(&(si, name.clone())) {
                        let store = self.store.clone();
                        prog.eval(t, frame, &|n, ch| chop_get(&store, n, ch)) * tu.mul
                    } else {
                        0.0
                    };
                    names.push(name);
                    sig.push(match tu.modulo { Some(m) if m != 0.0 => v % m, _ => v });
                }
                let changed = self.step_sig.get(&st.id).map_or(true, |prev| prev != &sig);
                let must_draw = first || changed;
                dirty.insert(st.id.clone(), must_draw);
                if must_draw {
                    let ts = Instant::now();
                    unsafe {
                        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.fbo[&st.id]));
                        gl.viewport(0, 0, st.w, st.h);
                        gl.enable(glow::DEPTH_TEST);
                        gl.depth_func(glow::LESS);
                        gl.clear_color(0.0, 0.0, 0.0, 0.0);
                        gl.clear(glow::COLOR_BUFFER_BIT | glow::DEPTH_BUFFER_BIT);
                        let p = self.prog[&st.id];
                        gl.use_program(Some(p));
                        for (name, u) in &st.uniforms {
                            if let Some(l) = gl.get_uniform_location(p, name) {
                                if u.ty == "float" {
                                    gl.uniform_1_f32(Some(&l), u.value.as_f64().unwrap() as f32);
                                }
                            }
                        }
                        for (name, v) in names.iter().zip(sig.iter()) {
                            if let Some(l) = gl.get_uniform_location(p, name) {
                                gl.uniform_1_f32(Some(&l), *v as f32);
                            }
                        }
                        if let Some(mt) = self.mesh_tex.get(&st.id) {
                            gl.active_texture(glow::TEXTURE0);
                            gl.bind_texture(glow::TEXTURE_2D, Some(*mt));
                            if let Some(l) = gl.get_uniform_location(p, "tex0") {
                                gl.uniform_1_i32(Some(&l), 0);
                            }
                        }
                        if let (Some(m), Some(vb), Some(ib)) =
                            (&st.mesh, self.vbo.get(&st.id), self.ebo.get(&st.id))
                        {
                            gl.bind_buffer(glow::ARRAY_BUFFER, Some(*vb));
                            gl.bind_buffer(glow::ELEMENT_ARRAY_BUFFER, Some(*ib));
                            // pos3 | nrm3 | uv2, tightly interleaved.
                            for (attr, comps, off) in
                                [("aPos", 3, 0i32), ("aNrm", 3, 12), ("aUV", 2, 24)]
                            {
                                if let Some(loc) = gl.get_attrib_location(p, attr) {
                                    gl.enable_vertex_attrib_array(loc);
                                    gl.vertex_attrib_pointer_f32(
                                        loc, comps, glow::FLOAT, false, m.stride, off);
                                }
                            }
                            let ity = if m.index_type == "u32" {
                                glow::UNSIGNED_INT
                            } else {
                                glow::UNSIGNED_SHORT
                            };
                            gl.draw_elements(glow::TRIANGLES, m.indices, ity, 0);
                        }
                        gl.disable(glow::DEPTH_TEST);
                        if self.profile_gpu {
                            gl.finish();
                        }
                    }
                    prof_add(&self.prof, &format!("top:{}", st.id), ts.elapsed().as_secs_f64());
                    self.step_sig.insert(st.id.clone(), sig);
                }
            } else if st.kind == "passthrough" {
                if let Some(src) = st.inputs.first() {
                    let (a, b) = (self.tex[src], self.size[src]);
                    self.tex.insert(st.id.clone(), a);
                    self.size.insert(st.id.clone(), b);
                }
                // An alias is exactly as fresh as what it aliases.
                let d = st.inputs.first()
                    .map(|i| *dirty.get(i).unwrap_or(&true)).unwrap_or(first);
                dirty.insert(st.id.clone(), d);
            } else if st.kind == "shader" {
                // The per-frame uniforms ARE this step's signature. Evaluate them
                // first (cheap — a few expression calls) so they can be compared
                // against last frame; the expensive part is the draw that follows.
                let frame = (t * 60.0).floor();
                let mut names: Vec<&String> = Vec::with_capacity(st.time_uniforms.len());
                let mut sig: Vec<f64> = Vec::with_capacity(st.time_uniforms.len());
                for (name, tu) in &st.time_uniforms {
                    let v = if let (Some(func), Some(lib)) = (&tu.func, &self.exprs) {
                        let args: Vec<f64> = tu.inputs.iter().map(|n| match n.as_str() {
                            "t" => t,
                            "frame" => frame,
                            other => chop_value(&self.store, other),
                        }).collect();
                        // Calls into the dlopen'd expression library.
                        (unsafe { call_expr(lib, func, &args) }) * tu.mul
                    } else if let Some(prog) = self.uniform_progs.get(&(si, name.clone())) {
                        // Interpreted expr (op('name')[i], absTime, math) via fasteval.
                        let store = self.store.clone();
                        prog.eval(t, frame, &|n, ch| chop_get(&store, n, ch)) * tu.mul
                    } else {
                        0.0
                    };
                    // Wrap periodic uniforms (rotation: mod=2pi) in f64 before the
                    // f32 cast, so a large angle keeps full float precision.
                    names.push(name);
                    sig.push(match tu.modulo { Some(m) if m != 0.0 => v % m, _ => v });
                }
                // vec4 user uniforms (a GLSL TOP's "Vectors" page). Their
                // components join the signature so a knob driving one still wakes
                // the step up.
                let mut vecs: Vec<(&String, [f32; 4])> = Vec::new();
                for (name, comps) in &st.vec_uniforms {
                    let mut v = [0.0f32; 4];
                    for (k, tu) in comps.iter().take(4).enumerate() {
                        let interp = self
                            .uniform_progs
                            .get(&(si, format!("{name}[{k}]")));
                        let value = eval_tu(tu, &self.exprs, interp, &self.store, t, frame);
                        v[k] = value as f32;
                        sig.push(value);
                    }
                    vecs.push((name, v));
                }
                let inputs_dirty = st.inputs.iter().any(|i| *dirty.get(i).unwrap_or(&true));
                let changed = self.step_sig.get(&st.id).map_or(true, |prev| prev != &sig);
                let must_draw = first || inputs_dirty || changed;
                dirty.insert(st.id.clone(), must_draw);
                if must_draw {
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
                    for (name, v) in names.iter().zip(sig.iter()) {
                        if let Some(l) = gl.get_uniform_location(p, name) {
                            gl.uniform_1_f32(Some(&l), *v as f32);
                        }
                    }
                    for (name, v) in &vecs {
                        if let Some(l) = gl.get_uniform_location(p, name) {
                            gl.uniform_4_f32(Some(&l), v[0], v[1], v[2], v[3]);
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
                self.step_sig.insert(st.id.clone(), sig);
                }
            }
        }

        // End of frame: copy each Feedback TOP's target into its persistent
        // buffer, so the NEXT frame reads what this frame produced. Done after
        // the whole cook because a target is normally DOWNSTREAM of the feedback
        // that echoes it — that loop is what makes it a feedback in the first place.
        if let Some(scratch) = scratch {
            let tf = Instant::now();
            for st in self.sched.steps.iter() {
                if st.kind != "feedback" {
                    continue;
                }
                if let Some(src) = &st.feedback_from {
                    if let (Some(&sx), Some(&dx)) = (self.tex.get(src), self.tex.get(&st.id)) {
                        let (dw, dh) = self.size[&st.id];
                        let (sw, sh) = self.size[src];
                        capture_tex(gl, scratch, sx, dx, dw.min(sw), dh.min(sh));
                    }
                }
            }
            prof_add(&self.prof, "feedback:capture", tf.elapsed().as_secs_f64());
        }
        self.last_dirty = dirty;
        self.cooked_once = true;
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

// Self-contained frame-timing page served at /perf: polls /stats (same origin) and
// plots the per-frame loop breakdown (stacked), so performance is viewable from any
// browser with no app installed — same visualization as the app's Performance pane.
const PERF_HTML: &str = r#"<!doctype html><html><head><meta charset="utf-8">
<title>td-deploy — performance</title><style>
body{margin:0;background:#14161a;color:#e7e9ee;font:13px -apple-system,system-ui,sans-serif;padding:14px}
h1{font-size:15px;margin:0 0 8px}#summary{font-size:12px;color:#8b909c;margin-bottom:8px}
canvas{width:100%;height:240px;display:block;background:#0f1116;border:1px solid #2b2f39;border-radius:6px}
#legend{display:flex;flex-wrap:wrap;gap:8px 14px;margin-top:10px;font:11px ui-monospace,Menlo,monospace;color:#8b909c}
.leg{display:inline-flex;align-items:center;gap:5px}.sw{width:10px;height:10px;border-radius:2px}
</style></head><body><h1>td-deploy — frame timing</h1>
<div id="summary">connecting…</div><canvas id="c"></canvas><div id="legend"></div><script>
const MAXN=180,MS=500;let prev=null,samples=[];
function excl(k){return k==='frame'||k==='present';}
function color(l){let h=0;for(let i=0;i<l.length;i++)h=(h*31+l.charCodeAt(i))>>>0;return`hsl(${h%360} 65% 55%)`;}
async function poll(){let s;try{s=await(await fetch('/stats',{cache:'no-store'})).json();}catch(e){document.getElementById('summary').textContent='no /stats';prev=null;return;}
const now={};for(const k in s)now[k]={total:s[k].total_ms||0,count:s[k].count||0};
if(prev&&prev.frame&&now.frame&&now.frame.count>prev.frame.count){const df=now.frame.count-prev.frame.count,parts={};
for(const k in now){if(excl(k))continue;const p=prev[k];if(!p)continue;const dt=now[k].total-p.total;if(dt>0)parts[k]=dt/df;}
const fr=(now.frame.total-prev.frame.total)/df;samples.push({parts,frame:fr,fps:fr>0?1000/fr:0});if(samples.length>MAXN)samples.shift();draw();
const last=samples[samples.length-1];document.getElementById('summary').textContent=`frame ${last.frame.toFixed(1)} ms · ${last.fps.toFixed(1)} fps`;}
prev=now;}
function draw(){const cv=document.getElementById('c'),w=cv.clientWidth||800,h=240;if(cv.width!==w)cv.width=w;cv.height=h;
const ctx=cv.getContext('2d');ctx.clearRect(0,0,w,h);if(!samples.length)return;let ymax=20;
for(const smp of samples){let su=0;for(const k in smp.parts)su+=smp.parts[k];ymax=Math.max(ymax,su,smp.frame);}ymax*=1.1;
const labels=Array.from(new Set(samples.flatMap(s=>Object.keys(s.parts)))).sort(),n=samples.length,bw=w/MAXN;
for(let i=0;i<n;i++){const smp=samples[i],x=w-(n-i)*bw;let y=h;for(const l of labels){const v=smp.parts[l]||0;if(v<=0)continue;const ph=(v/ymax)*h;ctx.fillStyle=color(l);ctx.fillRect(x,y-ph,Math.ceil(bw),ph);y-=ph;}}
ctx.strokeStyle='rgba(255,255,255,0.25)';ctx.lineWidth=1;for(const ms of [1000/30,1000/60]){const y=h-(ms/ymax)*h;if(y>0&&y<h){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();}}
const last=samples[samples.length-1],lg=document.getElementById('legend');lg.innerHTML='';
for(const l of labels){const v=last.parts[l]||0,it=document.createElement('span');it.className='leg';const sw=document.createElement('span');sw.className='sw';sw.style.background=color(l);it.appendChild(sw);it.appendChild(document.createTextNode(`${l} ${v.toFixed(1)}`));lg.appendChild(it);}}
poll();setInterval(poll,MS);
</script></body></html>"#;

// Count of clients currently pulling frames. The render loop only renders +
// encodes while this is > 0, so an idle box (no viewer) spends no CPU/GPU.
fn serve_http(port: u16, latest: Arc<Mutex<Vec<u8>>>, clients: Arc<AtomicUsize>, prof: Prof, store: Chops) {
    let l = TcpListener::bind(("0.0.0.0", port)).expect("bind http");
    println!("[stream] native MJPEG on http://0.0.0.0:{port}/  (perf graph at /perf, counters at /stats, CHOP/MIDI store at /chops)");
    for c in l.incoming().flatten() {
        let latest = latest.clone();
        let clients = clients.clone();
        let prof = prof.clone();
        let store = store.clone();
        thread::spawn(move || handle_conn(c, latest, clients, prof, store));
    }
}

// Decrement the client count when a viewer's handler returns, on every path.
struct ClientGuard(Arc<AtomicUsize>);
impl Drop for ClientGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}

fn handle_conn(mut s: TcpStream, latest: Arc<Mutex<Vec<u8>>>, clients: Arc<AtomicUsize>, prof: Prof, store: Chops) {
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
    } else if path.starts_with("/chops") {
        // The live CHOP/MIDI store: chop_name -> { channel -> value }. Wiggle a
        // knob and GET /chops to see the exact key a controller produces (e.g.
        // `{"midiin1":{"ch1ctrl21":100.0,"cc21":100.0,"21":100.0}}`).
        let body = serde_json::to_string(&*store.lock().unwrap()).unwrap_or_else(|_| "{}".into());
        let hdr = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nAccess-Control-Allow-Origin: *\r\n\r\n", body.len());
        let _ = s.write_all(hdr.as_bytes());
        let _ = s.write_all(body.as_bytes());
    } else if path.starts_with("/perf") {
        // Standalone frame-timing page (polls /stats) — browser diagnostics, no app.
        let hdr = format!("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n\r\n", PERF_HTML.len());
        let _ = s.write_all(hdr.as_bytes());
        let _ = s.write_all(PERF_HTML.as_bytes());
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
    let gl;
    let mut fallback_egl = None; // keeps the surfaceless EGL alive on the fallback path
    let mut drm = None;
    match sc.as_mut() {
        Some(s) => {
            gl = s.init_gl(); // scanout owns its EGL (so it can recreate on hotplug)
        }
        None => {
            println!("[scanout] no GBM/HDMI — surfaceless GL + dumb-buffer sink / MJPEG");
            let (e, _d, g) = make_gl(target);
            gl = g;
            fallback_egl = Some(e);
            drm = sink::DrmSink::open();
            if drm.is_none() {
                println!("[sink] no DRM/HDMI output (headless or no permission) — MJPEG only");
            }
        }
    }
    let _ = &fallback_egl; // keep-alive only
    let hdmi = sc.is_some() || drm.is_some();

    let mut r = Renderer::new(&gl, dir);
    let prof = r.prof();
    let latest: Arc<Mutex<Vec<u8>>> = Arc::new(Mutex::new(Vec::new()));
    let clients: Arc<AtomicUsize> = Arc::new(AtomicUsize::new(0));
    {
        let latest = latest.clone();
        let clients = clients.clone();
        let prof = prof.clone();
        let store = r.store.clone();
        thread::spawn(move || serve_http(port, latest, clients, prof, store));
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
        // HDMI hotplug: pick up a newly-attached display (or a mode change) and
        // reconfigure scanout, before we render this frame.
        if let Some(s) = sc.as_mut() {
            s.poll_hotplug();
        }
        let t = start.elapsed().as_secs_f64();
        let tf = Instant::now();
        r.cook(t); // graph passes into FBOs (no readback)

        // HDMI, zero-copy: GPU-blit the final texture into the scanout surface.
        if let Some(s) = sc.as_mut() {
            let tp = Instant::now();
            r.present_scanout(s.dw as i32, s.dh as i32);
            s.swap();
            let tb = Instant::now();
            prof_add(&prof, "present:blit", (tb - tp).as_secs_f64());
            // Drain the GPU (glFinish) BEFORE the page-flip so we can attribute the
            // frame to GPU render vs vblank wait separately — without this the flip
            // lumps both together (a large "present:flip" then can't tell GPU-bound
            // from vsync-bound). No net cost: the flip fences on render completion
            // anyway; this just moves the wait somewhere we can time it.
            unsafe { r.gl.finish() };
            let tg = Instant::now();
            prof_add(&prof, "gpu", (tg - tb).as_secs_f64());
            s.flip(); // page-flip: now a pure vblank wait (GPU already drained)
            prof_add(&prof, "present:flip", tg.elapsed().as_secs_f64());
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
        // Software fps cap ONLY when nothing else paces us. On HDMI the page-flip
        // already blocks to vblank, so an extra sleep just fights vsync and adds
        // beat (two unsynchronized throttles) — skip it there. Headless/MJPEG has no
        // vsync, so the cap governs there.
        if !hdmi {
            let ft = start.elapsed().as_secs_f64() - t;
            if let Some(s) = period.checked_sub(Duration::from_secs_f64(ft.max(0.0))) {
                thread::sleep(s);
            }
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
