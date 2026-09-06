"""Compile-check translated shaders under a real Mesa GLES 2.0 context (proves
Pi3/VC4 compatibility — same Mesa GLSL-ES-1.00 frontend). Usage: check_gles2.py <dir>"""
import glob
import os
import sys

os.environ["PYOPENGL_PLATFORM"] = "egl"
from OpenGL import EGL, GL  # noqa: E402

SURFACELESS = 0x31DD
EGL_OPENGL_ES2_BIT = 0x0004

dpy = EGL.eglGetPlatformDisplay(SURFACELESS, EGL.EGL_DEFAULT_DISPLAY, None)
EGL.eglInitialize(dpy, EGL.EGLint(), EGL.EGLint())
EGL.eglBindAPI(EGL.EGL_OPENGL_ES_API)
cfg_attrs = (EGL.EGLint * 9)(
    EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT,
    EGL.EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
    EGL.EGL_RED_SIZE, 8, EGL.EGL_GREEN_SIZE, 8, EGL.EGL_NONE)
cfg = (EGL.EGLConfig * 1)()
n = EGL.EGLint()
EGL.eglChooseConfig(dpy, cfg_attrs, cfg, 1, n)
ctx = EGL.eglCreateContext(dpy, cfg[0], EGL.EGL_NO_CONTEXT,
                           (EGL.EGLint * 3)(EGL.EGL_CONTEXT_MAJOR_VERSION, 2, EGL.EGL_NONE))
EGL.eglMakeCurrent(dpy, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, ctx)
print("context:", GL.glGetString(GL.GL_VERSION).decode())
print("GLSL   :", GL.glGetString(GL.GL_SHADING_LANGUAGE_VERSION).decode())


def compile_one(src, stage):
    s = GL.glCreateShader(stage)
    GL.glShaderSource(s, src)
    GL.glCompileShader(s)
    ok = GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS)
    return ok == GL.GL_TRUE, GL.glGetShaderInfoLog(s)


allok = True
for f in sorted(glob.glob(sys.argv[1] + "/*.frag")):
    base = f[:-5]
    name = os.path.basename(base)
    vok, vl = compile_one(open(base + ".vert").read(), GL.GL_VERTEX_SHADER)
    fok, fl = compile_one(open(f).read(), GL.GL_FRAGMENT_SHADER)
    if vok and fok:
        print(f"  OK   {name}")
    else:
        allok = False
        print(f"  FAIL {name}\n    vert: {vl}\n    frag: {fl}")
print("\nALL COMPILE under GLES 2.0 (Pi3-compatible)" if allok else "\nFAILURES")
sys.exit(0 if allok else 1)
