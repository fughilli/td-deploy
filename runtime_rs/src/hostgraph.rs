// Renderer for Python-host artifacts (schedule format "toxc-host/1", written by
// compiler/host_compile.py).
//
// Every dynamic value in the schedule is a *binding* the Python host evaluates
// each frame: `["b", i]` indexes the host's float vector (parameters, then
// flags), `mat` fields index its world-matrix list. The node list is in cook
// order; sizes follow TouchDesigner's rules and are re-evaluated every frame, so
// a Script TOP that changes resolution or an expression-driven Resolution just
// works (targets are reallocated on change).
//
// Per frame:
//   host.frame(t, readbacks)  -> bindings, Script TOP pixels, CHOP channels,
//                                Script SOP meshes
//   cook every node (on-demand nodes only when a script asked for their pixels)
//   host.frame_end()          -> which TOPs the scripts read back (numpyArray);
//                                those are cooked/read now and delivered with the
//                                next frame — TD's numpyArray(delayed=True).
use glow::HasContext;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::time::Instant;

use crate::host::Host;
use crate::{prof_add, Chops, Prof};

// ---------------------------------------------------------------- 4x4 math
// Column-major (OpenGL layout): element (row r, col c) = m[c*4 + r].
pub type M4 = [f64; 16];

pub fn m4_ident() -> M4 {
    let mut m = [0.0; 16];
    m[0] = 1.0;
    m[5] = 1.0;
    m[10] = 1.0;
    m[15] = 1.0;
    m
}

pub fn m4_mul(a: &M4, b: &M4) -> M4 {
    let mut o = [0.0; 16];
    for c in 0..4 {
        for r in 0..4 {
            let mut s = 0.0;
            for k in 0..4 {
                s += a[k * 4 + r] * b[c * 4 + k];
            }
            o[c * 4 + r] = s;
        }
    }
    o
}

pub fn m4_inv(m: &M4) -> M4 {
    // Gauss-Jordan on the row-major view
    let mut a = [[0.0f64; 8]; 4];
    for r in 0..4 {
        for c in 0..4 {
            a[r][c] = m[c * 4 + r];
        }
        a[r][4 + r] = 1.0;
    }
    for i in 0..4 {
        let mut p = i;
        for r in i + 1..4 {
            if a[r][i].abs() > a[p][i].abs() {
                p = r;
            }
        }
        a.swap(i, p);
        let d = a[i][i];
        if d.abs() < 1e-300 {
            return m4_ident();
        }
        for c in 0..8 {
            a[i][c] /= d;
        }
        for r in 0..4 {
            if r != i {
                let f = a[r][i];
                if f != 0.0 {
                    for c in 0..8 {
                        a[r][c] -= f * a[i][c];
                    }
                }
            }
        }
    }
    let mut o = [0.0; 16];
    for r in 0..4 {
        for c in 0..4 {
            o[c * 4 + r] = a[r][4 + c];
        }
    }
    o
}

fn m4_f32(m: &M4) -> [f32; 16] {
    let mut o = [0f32; 16];
    for i in 0..16 {
        o[i] = m[i] as f32;
    }
    o
}

fn norm3(v: [f64; 3]) -> [f64; 3] {
    let n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
    if n < 1e-12 {
        v
    } else {
        [v[0] / n, v[1] / n, v[2] / n]
    }
}

fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
}

fn euler_xyz(rx: f64, ry: f64, rz: f64) -> M4 {
    let (sx, cx) = rx.to_radians().sin_cos();
    let (sy, cy) = ry.to_radians().sin_cos();
    let (sz, cz) = rz.to_radians().sin_cos();
    // R = Rz * Ry * Rx (rotate x first)
    let r = [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ];
    let mut m = m4_ident();
    for rr in 0..3 {
        for c in 0..3 {
            m[c * 4 + rr] = r[rr][c];
        }
    }
    m
}

// ---------------------------------------------------------------- GL objects
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Fmt {
    Rgba8,
    Rgba16f,
    Rgba32f,
    R32f,
}

impl Fmt {
    fn parse(s: &str) -> Fmt {
        match s {
            "rgba16f" => Fmt::Rgba16f,
            "rgba32f" => Fmt::Rgba32f,
            "r32f" | "r16f" => Fmt::R32f,
            _ => Fmt::Rgba8,
        }
    }
    fn gl(self) -> (u32, u32, u32) {
        match self {
            Fmt::Rgba8 => (glow::RGBA8, glow::RGBA, glow::UNSIGNED_BYTE),
            Fmt::Rgba16f => (glow::RGBA16F, glow::RGBA, glow::HALF_FLOAT),
            Fmt::Rgba32f => (glow::RGBA32F, glow::RGBA, glow::FLOAT),
            Fmt::R32f => (glow::R32F, glow::RED, glow::FLOAT),
        }
    }
}

#[derive(Clone, Copy)]
struct Tex {
    tex: glow::Texture,
    w: i32,
    h: i32,
    fmt: Fmt,
}

struct Target {
    fbo: glow::Framebuffer,
    colors: Vec<Tex>,
    // render targets: multisampled draw buffers resolved into `colors`
    msaa: Option<(
        glow::Framebuffer,
        Vec<glow::Renderbuffer>,
        glow::Renderbuffer,
    )>,
    depth_rb: Option<glow::Renderbuffer>,
    w: i32,
    h: i32,
}

struct MeshGL {
    vbos: Vec<(glow::Buffer, Vec<(u32, i32, i32, i32)>)>, // buffer, [(loc, comps, stride, offset)]
    ebo: glow::Buffer,
    count: i32,
    present: HashSet<u32>,
}

struct GeoGL {
    vao: glow::VertexArray,
    inst: glow::Buffer,
    mesh_key: String,
    bound_mesh_gen: u64,
}

unsafe fn new_tex(
    gl: &glow::Context,
    w: i32,
    h: i32,
    fmt: Fmt,
    data: Option<&[u8]>,
) -> glow::Texture {
    let t = gl.create_texture().unwrap();
    gl.bind_texture(glow::TEXTURE_2D, Some(t));
    for (k, v) in [
        (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE),
        (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE),
        (glow::TEXTURE_MIN_FILTER, glow::LINEAR),
        (glow::TEXTURE_MAG_FILTER, glow::LINEAR),
    ] {
        gl.tex_parameter_i32(glow::TEXTURE_2D, k, v as i32);
    }
    let (ifmt, f, ty) = fmt.gl();
    gl.tex_image_2d(glow::TEXTURE_2D, 0, ifmt as i32, w, h, 0, f, ty, data);
    t
}

fn link(gl: &glow::Context, vs: &str, fs: &str, name: &str) -> glow::Program {
    unsafe {
        let p = gl.create_program().unwrap();
        for (ty, src) in [(glow::VERTEX_SHADER, vs), (glow::FRAGMENT_SHADER, fs)] {
            let s = gl.create_shader(ty).unwrap();
            gl.shader_source(s, src);
            gl.compile_shader(s);
            if !gl.get_shader_compile_status(s) {
                panic!(
                    "[host] shader compile failed for {name}:\n{}",
                    gl.get_shader_info_log(s)
                );
            }
            gl.attach_shader(p, s);
        }
        gl.link_program(p);
        if !gl.get_program_link_status(p) {
            panic!(
                "[host] link failed for {name}:\n{}",
                gl.get_program_info_log(p)
            );
        }
        p
    }
}

const FULLSCREEN_VS: &str = "#version 330 core\nout vec3 vUV;\nvoid main(){ vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2)); vUV = vec3(p, 0.0); gl_Position = vec4(p*2.0-1.0, 0.0, 1.0); }\n";
const BLIT_FS: &str = "#version 330 core\nin vec3 vUV;\nout vec4 fragColor;\nuniform sampler2D tex;\nuniform vec2 uScale;\nvoid main(){ vec2 uv = (vUV.st-0.5)/uScale+0.5; if(uv.x<0.0||uv.x>1.0||uv.y<0.0||uv.y>1.0) fragColor=vec4(0,0,0,1); else fragColor=vec4(texture(tex, uv).rgb, 1.0); }\n";

// ---------------------------------------------------------------- renderer
struct PendingRead {
    path: String,
    w: i32,
    h: i32,
    pbo: glow::Buffer,
    cap: i32,
}

