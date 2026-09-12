# PyInstaller spec: freeze the td-deploy Studio sidecar into a single self-
# contained binary (no Python install needed on the user's machine).
#
# It bundles:
#   * the sidecar + deploy_engine
#   * the REUSED compiler pipeline (ir/ passes/ lowering/ importer/ runtime/ +
#     compiler/emit_artifact + compiler/translate_gles) — same code the dev CLI runs
#   * Pillow + PyAV (host-asset decoding)
#   * the per-OS cross toolchain under app/toolchain/ (clang/lld/mlir/glslang/
#     spirv-cross), if CI has populated it — added as data so it
#     lands under sys._MEIPASS/toolchain (see toolchain.default_toolchain()).
#
# Build:  pyinstaller app/packaging/sidecar.spec
import os
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_all

# SPECPATH is the spec's dir (app/packaging), so the repo root is two levels up.
REPO = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
APP = os.path.join(REPO, "app")
COMPILER = os.path.join(REPO, "compiler")

# collect_submodules() runs now (before Analysis applies pathex), so the reused
# packages must be importable at spec-eval time: deploy_engine (app/), ir/passes/
# lowering/importer/runtime (repo root), emit_artifact/translate_gles (compiler/).
sys.path[:0] = [REPO, APP, COMPILER]

# Reused pure-Python compiler packages -> pull in every submodule.
hidden = []
for pkg in ("deploy_engine", "ir", "passes", "lowering", "importer", "runtime"):
    hidden += collect_submodules(pkg, on_error="ignore")
# Top-level compiler scripts imported by bare name.
hidden += ["emit_artifact", "translate_gles"]

datas = []
binaries = []
# PyAV ships FFmpeg shared libs + submodules; zstandard has a C extension; PIL too.
for mod in ("av", "PIL", "zstandard"):
    d, b, h = collect_all(mod)
    datas += d
    binaries += b
    hidden += h

# The bundled cross-toolchain (populated per-OS by CI before packaging).
_tc = os.path.join(APP, "toolchain")
if os.path.isdir(_tc):
    datas.append((_tc, "toolchain"))

# CI-stamped base image tag (build_app writes app/version.json), read at runtime.
_ver = os.path.join(APP, "version.json")
if os.path.isfile(_ver):
    datas.append((_ver, "."))

a = Analysis(
    [os.path.join(APP, "sidecar.py")],
    pathex=[REPO, APP, COMPILER],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="td-deploy-sidecar",
    console=True,          # stdio JSON protocol; no window
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="td-deploy-sidecar",
)
