"""
Persistent GL renderer for realtime playback.

Unlike the one-shot backend, this compiles programs and uploads source textures
ONCE, then `render(t)` redraws each frame — evaluating per-frame (time) uniforms
so animated params (e.g. a Transform's `rotate = absTime.seconds*10`) actually
move. Output frames are read back as numpy RGBA. Used by the MJPEG stream server;
`run()` wraps it for a single frame (host reference / conformance).
"""
from __future__ import annotations
import numpy as np
from OpenGL import GL

from runtime import egl_context, sources
from runtime.expr import eval_expr
from lowering.lower import RuntimePlan


def _compile(src: str, stage) -> int:
    sh = GL.glCreateShader(stage)
    GL.glShaderSource(sh, src)
    GL.glCompileShader(sh)
    if GL.glGetShaderiv(sh, GL.GL_COMPILE_STATUS) != GL.GL_TRUE:
        raise RuntimeError("shader compile failed:\n" + GL.glGetShaderInfoLog(sh).decode()
                           + "\n--- source ---\n" + src)
    return sh


def _program(vs: str, fs: str) -> int:
    prog = GL.glCreateProgram()
    a, b = _compile(vs, GL.GL_VERTEX_SHADER), _compile(fs, GL.GL_FRAGMENT_SHADER)
    GL.glAttachShader(prog, a); GL.glAttachShader(prog, b)
    GL.glLinkProgram(prog)
    if GL.glGetProgramiv(prog, GL.GL_LINK_STATUS) != GL.GL_TRUE:
        raise RuntimeError("link failed:\n" + GL.glGetProgramInfoLog(prog).decode())
    GL.glDeleteShader(a); GL.glDeleteShader(b)
    return prog


def _texture(w: int, h: int, data: np.ndarray | None) -> int:
    tex = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
    for k, v in ((GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE),
                 (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE),
                 (GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR),
                 (GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)):
        GL.glTexParameteri(GL.GL_TEXTURE_2D, k, v)
    buf = None if data is None else np.ascontiguousarray(data, np.uint8)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0,
                    GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, buf)
    return tex


def _set_uniform(prog: int, name: str, kind: str, val) -> None:
    loc = GL.glGetUniformLocation(prog, name)
    if loc == -1:
        return
    if kind == "vec2":
        GL.glUniform2f(loc, float(val[0]), float(val[1]))
    elif kind == "vec4":
        GL.glUniform4f(loc, *(float(x) for x in val))
    elif kind == "float":
        GL.glUniform1f(loc, float(val))
    elif kind == "int":
        GL.glUniform1i(loc, int(val))


class Renderer:
    def __init__(self, plan: RuntimePlan):
        self.plan = plan
        egl_context.make_current()
        self.vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self.vao)
        self.prog: dict[str, int] = {}
        self.tex: dict[str, int] = {}
        self.fbo: dict[str, int] = {}
        self.size: dict[str, tuple[int, int]] = {}
        for st in plan.steps:
            w, h = st.target["w"], st.target["h"]
            if st.kind == "source":
                img = sources.load(st.params)
                if (img.shape[1], img.shape[0]) != (w, h):
                    w, h = img.shape[1], img.shape[0]
                self.tex[st.node_id] = _texture(w, h, (img * 255.0 + 0.5).astype(np.uint8))
                self.size[st.node_id] = (w, h)
            elif st.kind == "shader":
                self.prog[st.node_id] = _program(st.vertex, st.fragment)
                out = _texture(w, h, None)
                fbo = GL.glGenFramebuffers(1)
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
                GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                          GL.GL_TEXTURE_2D, out, 0)
                self.tex[st.node_id] = out
                self.fbo[st.node_id] = fbo
                self.size[st.node_id] = (w, h)

    def render(self, t: float, frame: int = 0) -> np.ndarray:
        for st in self.plan.steps:
            if st.kind == "passthrough":
                src = st.inputs[0] if st.inputs else None
                if src in self.tex:
                    self.tex[st.node_id] = self.tex[src]
                    self.size[st.node_id] = self.size[src]
                else:
                    w, h = st.target["w"], st.target["h"]
                    self.tex[st.node_id] = _texture(w, h, np.zeros((h, w, 4), np.uint8))
                    self.size[st.node_id] = (w, h)
            elif st.kind == "shader":
                w, h = self.size[st.node_id]
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo[st.node_id])
                GL.glViewport(0, 0, w, h)
                prog = self.prog[st.node_id]
                GL.glUseProgram(prog)
                for i, src_id in enumerate(st.inputs):
                    GL.glActiveTexture(GL.GL_TEXTURE0 + i)
                    GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex[src_id])
                if st.sampler_array:
                    loc = GL.glGetUniformLocation(prog, st.sampler_array + "[0]")
                    if loc == -1:
                        loc = GL.glGetUniformLocation(prog, st.sampler_array)
                    if loc != -1:
                        GL.glUniform1iv(loc, len(st.inputs),
                                        (GL.GLint * len(st.inputs))(*range(len(st.inputs))))
                else:
                    for i in range(len(st.inputs)):
                        l2 = GL.glGetUniformLocation(prog, f"tex{i}")
                        if l2 != -1:
                            GL.glUniform1i(l2, i)
                for name, (kind, val) in st.uniforms.items():
                    _set_uniform(prog, name, kind, val)
                for name, spec in st.time_uniforms.items():
                    v = eval_expr(spec["expr"], t, frame) * spec.get("mul", 1.0)
                    _set_uniform(prog, name, "float", v)
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

        out_id = self.plan.output_id
        sw, sh = self.size[out_id]
        fbo = self.fbo.get(out_id)
        if fbo is None:  # output is a source/passthrough: attach its texture to read
            fbo = GL.glGenFramebuffers(1)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
            GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                      GL.GL_TEXTURE_2D, self.tex[out_id], 0)
        else:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        raw = GL.glReadPixels(0, 0, sw, sh, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        return np.frombuffer(raw, np.uint8).reshape(sh, sw, 4).copy()


def run(plan: RuntimePlan) -> np.ndarray:
    """One-shot render at t=0 (host reference / conformance)."""
    return Renderer(plan).render(0.0, 0)