pub struct HostRenderer<'a> {
    pub gl: &'a glow::Context,
    dir: String,
    nodes: Vec<Value>,
    output: String,
    meshes_spec: Value,
    host: Host,
    floats: Vec<f64>,
    mats: Vec<f64>,
    assets: HashMap<String, Tex>,
    out: HashMap<String, Vec<Tex>>,
    targets: HashMap<String, Target>,
    progs: HashMap<String, glow::Program>,
    script_tex: HashMap<String, Tex>,
    meshes: HashMap<String, MeshGL>,
    mesh_gen: HashMap<String, u64>,
    geos: HashMap<String, GeoGL>,
    chops: HashMap<String, (usize, HashMap<String, Vec<f32>>)>,
    inv_bind: HashMap<String, Vec<M4>>,
    empty_vao: glow::VertexArray,
    scratch_fbo: glow::Framebuffer,
    blit: glow::Program,
    readback_req: Vec<String>,
    readback_data: Vec<(String, i32, i32, Vec<u8>)>,
    // Asynchronous readback (GL 3 / GLES 3): glReadPixels into pixel-pack
    // buffers at the end of a frame, mapped at the start of the next one — the
    // GPU finishes the frame (and the page-flip) in between, instead of the CPU
    // stalling on it mid-frame. Delivery is unchanged: numpyArray(delayed=True)
    // gets the previous frame's pixels either way.
    async_readback: bool,
    pending_reads: Vec<PendingRead>,
    pending_fence: Option<glow::Fence>,
    pbo_pool: Vec<(glow::Buffer, i32)>,
    // TOXC_PROFILE: glFinish after every node so each top:<node> includes its
    // GPU time (otherwise GPU work lands wherever the CPU next waits: "gpu")
    profile_gpu: bool,
    frame: u64,
    msaa_samples: i32,
    prof: Prof,
    store: Chops,
    pub quit: bool,
    warned: HashSet<String>,
}

fn jf(v: &Value) -> f64 {
    v.as_f64().unwrap_or(0.0)
}

impl<'a> HostRenderer<'a> {
    pub fn new(
        gl: &'a glow::Context,
        dir: &str,
        prof: Prof,
        store: Chops,
        monitors: Value,
    ) -> Self {
        let sched: Value = serde_json::from_reader(
            std::fs::File::open(format!("{dir}/schedule.json")).expect("schedule.json"),
        )
        .expect("parse schedule.json");
        let nodes = sched["nodes"].as_array().cloned().unwrap_or_default();
        let output = sched["output"].as_str().unwrap_or("").to_string();
        let host = Host::spawn(dir, json!({"monitors": monitors})).expect("start the Python host");
        let (empty_vao, scratch_fbo, blit, msaa_samples) = unsafe {
            let v = gl.create_vertex_array().unwrap();
            let f = gl.create_framebuffer().unwrap();
            let b = link(gl, FULLSCREEN_VS, BLIT_FS, "blit");
            let max = gl.get_parameter_i32(glow::MAX_SAMPLES);
            (v, f, b, max.min(4).max(0))
        };
        let mut r = HostRenderer {
            gl,
            dir: dir.to_string(),
            nodes,
            output,
            meshes_spec: sched["meshes"].clone(),
            host,
            floats: vec![],
            mats: vec![],
            assets: HashMap::new(),
            out: HashMap::new(),
            targets: HashMap::new(),
            progs: HashMap::new(),
            script_tex: HashMap::new(),
            meshes: HashMap::new(),
            mesh_gen: HashMap::new(),
            geos: HashMap::new(),
            chops: HashMap::new(),
            inv_bind: HashMap::new(),
            empty_vao,
            scratch_fbo,
            blit,
            readback_req: vec![],
            readback_data: vec![],
            async_readback: gl.version().major >= 3
                && std::env::var_os("TOXC_SYNC_READBACK").is_none(),
            pending_reads: vec![],
            pending_fence: None,
            pbo_pool: vec![],
            profile_gpu: std::env::var_os("TOXC_PROFILE").is_some(),
            frame: 0,
            msaa_samples,
            prof,
            store,
            quit: false,
            warned: HashSet::new(),
        };
        r.load_assets(&sched["textures"]);
        r.load_meshes();
        eprintln!(
            "[host] {} nodes, output {}, MSAA x{}",
            r.nodes.len(),
            r.output,
            r.msaa_samples
        );
        r
    }

    // ------------------------------------------------------------ setup
    fn load_assets(&mut self, textures: &Value) {
        if let Some(m) = textures.as_object() {
            for (id, spec) in m {
                let file = spec["file"].as_str().unwrap_or("");
                match image::open(format!("{}/{}", self.dir, file)) {
                    Ok(im) => {
                        let im = im.to_rgba8();
                        let (w, h) = (im.width() as i32, im.height() as i32);
                        let t = unsafe {
                            let t = new_tex(self.gl, w, h, Fmt::Rgba8, Some(&im.into_raw()));
                            self.gl.generate_mipmap(glow::TEXTURE_2D);
                            self.gl.tex_parameter_i32(
                                glow::TEXTURE_2D,
                                glow::TEXTURE_MIN_FILTER,
                                glow::LINEAR_MIPMAP_LINEAR as i32,
                            );
                            t
                        };
                        self.assets.insert(
                            id.clone(),
                            Tex {
                                tex: t,
                                w,
                                h,
                                fmt: Fmt::Rgba8,
                            },
                        );
                    }
                    Err(e) => eprintln!("[host] texture {file}: {e}"),
                }
            }
        }
    }

    fn load_meshes(&mut self) {
        let spec = self.meshes_spec.clone();
        if let Some(m) = spec.as_object() {
            for (id, s) in m {
                let vbytes =
                    std::fs::read(format!("{}/{}", self.dir, s["file"].as_str().unwrap())).unwrap();
                let ibytes =
                    std::fs::read(format!("{}/{}", self.dir, s["idx"].as_str().unwrap())).unwrap();
                let stride = s["stride"].as_i64().unwrap_or(32) as i32;
                let mut attrs = vec![];
                let mut off = 0i32;
                let mut present = HashSet::new();
                for l in s["layout"].as_array().unwrap() {
                    let (loc, n) = match l.as_str().unwrap() {
                        "pos3" => (0, 3),
                        "nrm3" => (1, 3),
                        "uv2" => (2, 2),
                        "tan4" => (3, 4),
                        "bidx4" => (4, 4),
                        "bwt4" => (5, 4),
                        "col4" => (6, 4),
                        _ => (99, 0),
                    };
                    if loc != 99 {
                        attrs.push((loc, n, stride, off));
                        present.insert(loc);
                    }
                    off += n * 4;
                }
                let (vb, eb) = unsafe {
                    let vb = self.gl.create_buffer().unwrap();
                    self.gl.bind_buffer(glow::ARRAY_BUFFER, Some(vb));
                    self.gl
                        .buffer_data_u8_slice(glow::ARRAY_BUFFER, &vbytes, glow::STATIC_DRAW);
                    let eb = self.gl.create_buffer().unwrap();
                    self.gl.bind_buffer(glow::ELEMENT_ARRAY_BUFFER, Some(eb));
                    self.gl.buffer_data_u8_slice(
                        glow::ELEMENT_ARRAY_BUFFER,
                        &ibytes,
                        glow::STATIC_DRAW,
                    );
                    (vb, eb)
                };
                self.meshes.insert(
                    format!("baked:{id}"),
                    MeshGL {
                        vbos: vec![(vb, attrs)],
                        ebo: eb,
                        count: s["count"].as_i64().unwrap_or(0) as i32,
                        present,
                    },
                );
                self.mesh_gen.insert(format!("baked:{id}"), 1);
                if let Some(ib) = s["skin"]["inv_bind"].as_array() {
                    let mats: Vec<M4> = ib
                        .iter()
                        .map(|m| {
                            let v: Vec<f64> = m.as_array().unwrap().iter().map(jf).collect();
                            let mut o = [0.0; 16];
                            o.copy_from_slice(&v[..16]);
                            o
                        })
                        .collect();
                    self.inv_bind.insert(id.clone(), mats);
                }
            }
        }
    }

    // ------------------------------------------------------------ values
    fn v(&self, v: &Value) -> f64 {
        match v {
            Value::Number(n) => n.as_f64().unwrap_or(0.0),
            Value::Bool(b) => {
                if *b {
                    1.0
                } else {
                    0.0
                }
            }
            Value::Array(a) if a.len() == 2 && a[0].as_str() == Some("b") => {
                let i = a[1].as_u64().unwrap_or(0) as usize;
                self.floats.get(i).copied().unwrap_or(0.0)
            }
            _ => 0.0,
        }
    }

    fn mat(&self, i: &Value) -> M4 {
        let i = i.as_u64().unwrap_or(u64::MAX) as usize;
        let mut m = m4_ident();
        if i < usize::MAX && (i + 1) * 16 <= self.mats.len() {
            m.copy_from_slice(&self.mats[i * 16..i * 16 + 16]);
        }
        m
    }

