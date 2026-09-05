// toxc native runtime (M2). Headless EGL-surfaceless GL via Rust (khronos-egl +
// glow). `probe` prints the GL context; `blur <out.png>` renders the same gaussian
// blur as the Python reference over the same testcard, for conformance diffing.
// On the Pi this same code targets the V3D with GLES instead of llvmpipe.
use glow::HasContext;
use khronos_egl as egl;

const PLATFORM_SURFACELESS_MESA: egl::Enum = 0x31DD;
const CTX_OPENGL_PROFILE_MASK: egl::Int = 0x30FD;
const CTX_OPENGL_CORE_PROFILE_BIT: egl::Int = 0x0000_0001;

const W: usize = 512;
const H: usize = 512;
const SIGMA: f64 = 4.0;

fn make_gl() -> (egl::DynamicInstance<egl::EGL1_5>, egl::Display, glow::Context) {
    let egl = unsafe { egl::DynamicInstance::<egl::EGL1_5>::load_required() }
        .expect("load libEGL");
    let display = unsafe {
        egl.get_platform_display(PLATFORM_SURFACELESS_MESA, egl::DEFAULT_DISPLAY,
                                 &[egl::ATTRIB_NONE])
    }.expect("get_platform_display");
    egl.initialize(display).expect("initialize");
    egl.bind_api(egl::OPENGL_API).expect("bind_api");
    let cfg = egl.choose_first_config(display, &[
        egl::SURFACE_TYPE, egl::PBUFFER_BIT,
        egl::RENDERABLE_TYPE, egl::OPENGL_BIT,
        egl::RED_SIZE, 8, egl::GREEN_SIZE, 8, egl::BLUE_SIZE, 8, egl::NONE,
    ]).expect("choose_config").expect("no config");
    let ctx = egl.create_context(display, cfg, None, &[
        egl::CONTEXT_MAJOR_VERSION, 3, egl::CONTEXT_MINOR_VERSION, 3,
        CTX_OPENGL_PROFILE_MASK, CTX_OPENGL_CORE_PROFILE_BIT, egl::NONE,
    ]).expect("create_context");
    egl.make_current(display, None, None, Some(ctx)).expect("make_current");
    let gl = unsafe {
        glow::Context::from_loader_function(|s| match egl.get_proc_address(s) {
            Some(f) => f as *const std::ffi::c_void,
            None => std::ptr::null(),
        })
    };
    (egl, display, gl)
}

