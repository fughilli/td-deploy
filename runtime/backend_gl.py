"""
GL runtime backend — executes a RuntimePlan on real (offscreen) OpenGL.

`shader` steps render a full-screen pass into an FBO-backed RGBA8 texture;
`source` uploads a texture; `passthrough` aliases input 0's texture. At the end
the output node's texture is read back to a numpy image. Textures use
clamp-to-edge (matches the CPU reference's edge replication).
"""
from __future__ import annotations
import numpy as np
from OpenGL import GL

from runtime import egl_context, sources
from lowering.lower import RuntimePlan


def _compile(src: str, stage) -> int:
    sh = GL.glCreateShader(stage)
    GL.glShaderSource(sh, src)
    GL.glCompileShader(sh)
    if GL.glGetShaderiv(sh, GL.GL_COMPILE_STATUS) != GL.GL_TRUE:
        log = GL.glGetShaderInfoLog(sh).decode()
        raise RuntimeError(f"shader compile failed:\n{log}\n--- source ---\n{src}")
    return sh


def _program(vs_src: str, fs_src: str) -> int:
    prog = GL.glCreateProgram()
    vs = _compile(vs_src, GL.GL_VERTEX_SHADER)
    fs = _compile(fs_src, GL.GL_FRAGMENT_SHADER)
    GL.glAttachShader(prog, vs)
    GL.glAttachShader(prog, fs)
    GL.glLinkProgram(prog)
    if GL.glGetProgramiv(prog, GL.GL_LINK_STATUS) != GL.GL_TRUE:
        raise RuntimeError("link failed:\n" + GL.glGetProgramInfoLog(prog).decode())
    GL.glDeleteShader(vs)
    GL.glDeleteShader(fs)
    return prog


def _make_texture(w: int, h: int, data: np.ndarray | None) -> int:
    tex = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
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
        GL.glUniform4f(loc, float(val[0]), float(val[1]), float(val[2]), float(val[3]))
    elif kind == "float":
        GL.glUniform1f(loc, float(val))
    elif kind == "int":
        GL.glUniform1i(loc, int(val))


def run(plan: RuntimePlan) -> np.ndarray:
    egl_context.make_current()
    vao = GL.glGenVertexArrays(1)
    GL.glBindVertexArray(vao)
    tex_of: dict[str, int] = {}
    size_of: dict[str, tuple[int, int]] = {}

    for st in plan.steps:
        w, h = st.target["w"], st.target["h"]
        if st.kind == "source":
            img = sources.load(st.params)
            tex_of[st.node_id] = _make_texture(w, h, (img * 255.0 + 0.5).astype(np.uint8))
            size_of[st.node_id] = (w, h)

        elif st.kind == "passthrough":
            src = st.inputs[0] if st.inputs else None
            if src is None or src not in tex_of:
                # nothing upstream (e.g. dangling COMP input) -> black
                tex_of[st.node_id] = _make_texture(w, h, np.zeros((h, w, 4), np.uint8))
                size_of[st.node_id] = (w, h)
            else:
                tex_of[st.node_id] = tex_of[src]
                size_of[st.node_id] = size_of[src]

        elif st.kind == "shader":
            out_tex = _make_texture(w, h, None)
            fbo = GL.glGenFramebuffers(1)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
            GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                      GL.GL_TEXTURE_2D, out_tex, 0)
            GL.glViewport(0, 0, w, h)
            prog = _program(st.vertex, st.fragment)
            GL.glUseProgram(prog)
            # bind input textures
            for i, src_id in enumerate(st.inputs):
                GL.glActiveTexture(GL.GL_TEXTURE0 + i)
                GL.glBindTexture(GL.GL_TEXTURE_2D, tex_of[src_id])
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
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
            tex_of[st.node_id] = out_tex
            size_of[st.node_id] = (w, h)

    # read back the output node's texture
    out_id = plan.output_id
    sw, sh = size_of[out_id]
    fbo = GL.glGenFramebuffers(1)
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
    GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                              GL.GL_TEXTURE_2D, tex_of[out_id], 0)
    GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
    raw = GL.glReadPixels(0, 0, sw, sh, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
    return np.frombuffer(raw, np.uint8).reshape(sh, sw, 4).copy()