    fn size_of(&self, id: &str) -> Option<(i32, i32)> {
        self.out.get(id).and_then(|v| v.first()).map(|t| (t.w, t.h))
    }

    fn fmt_of(&self, id: &str) -> Fmt {
        self.out
            .get(id)
            .and_then(|v| v.first())
            .map(|t| t.fmt)
            .unwrap_or(Fmt::Rgba8)
    }

    fn node_size(&self, n: &Value) -> (i32, i32) {
        let rule = &n["size"];
        let input = n["inputs"]
            .as_array()
            .and_then(|a| a.first())
            .and_then(|v| v.as_str());
        let insz = input.and_then(|i| self.size_of(i)).unwrap_or((256, 256));
        let (w, h) = match rule["mode"].as_str().unwrap_or("input") {
            "custom" => (
                self.v(&rule["w"]).round() as i32,
                self.v(&rule["h"]).round() as i32,
            ),
            "fraction" => {
                let f = jf(&rule["f"]);
                (
                    ((insz.0 as f64) * f).round() as i32,
                    ((insz.1 as f64) * f).round() as i32,
                )
            }
            "input_flop" => {
                if self.v(&rule["flop"]) > 0.5 {
                    (insz.1, insz.0)
                } else {
                    insz
                }
            }
            "of" => rule["node"]
                .as_str()
                .and_then(|i| self.size_of(i))
                .unwrap_or((256, 256)),
            _ => insz,
        };
        (w.clamp(1, 16384), h.clamp(1, 16384))
    }

