"""
Persistent GL renderer for realtime playback.

Unlike the one-shot backend, this compiles programs and uploads source textures
ONCE, then `render(t)` redraws each frame — evaluating per-frame (time) uniforms
so animated params (e.g. a Transform's `rotate = absTime.seconds*10`) actually
move. Output frames are read back as numpy RGBA. Used by the MJPEG stream server;
`run()` wraps it for a single frame (host reference / conformance).
"""

from __future__ import annotations

import math
import os

import numpy as np
from OpenGL import GL

from lowering import shaders
from lowering.lower import RuntimePlan
from runtime import egl_context, sources, video
from runtime.expr import eval_expr


def _compile(src: str, stage) -> int:
    sh = GL.glCreateShader(stage)
    GL.glShaderSource(sh, src)
    GL.glCompileShader(sh)
    if GL.glGetShaderiv(sh, GL.GL_COMPILE_STATUS) != GL.GL_TRUE:
        raise RuntimeError(
            "shader compile failed:\n"
            + GL.glGetShaderInfoLog(sh).decode()
            + "\n--- source ---\n"
            + src
        )
    return sh


def _program(vs: str, fs: str) -> int:
    prog = GL.glCreateProgram()
    a, b = _compile(vs, GL.GL_VERTEX_SHADER), _compile(fs, GL.GL_FRAGMENT_SHADER)
    GL.glAttachShader(prog, a)
    GL.glAttachShader(prog, b)
    GL.glLinkProgram(prog)
    if GL.glGetProgramiv(prog, GL.GL_LINK_STATUS) != GL.GL_TRUE:
        raise RuntimeError("link failed:\n" + GL.glGetProgramInfoLog(prog).decode())
    GL.glDeleteShader(a)
    GL.glDeleteShader(b)
    return prog