// Matches runtime/sources.py testcard(), top-down RGBA8.
fn testcard() -> Vec<u8> {
    let mut buf = vec![0u8; W * H * 4];
    for y in 0..H {
        for x in 0..W {
            let i = (y * W + x) * 4;
            let checker = if ((x / 32) + (y / 32)) % 2 == 1 { 1.0 } else { 0.0 };
            let mut r = checker;
            let mut g = x as f32 / (W - 1) as f32;
            let mut b = y as f32 / (H - 1) as f32;
            // 2px-wide white cross, matching runtime/sources.py (h//2-1:h//2+1)
            if y == H / 2 - 1 || y == H / 2 || x == W / 2 - 1 || x == W / 2 {
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

fn gaussian_weights(sigma: f64) -> (usize, Vec<f64>) {
    let radius = (3.0 * sigma).ceil() as usize;
    let radius = radius.clamp(1, 20);
    let mut raw = Vec::new();
    for k in 0..(2 * radius + 1) {
        let x = k as f64 - radius as f64;
        raw.push((-(x * x) / (2.0 * sigma * sigma)).exp());
    }
    let s: f64 = raw.iter().sum();
    (radius, raw.iter().map(|w| w / s).collect())
}

const VERT: &str = "#version 330 core\n\
out vec2 vUV;\n\
void main(){ vec2 uv=vec2((gl_VertexID==1)?2.0:0.0,(gl_VertexID==2)?2.0:0.0);\
 vUV=uv; gl_Position=vec4(uv*2.0-1.0,0.0,1.0); }\n";

fn gaussian_frag(radius: usize, w: &[f64]) -> String {
    let wl: Vec<String> = w.iter().map(|v| format!("{:.9e}", v)).collect();
    format!(
        "#version 330 core\n\
in vec2 vUV;\nout vec4 fragColor;\n\
uniform sampler2D tex0;\nuniform vec2 uResolution;\n\
const int R = {r};\nconst float Wt[{n}] = float[]({wl});\n\
void main(){{\n\
  vec2 texel = 1.0/uResolution; vec4 acc = vec4(0.0);\n\
  for(int j=-R;j<=R;++j) for(int i=-R;i<=R;++i){{\n\
    float wv = Wt[i+R]*Wt[j+R];\n\
    acc += wv*texture(tex0, vUV+vec2(float(i),float(j))*texel);\n\
  }}\n  fragColor = acc;\n}}\n",
        r = radius, n = 2 * radius + 1, wl = wl.join(", ")
    )
}

fn compile(gl: &glow::Context, ty: u32, src: &str) -> glow::Shader {
    unsafe {
        let s = gl.create_shader(ty).unwrap();
        gl.shader_source(s, src);
        gl.compile_shader(s);
        if !gl.get_shader_compile_status(s) {
            panic!("shader compile error:\n{}\n---\n{}", gl.get_shader_info_log(s), src);
        }
        s
    }
}

fn render_blur(gl: &glow::Context, out: &str) {
    let (radius, w) = gaussian_weights(SIGMA);
    let img = testcard();
    unsafe {
        let vao = gl.create_vertex_array().unwrap();
        gl.bind_vertex_array(Some(vao));

        // source texture (upload flipped -> GL bottom-up, matching Python)
        let mut flipped = vec![0u8; img.len()];
        for y in 0..H {
            let src = (H - 1 - y) * W * 4;
            flipped[y * W * 4..(y + 1) * W * 4].copy_from_slice(&img[src..src + W * 4]);
        }
        let tex = gl.create_texture().unwrap();
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::RGBA8 as i32, W as i32, H as i32, 0,
                        glow::RGBA, glow::UNSIGNED_BYTE, Some(&flipped));

        // output FBO
        let out_tex = gl.create_texture().unwrap();
        gl.bind_texture(glow::TEXTURE_2D, Some(out_tex));
        gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::RGBA8 as i32, W as i32, H as i32, 0,
                        glow::RGBA, glow::UNSIGNED_BYTE, None);
        let fbo = gl.create_framebuffer().unwrap();
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
        gl.framebuffer_texture_2d(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0,
                                  glow::TEXTURE_2D, Some(out_tex), 0);
        gl.viewport(0, 0, W as i32, H as i32);

        // program
        let prog = gl.create_program().unwrap();
        let vs = compile(gl, glow::VERTEX_SHADER, VERT);
        let fs = compile(gl, glow::FRAGMENT_SHADER, &gaussian_frag(radius, &w));
        gl.attach_shader(prog, vs);
        gl.attach_shader(prog, fs);
        gl.link_program(prog);
        if !gl.get_program_link_status(prog) {
            panic!("link error: {}", gl.get_program_info_log(prog));
        }
        gl.use_program(Some(prog));
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        if let Some(l) = gl.get_uniform_location(prog, "tex0") { gl.uniform_1_i32(Some(&l), 0); }
        if let Some(l) = gl.get_uniform_location(prog, "uResolution") {
            gl.uniform_2_f32(Some(&l), W as f32, H as f32);
        }
        gl.draw_arrays(glow::TRIANGLES, 0, 3);

        // readback (bottom-up) then flip to top-down
        let mut raw = vec![0u8; W * H * 4];
        gl.read_pixels(0, 0, W as i32, H as i32, glow::RGBA, glow::UNSIGNED_BYTE,
                       glow::PixelPackData::Slice(&mut raw));
        let mut top = vec![0u8; raw.len()];
        for y in 0..H {
            let src = (H - 1 - y) * W * 4;
            top[y * W * 4..(y + 1) * W * 4].copy_from_slice(&raw[src..src + W * 4]);
        }
        image::save_buffer(out, &top, W as u32, H as u32, image::ExtendedColorType::Rgba8)
            .expect("save png");
        println!("[rust] wrote {out}");
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let (_egl, _dpy, gl) = make_gl();
    if args.get(1).map(|s| s.as_str()) == Some("blur") {
        let out = args.get(2).map(|s| s.as_str()).unwrap_or("rust_blur.png");
        render_blur(&gl, out);
    } else {
        unsafe {
            println!("OK");
            println!("GL_RENDERER {}", gl.get_parameter_string(glow::RENDERER));
            println!("GL_VERSION  {}", gl.get_parameter_string(glow::VERSION));
        }
    }
}