    fn node_fmt(&self, n: &Value) -> Fmt {
        match n["fmt"].as_str() {
            Some(s) => Fmt::parse(s),
            None => n["inputs"]
                .as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_str())
                .map(|i| self.fmt_of(i))
                .unwrap_or(Fmt::Rgba8),
        }
    }

    fn program(&mut self, vert: Option<&str>, frag: &str) -> glow::Program {
        let key = format!("{}|{}", vert.unwrap_or("<fs>"), frag);
        if let Some(p) = self.progs.get(&key) {
            return *p;
        }
        let fs = std::fs::read_to_string(format!("{}/{}", self.dir, frag)).expect(frag);
        let vs = match vert {
            Some(v) => std::fs::read_to_string(format!("{}/{}", self.dir, v)).expect(v),
            None => FULLSCREEN_VS.to_string(),
        };
        let p = link(self.gl, &vs, &fs, frag);
        self.progs.insert(key, p);
        p
    }

    /// (Re)allocate a node's render target: `n_colors` attachments of `fmt`
    /// (plus an R32F depth-colour attachment for Render TOPs that feed a Depth
    /// TOP), multisampled when `msaa`.
    fn ensure_target(&mut self, id: &str, w: i32, h: i32, fmts: &[Fmt], msaa: bool) {
        if let Some(t) = self.targets.get(id) {
            if t.w == w
                && t.h == h
                && t.colors.len() == fmts.len()
                && t.colors.iter().zip(fmts).all(|(c, f)| c.fmt == *f)
            {
                return;
            }
        }
        let gl = self.gl;
        unsafe {
            if let Some(old) = self.targets.remove(id) {
                gl.delete_framebuffer(old.fbo);
                for c in old.colors {
                    gl.delete_texture(c.tex);
                }
                if let Some((f, rbs, d)) = old.msaa {
                    gl.delete_framebuffer(f);
                    for rb in rbs {
                        gl.delete_renderbuffer(rb);
                    }
                    gl.delete_renderbuffer(d);
                }
                if let Some(d) = old.depth_rb {
                    gl.delete_renderbuffer(d);
                }
            }
            let fbo = gl.create_framebuffer().unwrap();
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            let mut colors = vec![];
            let mut bufs = vec![];
            for (k, f) in fmts.iter().enumerate() {
                let t = new_tex(gl, w, h, *f, None);
                gl.framebuffer_texture_2d(
                    glow::FRAMEBUFFER,
                    glow::COLOR_ATTACHMENT0 + k as u32,
                    glow::TEXTURE_2D,
                    Some(t),
                    0,
                );
                colors.push(Tex {
                    tex: t,
                    w,
                    h,
                    fmt: *f,
                });
                bufs.push(glow::COLOR_ATTACHMENT0 + k as u32);
            }
            gl.draw_buffers(&bufs);
            let mut depth_rb = None;
            let mut ms = None;
            if msaa && self.msaa_samples > 1 {
                let mf = gl.create_framebuffer().unwrap();
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(mf));
                let mut rbs = vec![];
                for (k, f) in fmts.iter().enumerate() {
                    let rb = gl.create_renderbuffer().unwrap();
                    gl.bind_renderbuffer(glow::RENDERBUFFER, Some(rb));
                    gl.renderbuffer_storage_multisample(
                        glow::RENDERBUFFER,
                        self.msaa_samples,
                        f.gl().0,
                        w,
                        h,
                    );
                    gl.framebuffer_renderbuffer(
                        glow::FRAMEBUFFER,
                        glow::COLOR_ATTACHMENT0 + k as u32,
                        glow::RENDERBUFFER,
                        Some(rb),
                    );
                    rbs.push(rb);
                }
                let d = gl.create_renderbuffer().unwrap();
                gl.bind_renderbuffer(glow::RENDERBUFFER, Some(d));
                gl.renderbuffer_storage_multisample(
                    glow::RENDERBUFFER,
                    self.msaa_samples,
                    glow::DEPTH_COMPONENT24,
                    w,
                    h,
                );
                gl.framebuffer_renderbuffer(
                    glow::FRAMEBUFFER,
                    glow::DEPTH_ATTACHMENT,
                    glow::RENDERBUFFER,
                    Some(d),
                );
                gl.draw_buffers(&bufs);
                ms = Some((mf, rbs, d));
            } else if msaa {
                let d = gl.create_renderbuffer().unwrap();
                gl.bind_renderbuffer(glow::RENDERBUFFER, Some(d));
                gl.renderbuffer_storage(glow::RENDERBUFFER, glow::DEPTH_COMPONENT24, w, h);
                gl.framebuffer_renderbuffer(
                    glow::FRAMEBUFFER,
                    glow::DEPTH_ATTACHMENT,
                    glow::RENDERBUFFER,
                    Some(d),
                );
                depth_rb = Some(d);
            }
            let st = gl.check_framebuffer_status(glow::FRAMEBUFFER);
            if st != glow::FRAMEBUFFER_COMPLETE {
                eprintln!("[host] {id}: framebuffer incomplete 0x{st:x}");
            }
            self.targets.insert(
                id.to_string(),
                Target {
                    fbo,
                    colors,
                    msaa: ms,
                    depth_rb,
                    w,
                    h,
                },
            );
        }
    }

    // ------------------------------------------------------------ frame
    pub fn cook(&mut self, t: f64) {
        self.frame += 1;
        let t0 = Instant::now();
        // last frame's asynchronous readbacks (normally complete by now)
        if !self.pending_reads.is_empty() {
            let tr = Instant::now();
            self.finish_readbacks();
            prof_add(&self.prof, "host:readback_wait", tr.elapsed().as_secs_f64());
        }
        // 1. the host: frame-start callbacks, script cooks, bindings
        let rb = std::mem::take(&mut self.readback_data);
        let specs: Vec<Value> = rb
            .iter()
            .map(|(p, w, h, _)| json!({"path": p, "w": w, "h": h}))
            .collect();
        let blobs: Vec<&[u8]> = rb.iter().map(|(_, _, _, d)| d.as_slice()).collect();
        let sizes: serde_json::Map<String, Value> = self
            .out
            .iter()
            .filter_map(|(k, v)| v.first().map(|t| (k.clone(), json!([t.w, t.h]))))
            .collect();
        let msg = match self.host.call(
            json!({"cmd": "frame", "t": t, "frame": self.frame, "readbacks": specs, "sizes": sizes}),
            &blobs,
        ) {
            Ok(m) => m,
            Err(e) => {
                eprintln!("[host] frame failed: {e} — the Python host exited");
                self.quit = true;
                return;
            }
        };
        if let Some(err) = msg.head.get("error") {
            eprintln!("[host] frame error: {err}");
        }
        self.floats = msg.f64s(&msg.head["floats"]);
        self.mats = msg.f64s(&msg.head["mats"]);
        if msg.head["quit"].as_bool() == Some(true) {
            self.quit = true;
        }
        // Script TOP pixels
        if let Some(tops) = msg.head["tops"].as_object() {
            for (path, spec) in tops {
                let (w, h) = (
                    spec["w"].as_i64().unwrap_or(1) as i32,
                    spec["h"].as_i64().unwrap_or(1) as i32,
                );
                let fmt = if spec["dtype"].as_str() == Some("f32") {
                    Fmt::Rgba32f
                } else {
                    Fmt::Rgba8
                };
                let data = msg.blob(&spec["blob"]).unwrap_or(&[]);
                unsafe {
                    let reuse = self
                        .script_tex
                        .get(path)
                        .filter(|t| t.w == w && t.h == h && t.fmt == fmt)
                        .copied();
                    let tex = match reuse {
                        Some(t) => {
                            self.gl.bind_texture(glow::TEXTURE_2D, Some(t.tex));
                            let (_, f, ty) = fmt.gl();
                            self.gl.tex_sub_image_2d(
                                glow::TEXTURE_2D,
                                0,
                                0,
                                0,
                                w,
                                h,
                                f,
                                ty,
                                glow::PixelUnpackData::Slice(data),
                            );
                            t
                        }
                        None => {
                            if let Some(old) = self.script_tex.remove(path) {
                                self.gl.delete_texture(old.tex);
                            }
                            let t = Tex {
                                tex: new_tex(self.gl, w, h, fmt, Some(data)),
                                w,
                                h,
                                fmt,
                            };
                            t
                        }
                    };
                    self.script_tex.insert(path.clone(), tex);
                }
            }
        }
        // CHOP channels (instancing)
        if let Some(chops) = msg.head["chops"].as_object() {
            for (path, spec) in chops {
                let n = spec["n"].as_u64().unwrap_or(1) as usize;
                let mut chans = HashMap::new();
                if let Some(cm) = spec["chans"].as_object() {
                    for (c, b) in cm {
                        chans.insert(c.clone(), msg.f32s(b));
                    }
                }
                self.chops.insert(path.clone(), (n, chans));
            }
        }
        // Script SOP meshes
        if let Some(sops) = msg.head["sops"].as_object() {
            for (path, spec) in sops {
                self.upload_sop(path, spec, &msg);
            }
        }
        prof_add(&self.prof, "host:frame", t0.elapsed().as_secs_f64());

        // 2. cook the graph
        let needed = self.on_demand_needed();
        let nodes = self.nodes.clone();
        for n in &nodes {
            if n["on_demand"].as_bool() == Some(true)
                && !needed.contains(n["id"].as_str().unwrap_or(""))
            {
                continue;
            }
            let ts = Instant::now();
            self.cook_node(n);
            if self.profile_gpu {
                unsafe { self.gl.finish() };
            }
            prof_add(
                &self.prof,
                &format!("top:{}", n["id"].as_str().unwrap_or("?")),
                ts.elapsed().as_secs_f64(),
            );
        }

        // 3. frame-end callbacks; read back what the scripts asked for
        let te = Instant::now();
        match self.host.call(json!({"cmd": "frame_end"}), &[]) {
            Ok(m) => {
                self.readback_req = m.head["readback"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_default();
            }
            Err(e) => {
                eprintln!("[host] frame_end failed: {e}");
                self.quit = true;
            }
        }
        prof_add(&self.prof, "host:frame_end", te.elapsed().as_secs_f64());
        if !self.readback_req.is_empty() {
            let tr = Instant::now();
            // on-demand nodes the scripts want this frame (TD cooks on request)
            let needed = self.on_demand_needed();
            for n in &nodes {
                let id = n["id"].as_str().unwrap_or("");
                if n["on_demand"].as_bool() == Some(true) && needed.contains(id) {
                    let ts = Instant::now();
                    self.cook_node(n);
                    if self.profile_gpu {
                        unsafe { self.gl.finish() };
                    }
                    prof_add(&self.prof, &format!("top:{id}"), ts.elapsed().as_secs_f64());
                }
            }
            let req = self.readback_req.clone();
            for p in req {
                if let Some(tex) = self.out.get(&p).and_then(|v| v.first()).copied() {
                    if self.async_readback {
                        self.start_readback(p, tex);
                    } else {
                        let data = self.read_tex_f32(tex);
                        self.readback_data.push((p, tex.w, tex.h, data));
                    }
                }
            }
            if !self.pending_reads.is_empty() {
                unsafe {
                    self.pending_fence =
                        self.gl.fence_sync(glow::SYNC_GPU_COMMANDS_COMPLETE, 0).ok();
                    self.gl.flush();
                }
            }
            prof_add(&self.prof, "host:readback", tr.elapsed().as_secs_f64());
        }
    }

    fn on_demand_needed(&self) -> HashSet<String> {
        let mut need: HashSet<String> = self.readback_req.iter().cloned().collect();
        let by: HashMap<&str, &Value> = self
            .nodes
            .iter()
            .filter_map(|n| n["id"].as_str().map(|i| (i, n)))
            .collect();
        let mut stack: Vec<String> = need.iter().cloned().collect();
        while let Some(id) = stack.pop() {
            if let Some(n) = by.get(id.as_str()) {
                let mut deps: Vec<String> = n["inputs"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_default();
                if let Some(o) = n["of"].as_str() {
                    deps.push(o.to_string());
                }
                for d in deps {
                    if need.insert(d.clone()) {
                        stack.push(d);
                    }
                }
            }
        }
        need
    }

    /// Queue a float RGBA read of `t` into a pixel-pack buffer (no CPU wait).
    fn start_readback(&mut self, path: String, t: Tex) {
        let need = t.w * t.h * 16;
        let gl = self.gl;
        unsafe {
            let (pbo, cap) = match self.pbo_pool.iter().position(|&(_, c)| c >= need) {
                Some(i) => self.pbo_pool.swap_remove(i),
                None => (gl.create_buffer().unwrap(), 0),
            };
            gl.bind_buffer(glow::PIXEL_PACK_BUFFER, Some(pbo));
            let cap = if cap < need {
                gl.buffer_data_size(glow::PIXEL_PACK_BUFFER, need, glow::STREAM_READ);
                need
            } else {
                cap
            };
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.scratch_fbo));
            gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER,
                glow::COLOR_ATTACHMENT0,
                glow::TEXTURE_2D,
                Some(t.tex),
                0,
            );
            gl.read_buffer(glow::COLOR_ATTACHMENT0);
            gl.read_pixels(
                0,
                0,
                t.w,
                t.h,
                glow::RGBA,
                glow::FLOAT,
                glow::PixelPackData::BufferOffset(0),
            );
            gl.bind_buffer(glow::PIXEL_PACK_BUFFER, None);
            self.pending_reads.push(PendingRead {
                path,
                w: t.w,
                h: t.h,
                pbo,
                cap,
            });
        }
    }

    /// Wait for the queued reads and hand their pixels to the next host frame.
    fn finish_readbacks(&mut self) {
        let gl = self.gl;
        unsafe {
            if let Some(f) = self.pending_fence.take() {
                // flush + wait up to 1 s; the frame's GPU work is normally long done
                gl.client_wait_sync(f, glow::SYNC_FLUSH_COMMANDS_BIT, 1_000_000_000);
                gl.delete_sync(f);
            }
            for r in std::mem::take(&mut self.pending_reads) {
                let n = (r.w * r.h * 16) as usize;
                gl.bind_buffer(glow::PIXEL_PACK_BUFFER, Some(r.pbo));
                let ptr =
                    gl.map_buffer_range(glow::PIXEL_PACK_BUFFER, 0, n as i32, glow::MAP_READ_BIT);
                if !ptr.is_null() {
                    let data = std::slice::from_raw_parts(ptr as *const u8, n).to_vec();
                    gl.unmap_buffer(glow::PIXEL_PACK_BUFFER);
                    self.readback_data.push((r.path, r.w, r.h, data));
                }
                gl.bind_buffer(glow::PIXEL_PACK_BUFFER, None);
                self.pbo_pool.push((r.pbo, r.cap));
            }
        }
    }

    fn read_tex_f32(&self, t: Tex) -> Vec<u8> {
        let mut buf = vec![0u8; (t.w * t.h * 16) as usize];
        unsafe {
            self.gl
                .bind_framebuffer(glow::FRAMEBUFFER, Some(self.scratch_fbo));
            self.gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER,
                glow::COLOR_ATTACHMENT0,
                glow::TEXTURE_2D,
                Some(t.tex),
                0,
            );
            self.gl.read_buffer(glow::COLOR_ATTACHMENT0);
            self.gl.read_pixels(
                0,
                0,
                t.w,
                t.h,
                glow::RGBA,
                glow::FLOAT,
                glow::PixelPackData::Slice(&mut buf),
            );
        }
        buf
    }

    fn upload_sop(&mut self, path: &str, spec: &Value, msg: &crate::host::Msg) {
        let gl = self.gl;
        let key = format!("sop:{path}");
        unsafe {
            if let Some(old) = self.meshes.remove(&key) {
                for (b, _) in old.vbos {
                    gl.delete_buffer(b);
                }
                gl.delete_buffer(old.ebo);
            }
            let mut vbos = vec![];
            let mut present = HashSet::new();
            for (name, loc, n) in [
                ("pos", 0u32, 3i32),
                ("nrm", 1, 3),
                ("uv", 2, 2),
                ("col", 6, 4),
            ] {
                if let Some(bytes) = msg.blob(&spec[name]) {
                    let b = gl.create_buffer().unwrap();
                    gl.bind_buffer(glow::ARRAY_BUFFER, Some(b));
                    gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
                    vbos.push((b, vec![(loc, n, n * 4, 0)]));
                    present.insert(loc);
                }
            }
            let eb = gl.create_buffer().unwrap();
            gl.bind_buffer(glow::ELEMENT_ARRAY_BUFFER, Some(eb));
            gl.buffer_data_u8_slice(
                glow::ELEMENT_ARRAY_BUFFER,
                msg.blob(&spec["idx"]).unwrap_or(&[]),
                glow::STATIC_DRAW,
            );
            let count = spec["count"].as_i64().unwrap_or(0) as i32;
            self.meshes.insert(
                key.clone(),
                MeshGL {
                    vbos,
                    ebo: eb,
                    count,
                    present,
                },
            );
            *self.mesh_gen.entry(key).or_insert(0) += 1;
        }
    }

    fn warn_once(&mut self, key: String, msg: String) {
        if self.warned.insert(key) {
            eprintln!("{msg}");
        }
    }

    // ------------------------------------------------------------ nodes
    fn cook_node(&mut self, n: &Value) {
        let id = n["id"].as_str().unwrap_or("").to_string();
        let kind = n["kind"].as_str().unwrap_or("");
        match kind {
            "alias" => {
                if let Some(src) = n["of"].as_str() {
                    if let Some(v) = self.out.get(src).cloned() {
                        self.out.insert(id, v);
                    }
                }
            }
            "renderselect" => {
                let k = n["buffer"].as_u64().unwrap_or(0) as usize;
                if let Some(v) = n["of"].as_str().and_then(|s| self.out.get(s)) {
                    if let Some(t) = v.get(k).copied() {
                        self.out.insert(id, vec![t]);
                    }
                }
            }
            "depth" => {
                let depth = n["of"]
                    .as_str()
                    .and_then(|s| self.targets.get(s))
                    .and_then(|t| t.colors.get(1).copied());
                match depth {
                    Some(t) => {
                        self.out.insert(id, vec![t]);
                    }
                    None => self.warn_once(
                        id.clone(),
                        format!("[host] {id}: render has no depth output"),
                    ),
                }
            }
            "script" => {
                let path = n["op"].as_str().unwrap_or("");
                let t = match self.script_tex.get(path) {
                    Some(t) => *t,
                    None => {
                        let t = Tex {
                            tex: unsafe { new_tex(self.gl, 1, 1, Fmt::Rgba8, Some(&[0, 0, 0, 0])) },
                            w: 1,
                            h: 1,
                            fmt: Fmt::Rgba8,
                        };
                        self.script_tex.insert(path.to_string(), t);
                        t
                    }
                };
                self.out.insert(id, vec![t]);
            }
            "image" => {
                let tex = n["texture"]
                    .as_str()
                    .and_then(|t| self.assets.get(t))
                    .copied();
                let t = match tex {
                    Some(t) => t,
                    None => {
                        let tc = crate::testcard(256, 256);
                        let t = Tex {
                            tex: unsafe { new_tex(self.gl, 256, 256, Fmt::Rgba8, Some(&tc)) },
                            w: 256,
                            h: 256,
                            fmt: Fmt::Rgba8,
                        };
                        if let Some(k) = n["texture"].as_str() {
                            self.assets.insert(k.to_string(), t);
                        }
                        t
                    }
                };
                self.out.insert(id, vec![t]);
            }
            "blank" => {
                let (w, h) = self.node_size(n);
                self.ensure_target(&id, w, h, &[Fmt::Rgba8], false);
                let t = self.targets[&id].colors[0];
                unsafe {
                    self.gl
                        .bind_framebuffer(glow::FRAMEBUFFER, Some(self.targets[&id].fbo));
                    self.gl.clear_color(0.0, 0.0, 0.0, 0.0);
                    self.gl.clear(glow::COLOR_BUFFER_BIT);
                }
                self.out.insert(id, vec![t]);
            }
            "glsl" | "blur" | "fit" | "flip" | "resolution" | "composite" | "level" => {
                self.cook_pass(n)
            }
            "render" => self.cook_render(n),
            "feedback" => {
                // echo the target's previous frame (copied at the end of the frame)
                if let Some(v) = n["of"].as_str().and_then(|s| self.out.get(s)).cloned() {
                    self.out.insert(id, v);
                }
            }
            other => self.warn_once(
                format!("kind:{other}"),
                format!("[host] node kind {other:?} not implemented"),
            ),
        }
    }

    fn cook_pass(&mut self, n: &Value) {
        let id = n["id"].as_str().unwrap_or("").to_string();
        let kind = n["kind"].as_str().unwrap_or("");
        let inputs: Vec<String> = n["inputs"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        let (w, h) = self.node_size(n);
        let fmt = self.node_fmt(n);
        let nout = n["outputs"].as_u64().unwrap_or(1).max(1) as usize;
        let fmts = vec![fmt; nout];
        self.ensure_target(&id, w, h, &fmts, false);
        let prog = self.program(None, n["frag"].as_str().unwrap_or(""));
        let gl = self.gl;
        let in_tex: Vec<Option<Tex>> = inputs
            .iter()
            .map(|i| self.out.get(i).and_then(|v| v.first()).copied())
            .collect();
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.targets[&id].fbo));
            gl.viewport(0, 0, w, h);
            gl.disable(glow::DEPTH_TEST);
            gl.disable(glow::BLEND);
            gl.use_program(Some(prog));
            gl.bind_vertex_array(Some(self.empty_vao));
            for (k, t) in in_tex.iter().enumerate() {
                gl.active_texture(glow::TEXTURE0 + k as u32);
                gl.bind_texture(glow::TEXTURE_2D, t.map(|t| t.tex));
            }
            let units: Vec<i32> = (0..in_tex.len().max(1) as i32).collect();
            for name in ["sTD2DInputs[0]", "tex[0]"] {
                if let Some(l) = gl.get_uniform_location(prog, name) {
                    gl.uniform_1_i32_slice(Some(&l), &units[..in_tex.len().max(1)]);
                }
            }
            if let Some(l) = gl.get_uniform_location(prog, "tex0") {
                gl.uniform_1_i32(Some(&l), 0);
            }
            for (k, t) in in_tex.iter().enumerate() {
                if let (Some(t), Some(l)) = (
                    t,
                    gl.get_uniform_location(prog, &format!("uTD2DInfos[{k}].res")),
                ) {
                    gl.uniform_4_f32(
                        Some(&l),
                        1.0 / t.w as f32,
                        1.0 / t.h as f32,
                        t.w as f32,
                        t.h as f32,
                    );
                }
            }
            if let Some(l) = gl.get_uniform_location(prog, "uTDOutputInfo.res") {
                gl.uniform_4_f32(Some(&l), 1.0 / w as f32, 1.0 / h as f32, w as f32, h as f32);
            }
            let (iw, ih) = in_tex
                .first()
                .and_then(|t| *t)
                .map(|t| (t.w as f32, t.h as f32))
                .unwrap_or((w as f32, h as f32));
            for (name, val) in [
                ("uIn", [iw, ih]),
                ("uOut", [w as f32, h as f32]),
                ("uTexel", [1.0 / iw, 1.0 / ih]),
            ] {
                if let Some(l) = gl.get_uniform_location(prog, name) {
                    gl.uniform_2_f32(Some(&l), val[0], val[1]);
                }
            }
            if kind == "fit" {
                if let Some(l) = gl.get_uniform_location(prog, "uMode") {
                    gl.uniform_1_i32(Some(&l), n["mode"].as_i64().unwrap_or(3) as i32);
                }
            }
            if let Some(u) = n["uniforms"].as_object() {
                for (name, val) in u {
                    let l = match gl.get_uniform_location(prog, name) {
                        Some(l) => l,
                        None => continue,
                    };
                    match val.as_array() {
                        Some(a) if a.len() == 4 || (a.len() == 3 && a[0].as_str() != Some("b")) => {
                            let c: Vec<f32> = a.iter().map(|x| self.v(x) as f32).collect();
                            if c.len() == 4 {
                                gl.uniform_4_f32(Some(&l), c[0], c[1], c[2], c[3]);
                            } else {
                                gl.uniform_3_f32(Some(&l), c[0], c[1], c[2]);
                            }
                        }
                        _ => gl.uniform_1_f32(Some(&l), self.v(val) as f32),
                    }
                }
            }
            gl.draw_arrays(glow::TRIANGLES, 0, 3);
        }
        let colors = self.targets[&id].colors.clone();
        self.out.insert(id, colors);
    }

    // ------------------------------------------------------------ render
    fn cook_render(&mut self, n: &Value) {
        let id = n["id"].as_str().unwrap_or("").to_string();
        let (w, h) = self.node_size(n);
        let fmt = self.node_fmt(n);
        let depth = n["depth"].as_bool() == Some(true);
        let mut fmts = vec![fmt];
        if depth {
            fmts.push(Fmt::R32f);
        }
        self.ensure_target(&id, w, h, &fmts, true);
        let gl = self.gl;
        let (draw_fbo, resolve) = {
            let t = &self.targets[&id];
            match &t.msaa {
                Some((mf, _, _)) => (*mf, true),
                None => (t.fbo, false),
            }
        };
        // camera
        let (view, proj, campos) = {
            let cam = &n["camera"];
            let cw = if cam.is_null() {
                let mut m = m4_ident();
                m[14] = 5.0;
                m
            } else {
                self.mat(&cam["mat"])
            };
            let fov = if cam.is_null() {
                45.0
            } else {
                self.v(&cam["fov"])
            };
            let near = if cam.is_null() {
                0.1
            } else {
                self.v(&cam["near"])
            };
            let far = if cam.is_null() {
                1000.0
            } else {
                self.v(&cam["far"])
            };
            let tx = (fov.to_radians() * 0.5).tan();
            let ty = tx * h as f64 / w as f64;
            let mut p = [0.0; 16];
            p[0] = 1.0 / tx;
            p[5] = 1.0 / ty;
            p[10] = -(far + near) / (far - near);
            p[11] = -1.0;
            p[14] = -2.0 * far * near / (far - near);
            (m4_inv(&cw), p, [cw[12], cw[13], cw[14]])
        };
        // lights
        let mut lpos = vec![];
        let mut lcol = vec![];
        let mut latt = vec![];
        let mut amb = [0.0f32; 3];
        if let Some(ls) = n["lights"].as_array() {
            for l in ls {
                let d = self.v(&l["dimmer"]);
                let c: Vec<f64> = l["color"]
                    .as_array()
                    .map(|a| a.iter().map(|x| self.v(x)).collect())
                    .unwrap_or(vec![1.0; 3]);
                if l["kind"].as_str() == Some("ambient") {
                    for k in 0..3 {
                        amb[k] += (c[k] * d) as f32;
                    }
                } else if lpos.len() < 8 * 3 {
                    let m = self.mat(&l["mat"]);
                    lpos.extend_from_slice(&[m[12] as f32, m[13] as f32, m[14] as f32]);
                    lcol.extend_from_slice(&[
                        (c[0] * d) as f32,
                        (c[1] * d) as f32,
                        (c[2] * d) as f32,
                    ]);
                    let a: Vec<f64> = l["atten"]
                        .as_array()
                        .map(|a| a.iter().map(|x| self.v(x)).collect())
                        .unwrap_or(vec![0.0; 4]);
                    latt.extend_from_slice(&[a[0] as f32, a[1] as f32, a[2] as f32, a[3] as f32]);
                }
            }
        }
        let bg: Vec<f32> = n["bg"]
            .as_array()
            .map(|a| a.iter().map(|x| self.v(x) as f32).collect())
            .unwrap_or(vec![0.0; 4]);
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(draw_fbo));
            gl.viewport(0, 0, w, h);
            gl.clear_buffer_f32_slice(glow::COLOR, 0, &[bg[0], bg[1], bg[2], bg[3]]);
            if depth {
                gl.clear_buffer_f32_slice(glow::COLOR, 1, &[0.0, 0.0, 0.0, 0.0]);
            }
            gl.clear(glow::DEPTH_BUFFER_BIT);
            gl.enable(glow::DEPTH_TEST);
            gl.depth_func(glow::LESS);
            gl.depth_mask(true);
            gl.disable(glow::BLEND);
            gl.disable(glow::CULL_FACE);
        }
        let geos = n["geos"].as_array().cloned().unwrap_or_default();
        for (gi, g) in geos.iter().enumerate() {
            if self.v(&g["render"]) < 0.5 {
                continue;
            }
            let key = format!("{id}#{gi}");
            self.draw_geo(&key, g, &view, &proj, campos, &lpos, &lcol, &latt, amb);
        }
        unsafe {
            gl.disable(glow::DEPTH_TEST);
            if resolve {
                let t = &self.targets[&id];
                gl.bind_framebuffer(glow::READ_FRAMEBUFFER, Some(draw_fbo));
                gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, Some(t.fbo));
                for k in 0..t.colors.len() as u32 {
                    gl.read_buffer(glow::COLOR_ATTACHMENT0 + k);
                    let mut db = vec![glow::NONE; t.colors.len()];
                    db[k as usize] = glow::COLOR_ATTACHMENT0 + k;
                    gl.draw_buffers(&db);
                    gl.blit_framebuffer(
                        0,
                        0,
                        w,
                        h,
                        0,
                        0,
                        w,
                        h,
                        glow::COLOR_BUFFER_BIT,
                        glow::NEAREST,
                    );
                }
                let all: Vec<u32> = (0..t.colors.len() as u32)
                    .map(|k| glow::COLOR_ATTACHMENT0 + k)
                    .collect();
                gl.draw_buffers(&all);
            }
        }
        let colors = self.targets[&id].colors.clone();
        self.out.insert(id, colors);
    }

    #[allow(clippy::too_many_arguments)]
    fn draw_geo(
        &mut self,
        key: &str,
        g: &Value,
        view: &M4,
        proj: &M4,
        campos: [f64; 3],
        lpos: &[f32],
        lcol: &[f32],
        latt: &[f32],
        amb: [f32; 3],
    ) {
        let gl = self.gl;
        let mesh_key = if let Some(b) = g["mesh"]["baked"].as_str() {
            format!("baked:{b}")
        } else if let Some(s) = g["mesh"]["sop"].as_str() {
            format!("sop:{s}")
        } else {
            return;
        };
        if !self.meshes.contains_key(&mesh_key) {
            return; // a Script SOP that has not cooked yet
        }
        let prog = self.program(g["vert"].as_str(), g["frag"].as_str().unwrap_or(""));
        let gen = *self.mesh_gen.get(&mesh_key).unwrap_or(&0);
        // (re)build the VAO when the mesh changed
        let need_vao = match self.geos.get(key) {
            Some(gg) => gg.mesh_key != mesh_key || gg.bound_mesh_gen != gen,
            None => true,
        };
        if need_vao {
            unsafe {
                let (vao, inst) = match self.geos.remove(key) {
                    Some(old) => (old.vao, old.inst),
                    None => (
                        gl.create_vertex_array().unwrap(),
                        gl.create_buffer().unwrap(),
                    ),
                };
                gl.bind_vertex_array(Some(vao));
                let mesh = &self.meshes[&mesh_key];
                for loc in 0..12u32 {
                    gl.disable_vertex_attrib_array(loc);
                }
                for (buf, attrs) in &mesh.vbos {
                    gl.bind_buffer(glow::ARRAY_BUFFER, Some(*buf));
                    for (loc, comps, stride, off) in attrs {
                        gl.enable_vertex_attrib_array(*loc);
                        gl.vertex_attrib_pointer_f32(
                            *loc,
                            *comps,
                            glow::FLOAT,
                            false,
                            *stride,
                            *off,
                        );
                        gl.vertex_attrib_divisor(*loc, 0);
                    }
                }
                gl.bind_buffer(glow::ELEMENT_ARRAY_BUFFER, Some(mesh.ebo));
                // instance attributes: mat4 columns (7..10) + colour (11)
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(inst));
                for k in 0..5u32 {
                    gl.vertex_attrib_pointer_f32(7 + k, 4, glow::FLOAT, false, 80, (k * 16) as i32);
                    gl.vertex_attrib_divisor(7 + k, 1);
                }
                self.geos.insert(
                    key.to_string(),
                    GeoGL {
                        vao,
                        inst,
                        mesh_key: mesh_key.clone(),
                        bound_mesh_gen: gen,
                    },
                );
            }
        }
        let gg = &self.geos[key];
        let mesh = &self.meshes[&mesh_key];
        let present = mesh.present.clone();
        let (vao, inst_buf, count) = (gg.vao, gg.inst, mesh.count);

        // model / skinning
        let geo_w = self.mat(&g["mat"]);
        let mut model = geo_w;
        if let Some(st) = g["mesh"]["static"].as_array() {
            let mut s = [0.0; 16];
            for (i, v) in st.iter().take(16).enumerate() {
                s[i] = jf(v);
            }
            model = m4_mul(&geo_w, &s);
        }
        let mut bones: Vec<f32> = vec![];
        if !g["skin"].is_null() {
            let mid = g["mesh"]["baked"].as_str().unwrap_or("");
            let root = self.mat(&g["skin"]["root_mat"]);
            let root_inv = m4_inv(&root);
            let tail = m4_mul(&root_inv, &geo_w);
            if let (Some(bm), Some(ib)) =
                (g["skin"]["bone_mats"].as_array(), self.inv_bind.get(mid))
            {
                for (k, bi) in bm.iter().enumerate() {
                    let bw = self.mat(bi);
                    let m = m4_mul(&m4_mul(&bw, &ib[k]), &tail);
                    bones.extend_from_slice(&m4_f32(&m));
                }
            }
        }
        // instancing
        let mut instances = 1i32;
        let instanced = !g["inst"].is_null();
        if instanced {
            let data = self.instance_data(&g["inst"]);
            instances = (data.len() / 20) as i32;
            if instances == 0 {
                return;
            }
            unsafe {
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(inst_buf));
                let bytes = std::slice::from_raw_parts(data.as_ptr() as *const u8, data.len() * 4);
                gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STREAM_DRAW);
            }
        }
        let mat = &g["material"];
        let c3 = |this: &Self, v: &Value| -> [f32; 3] {
            let a: Vec<f32> = v
                .as_array()
                .map(|a| a.iter().map(|x| this.v(x) as f32).collect())
                .unwrap_or(vec![0.0; 3]);
            [a[0], a[1], a[2]]
        };
        let diff = c3(self, &mat["diff"]);
        let ambc = c3(self, &mat["amb"]);
        let spec = c3(self, &mat["spec"]);
        let emit = c3(self, &mat["emit"]);
        let cnst = c3(self, &mat["const"]);
        let shin = self.v(&mat["shininess"]) as f32;
        let alpha = self.v(&mat["alpha"]) as f32;
        let athr = self.v(&mat["alphathreshold"]) as f32;
        let bump = self.v(&mat["bump"]) as f32;
        let mut maps: Vec<(&str, Option<Tex>, bool)> = vec![];
        for (slot, uname) in [
            ("diffuse", "sDiffuse"),
            ("normal", "sNormal"),
            ("color", "sColor"),
            ("alpha", "sAlpha"),
        ] {
            let m = &mat["maps"][slot];
            if let Some(top) = m["top"].as_str() {
                let nearest = m["filter"].as_str() == Some("nearest");
                maps.push((
                    uname,
                    self.out.get(top).and_then(|v| v.first()).copied(),
                    nearest,
                ));
            }
        }
        unsafe {
            gl.use_program(Some(prog));
            gl.bind_vertex_array(Some(vao));
            // constant attributes for anything the mesh does not carry
            if !present.contains(&3) {
                gl.vertex_attrib_4_f32(3, 1.0, 0.0, 0.0, 1.0);
            }
            if !present.contains(&4) {
                gl.vertex_attrib_4_f32(4, 0.0, 0.0, 0.0, 0.0);
            }
            if !present.contains(&5) {
                gl.vertex_attrib_4_f32(5, 1.0, 0.0, 0.0, 0.0);
            }
            if !present.contains(&6) {
                gl.vertex_attrib_4_f32(6, 1.0, 1.0, 1.0, 1.0);
            }
            if !present.contains(&2) {
                gl.vertex_attrib_4_f32(2, 0.0, 0.0, 0.0, 0.0);
            }
            if !present.contains(&1) {
                gl.vertex_attrib_4_f32(1, 0.0, 0.0, 1.0, 0.0);
            }
            for k in 0..5u32 {
                if instanced {
                    gl.enable_vertex_attrib_array(7 + k);
                } else {
                    gl.disable_vertex_attrib_array(7 + k);
                }
            }
            if !instanced {
                gl.vertex_attrib_4_f32(7, 1.0, 0.0, 0.0, 0.0);
                gl.vertex_attrib_4_f32(8, 0.0, 1.0, 0.0, 0.0);
                gl.vertex_attrib_4_f32(9, 0.0, 0.0, 1.0, 0.0);
                gl.vertex_attrib_4_f32(10, 0.0, 0.0, 0.0, 1.0);
                gl.vertex_attrib_4_f32(11, 1.0, 1.0, 1.0, 1.0);
            }
            let set_m4 = |name: &str, m: &M4| {
                if let Some(l) = gl.get_uniform_location(prog, name) {
                    gl.uniform_matrix_4_f32_slice(Some(&l), false, &m4_f32(m));
                }
            };
            set_m4("uModel", &model);
            set_m4("uView", view);
            set_m4("uProj", proj);
            if !bones.is_empty() {
                if let Some(l) = gl.get_uniform_location(prog, "uBones[0]") {
                    gl.uniform_matrix_4_f32_slice(Some(&l), false, &bones);
                }
            }
            let s3 = |name: &str, v: [f32; 3]| {
                if let Some(l) = gl.get_uniform_location(prog, name) {
                    gl.uniform_3_f32(Some(&l), v[0], v[1], v[2]);
                }
            };
            let s1 = |name: &str, v: f32| {
                if let Some(l) = gl.get_uniform_location(prog, name) {
                    gl.uniform_1_f32(Some(&l), v);
                }
            };
            s3(
                "uCamPos",
                [campos[0] as f32, campos[1] as f32, campos[2] as f32],
            );
            if let Some(l) = gl.get_uniform_location(prog, "uNumLights") {
                gl.uniform_1_i32(Some(&l), (lpos.len() / 3) as i32);
            }
            if !lpos.is_empty() {
                if let Some(l) = gl.get_uniform_location(prog, "uLightPos[0]") {
                    gl.uniform_3_f32_slice(Some(&l), lpos);
                }
                if let Some(l) = gl.get_uniform_location(prog, "uLightCol[0]") {
                    gl.uniform_3_f32_slice(Some(&l), lcol);
                }
                if let Some(l) = gl.get_uniform_location(prog, "uLightAtten[0]") {
                    gl.uniform_4_f32_slice(Some(&l), latt);
                }
            }
            s3("uAmbient", amb);
            s3("uDiff", diff);
            s3("uAmb", ambc);
            s3("uSpec", spec);
            s3("uEmit", emit);
            s3("uConst", cnst);
            s1("uShininess", shin);
            s1("uAlphaFront", alpha);
            s1("uAlphaThreshold", athr);
            s1("uBumpScale", bump);
            for (unit, (uname, tex, nearest)) in maps.iter().enumerate() {
                gl.active_texture(glow::TEXTURE0 + unit as u32);
                gl.bind_texture(glow::TEXTURE_2D, tex.map(|t| t.tex));
                if let Some(t) = tex {
                    if *nearest {
                        gl.tex_parameter_i32(
                            glow::TEXTURE_2D,
                            glow::TEXTURE_MIN_FILTER,
                            glow::NEAREST as i32,
                        );
                        gl.tex_parameter_i32(
                            glow::TEXTURE_2D,
                            glow::TEXTURE_MAG_FILTER,
                            glow::NEAREST as i32,
                        );
                    }
                    let _ = t;
                }
                if let Some(l) = gl.get_uniform_location(prog, uname) {
                    gl.uniform_1_i32(Some(&l), unit as i32);
                }
            }
            if instanced {
                gl.draw_elements_instanced(
                    glow::TRIANGLES,
                    count,
                    glow::UNSIGNED_INT,
                    0,
                    instances,
                );
            } else {
                gl.draw_elements(glow::TRIANGLES, count, glow::UNSIGNED_INT, 0);
            }
        }
    }

    /// Per-instance [mat4 (column-major) | rgba] from the CHOP channels, TD's
    /// instancing semantics: T * RotateTo(forward -> dir, up) * R(xyz) * S;
    /// instances whose Active channel is <= 0 are skipped.
    fn instance_data(&self, spec: &Value) -> Vec<f32> {
        let chop = spec["chop"].as_str().unwrap_or("");
        let (n, chans) = match self.chops.get(chop) {
            Some(c) => (c.0, &c.1),
            None => return vec![],
        };
        let get = |name: &Value, i: usize, d: f64| -> f64 {
            name.as_str()
                .and_then(|c| chans.get(c))
                .and_then(|v| v.get(i))
                .map(|x| *x as f64)
                .unwrap_or(d)
        };
        let has = |v: &Value| {
            v.as_array()
                .map(|a| {
                    a.iter()
                        .any(|x| x.as_str().map(|c| chans.contains_key(c)).unwrap_or(false))
                })
                .unwrap_or(false)
        };
        let use_rotto = has(&spec["rotto"]);
        let use_r = has(&spec["r"]);
        let use_col = has(&spec["color"]);
        let mut out = Vec::with_capacity(n * 20);
        for i in 0..n {
            if !spec["active"].is_null() && get(&spec["active"], i, 1.0) <= 0.0 {
                continue;
            }
            let t = [
                get(&spec["t"][0], i, 0.0),
                get(&spec["t"][1], i, 0.0),
                get(&spec["t"][2], i, 0.0),
            ];
            let s = [
                get(&spec["s"][0], i, 1.0),
                get(&spec["s"][1], i, 1.0),
                get(&spec["s"][2], i, 1.0),
            ];
            let mut m = m4_ident();
            m[0] = s[0];
            m[5] = s[1];
            m[10] = s[2];
            if use_r {
                let r = euler_xyz(
                    get(&spec["r"][0], i, 0.0),
                    get(&spec["r"][1], i, 0.0),
                    get(&spec["r"][2], i, 0.0),
                );
                m = m4_mul(&r, &m);
            }
            if use_rotto {
                let z = norm3([
                    get(&spec["rotto"][0], i, 0.0),
                    get(&spec["rotto"][1], i, 0.0),
                    get(&spec["rotto"][2], i, 1.0),
                ]);
                let mut up = [
                    get(&spec["up"][0], i, 0.0),
                    get(&spec["up"][1], i, 1.0),
                    get(&spec["up"][2], i, 0.0),
                ];
                let mut x = cross(up, z);
                if (x[0] * x[0] + x[1] * x[1] + x[2] * x[2]) < 1e-12 {
                    up = if z[1].abs() < 0.9 {
                        [0.0, 1.0, 0.0]
                    } else {
                        [1.0, 0.0, 0.0]
                    };
                    x = cross(up, z);
                }
                let x = norm3(x);
                let y = cross(z, x);
                let mut r = m4_ident();
                r[0..3].copy_from_slice(&x);
                r[4..7].copy_from_slice(&y);
                r[8..11].copy_from_slice(&z);
                m = m4_mul(&r, &m);
            }
            m[12] = t[0];
            m[13] = t[1];
            m[14] = t[2];
            out.extend(m.iter().map(|v| *v as f32));
            if use_col {
                out.extend_from_slice(&[
                    get(&spec["color"][0], i, 1.0) as f32,
                    get(&spec["color"][1], i, 1.0) as f32,
                    get(&spec["color"][2], i, 1.0) as f32,
                    1.0,
                ]);
            } else {
                out.extend_from_slice(&[1.0, 1.0, 1.0, 1.0]);
            }
        }
        out
    }

    // ------------------------------------------------------------ output
    fn output_tex(&self) -> Option<Tex> {
        self.out.get(&self.output).and_then(|v| v.first()).copied()
    }

    pub fn readback(&self) -> (Vec<u8>, i32, i32) {
        let t = match self.output_tex() {
            Some(t) => t,
            None => return (vec![0, 0, 0, 255], 1, 1),
        };
        let mut raw = vec![0u8; (t.w * t.h * 4) as usize];
        unsafe {
            self.gl
                .bind_framebuffer(glow::FRAMEBUFFER, Some(self.scratch_fbo));
            self.gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER,
                glow::COLOR_ATTACHMENT0,
                glow::TEXTURE_2D,
                Some(t.tex),
                0,
            );
            self.gl.read_buffer(glow::COLOR_ATTACHMENT0);
            self.gl.read_pixels(
                0,
                0,
                t.w,
                t.h,
                glow::RGBA,
                glow::UNSIGNED_BYTE,
                glow::PixelPackData::Slice(&mut raw),
            );
        }
        (crate::flip_vert(&raw, t.w as usize, t.h as usize), t.w, t.h)
    }

    pub fn present_scanout(&self, dw: i32, dh: i32) {
        let t = match self.output_tex() {
            Some(t) => t,
            None => return,
        };
        let gl = self.gl;
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.viewport(0, 0, dw, dh);
            gl.disable(glow::DEPTH_TEST);
            gl.clear_color(0.0, 0.0, 0.0, 1.0);
            gl.clear(glow::COLOR_BUFFER_BIT);
            gl.use_program(Some(self.blit));
            gl.bind_vertex_array(Some(self.empty_vao));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(t.tex));
            if let Some(l) = gl.get_uniform_location(self.blit, "tex") {
                gl.uniform_1_i32(Some(&l), 0);
            }
            let (ia, da) = (t.w as f32 / t.h as f32, dw as f32 / dh as f32);
            let (sx, sy) = if ia < da {
                (ia / da, 1.0)
            } else {
                (1.0, da / ia)
            };
            if let Some(l) = gl.get_uniform_location(self.blit, "uScale") {
                gl.uniform_2_f32(Some(&l), sx, sy);
            }
            gl.draw_arrays(glow::TRIANGLES, 0, 3);
        }
    }

    pub fn prof(&self) -> Prof {
        self.prof.clone()
    }

    pub fn store(&self) -> Chops {
        self.store.clone()
    }

    /// Save any node's current output (debugging / tests).
    pub fn save_node(&self, id: &str, path: &str) -> bool {
        let t = match self.out.get(id).and_then(|v| v.first()).copied() {
            Some(t) => t,
            None => return false,
        };
        let f = self.read_tex_f32(t);
        let px: Vec<u8> = f
            .chunks_exact(4)
            .map(|c| {
                (f32::from_le_bytes(c.try_into().unwrap()).clamp(0.0, 1.0) * 255.0 + 0.5) as u8
            })
            .collect();
        let img = crate::flip_vert(&px, t.w as usize, t.h as usize);
        image::save_buffer(
            path,
            &img,
            t.w as u32,
            t.h as u32,
            image::ExtendedColorType::Rgba8,
        )
        .is_ok()
    }

    pub fn shutdown(&mut self) {
        self.host.exit();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: &M4, b: &M4) -> bool {
        a.iter().zip(b.iter()).all(|(x, y)| (x - y).abs() < 1e-9)
    }

    fn translate(x: f64, y: f64, z: f64) -> M4 {
        let mut m = m4_ident();
        m[12] = x;
        m[13] = y;
        m[14] = z;
        m
    }

    #[test]
    fn mul_is_column_major() {
        // T then R: a point at the origin lands at the translation
        let t = translate(1.0, 2.0, 3.0);
        let r = euler_xyz(0.0, 0.0, 90.0);
        let m = m4_mul(&t, &r);
        assert!((m[12] - 1.0).abs() < 1e-12 && (m[13] - 2.0).abs() < 1e-12);
        // rz 90 maps +X to +Y: column 0 is the image of +X
        assert!((m[0]).abs() < 1e-12 && (m[1] - 1.0).abs() < 1e-12);
    }

    #[test]
    fn euler_is_rz_ry_rx() {
        let r = euler_xyz(30.0, -45.0, 60.0);
        let want = m4_mul(
            &m4_mul(&euler_xyz(0.0, 0.0, 60.0), &euler_xyz(0.0, -45.0, 0.0)),
            &euler_xyz(30.0, 0.0, 0.0),
        );
        assert!(close(&r, &want));
    }

    #[test]
    fn inverse_round_trips() {
        let mut m = m4_mul(&translate(4.0, -2.0, 0.5), &euler_xyz(10.0, 20.0, 30.0));
        for i in 0..3 {
            m[i * 5] *= 2.0 + i as f64; // non-uniform scale on the diagonal
        }
        assert!(close(&m4_mul(&m, &m4_inv(&m)), &m4_ident()));
        assert!(close(&m4_mul(&m4_inv(&m), &m), &m4_ident()));
        // singular input falls back to identity instead of NaNs
        assert!(close(&m4_inv(&[0.0; 16]), &m4_ident()));
    }
}
