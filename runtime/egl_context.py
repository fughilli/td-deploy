"""
Headless EGL context creation.

Host reference runtime: EGL_PLATFORM_SURFACELESS_MESA + desktop core GL on llvmpipe
(no display, no DRM device — works in a bare container). On the Pi this file is the
seam that changes: same EGL calls, but a real V3D display/device and a GLES API. The
rest of the GL backend is unchanged.
"""
from __future__ import annotations
import os
import sys

if not sys.platform.startswith("linux"):
    raise RuntimeError(
        "toxc's GL render path is Linux-only (EGL surfaceless + Mesa, mirroring the "
        "Pi's V3D/GLES). On macOS, don't render locally — render in the Linux "
        "container and open the live stream URL in your browser (see README: "
        "'Live realtime preview'). Ask the agent to (re)start the stream. "
        f"(host platform: {sys.platform!r})")

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

from OpenGL import EGL

EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT = 0x00000001


def make_current(gl_major: int = 3, gl_minor: int = 3):
    dpy = EGL.eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA,
                                    EGL.EGL_DEFAULT_DISPLAY, None)
    if dpy == EGL.EGL_NO_DISPLAY:
        raise RuntimeError("eglGetPlatformDisplay(surfaceless) failed")
    major, minor = EGL.EGLint(), EGL.EGLint()
    if not EGL.eglInitialize(dpy, major, minor):
        raise RuntimeError("eglInitialize failed")
    if not EGL.eglBindAPI(EGL.EGL_OPENGL_API):
        raise RuntimeError("eglBindAPI(OpenGL) failed")

    cfg_attrs = (EGL.EGLint * 9)(
        EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT,
        EGL.EGL_RENDERABLE_TYPE, EGL.EGL_OPENGL_BIT,
        EGL.EGL_RED_SIZE, 8,
        EGL.EGL_GREEN_SIZE, 8,
        EGL.EGL_NONE,
    )
    cfg = (EGL.EGLConfig * 1)()
    n = EGL.EGLint()
    EGL.eglChooseConfig(dpy, cfg_attrs, cfg, 1, n)
    if n.value < 1:
        raise RuntimeError("no EGL config")

    ctx_attrs = (EGL.EGLint * 7)(
        EGL.EGL_CONTEXT_MAJOR_VERSION, gl_major,
        EGL.EGL_CONTEXT_MINOR_VERSION, gl_minor,
        0x30FD, EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT,  # EGL_CONTEXT_OPENGL_PROFILE_MASK
        EGL.EGL_NONE,
    )
    ctx = EGL.eglCreateContext(dpy, cfg[0], EGL.EGL_NO_CONTEXT, ctx_attrs)
    if ctx == EGL.EGL_NO_CONTEXT:
        raise RuntimeError("eglCreateContext failed")
    if not EGL.eglMakeCurrent(dpy, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, ctx):
        raise RuntimeError("eglMakeCurrent(surfaceless) failed")
    return dpy, ctx