def _texture(w: int, h: int, data: np.ndarray | None) -> int:
    tex = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
    for k, v in (
        (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE),
        (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE),
        (GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR),
        (GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR),
    ):
        GL.glTexParameteri(GL.GL_TEXTURE_2D, k, v)
    # Upload bottom-row-first (GL's convention): flip incoming top-down image data
    # so texel t=0 is the image's bottom row. TD's GLSL TOPs assume this (e.g.
    # sprite-atlas row math), and readback flips back — net identity for output.
    buf = None if data is None else np.ascontiguousarray(np.flipud(data), np.uint8)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, buf)
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
    def __init__(self, plan: RuntimePlan, chops=None):
        self.plan = plan
        self.chops = chops  # ChopStore for op('..')['..'] param exprs (OSC/MIDI)
        egl_context.make_current()
        self.vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self.vao)
        self.prog: dict[str, int] = {}
        self.tex: dict[str, int] = {}
        self.fbo: dict[str, int] = {}
        self.size: dict[str, tuple[int, int]] = {}
        self.videos: dict[str, video.VideoSource] = {}
        self._seeded: set[str] = set()  # feedback buffers already given a first frame
        self._blit: int | None = None  # lazily built copy program for feedback
        for st in plan.steps:
            w, h = st.target["w"], st.target["h"]
            if st.kind == "source":
                path = st.params.get("path")
                if video.is_video(path) and path and os.path.isfile(path):
                    vs = video.VideoSource(path)
                    self.videos[st.node_id] = vs
                    w, h = vs.width, vs.height
                    self.tex[st.node_id] = _texture(w, h, vs.frame_at(0.0))
                else:
                    img = sources.load(st.params)
                    if (img.shape[1], img.shape[0]) != (w, h):
                        w, h = img.shape[1], img.shape[0]
                    self.tex[st.node_id] = _texture(w, h, (img * 255.0 + 0.5).astype(np.uint8))
                self.size[st.node_id] = (w, h)
            elif st.kind in ("shader", "feedback"):
                if st.kind == "shader":
                    self.prog[st.node_id] = _program(st.vertex, st.fragment)
                # A feedback buffer has no shader of its own; it just needs a
                # texture that survives between frames, plus an FBO to copy into.
                out = _texture(w, h, np.zeros((h, w, 4), np.uint8))
                fbo = GL.glGenFramebuffers(1)
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
                GL.glFramebufferTexture2D(
                    GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, out, 0
                )
                self.tex[st.node_id] = out
                self.fbo[st.node_id] = fbo
                self.size[st.node_id] = (w, h)

    def _copy(self, src_tex: int, dst_id: str) -> None:
        """Full-screen copy of `src_tex` into the feedback buffer `dst_id`."""
        if self._blit is None:
            self._blit = _program(
                shaders.vertex(self.plan.target), shaders.passthrough(self.plan.target)
            )
        w, h = self.size[dst_id]
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo[dst_id])
        GL.glViewport(0, 0, w, h)
        GL.glUseProgram(self._blit)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, src_tex)
        loc = GL.glGetUniformLocation(self._blit, "tex0")
        if loc != -1:
            GL.glUniform1i(loc, 0)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

    def render(self, t: float, frame: int = 0) -> np.ndarray:
        # advance any video sources to the frame for this time
        for nid, vs in self.videos.items():
            data = np.ascontiguousarray(np.flipud(vs.frame_at(t)), np.uint8)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex[nid])
            GL.glTexSubImage2D(
                GL.GL_TEXTURE_2D,
                0,
                0,
                0,
                vs.width,
                vs.height,
                GL.GL_RGBA,
                GL.GL_UNSIGNED_BYTE,
                data,
            )
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
            elif st.kind == "feedback":
                # Its texture already holds the PREVIOUS frame of the target, which
                # is exactly what downstream should sample — so nothing to do here
                # except seed it the first time from input 0 (TD's reset image).
                if st.node_id not in self._seeded:
                    self._seeded.add(st.node_id)
                    src = st.inputs[0] if st.inputs else None
                    if src in self.tex:
                        self._copy(self.tex[src], st.node_id)
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
                        GL.glUniform1iv(
                            loc, len(st.inputs), (GL.GLint * len(st.inputs))(*range(len(st.inputs)))
                        )
                else:
                    for i in range(len(st.inputs)):
                        l2 = GL.glGetUniformLocation(prog, f"tex{i}")
                        if l2 != -1:
                            GL.glUniform1i(l2, i)
                for name, (kind, val) in st.uniforms.items():
                    _set_uniform(prog, name, kind, val)
                for name, spec in st.time_uniforms.items():
                    v = eval_expr(spec["expr"], t, frame, chops=self.chops) * spec.get("mul", 1.0)
                    # Bound periodic uniforms (rotation: mod=2pi) before upload, so a
                    # large angle keeps float precision — matches the Rust runtime.
                    mod = spec.get("mod")
                    if mod:
                        v = math.fmod(v, mod)
                    _set_uniform(prog, name, "float", v)
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

        # End of frame: capture each target into its feedback buffer, so the NEXT
        # frame reads this frame's result. Done after the whole cook because a
        # target is normally downstream of the feedback that echoes it.
        for st in self.plan.steps:
            if st.kind == "feedback" and st.feedback_from in self.tex:
                self._copy(self.tex[st.feedback_from], st.node_id)

        out_id = self.plan.output_id
        sw, sh = self.size[out_id]
        fbo = self.fbo.get(out_id)
        if fbo is None:  # output is a source/passthrough: attach its texture to read
            fbo = GL.glGenFramebuffers(1)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
            GL.glFramebufferTexture2D(
                GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, self.tex[out_id], 0
            )
        else:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        raw = GL.glReadPixels(0, 0, sw, sh, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        img = np.frombuffer(raw, np.uint8).reshape(sh, sw, 4)
        return np.flipud(img).copy()  # GL is bottom-up; restore top-down for output


def run(plan: RuntimePlan, chops=None) -> np.ndarray:
    """One-shot render at t=0 (host reference / conformance)."""
    return Renderer(plan, chops=chops).render(0.0, 0)
