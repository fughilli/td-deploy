"""Resolve the LLVM/MLIR + GLES tools used to finish an artifact (mlir.mlir -> .so,
GLSL -> GLSL ES). Two implementations:

- NixToolchain (dev / this container): runs the pinned nixpkgs mlir-opt/
  mlir-translate/clang (compiler/nix/shell.sh) + glslang/spirv-cross via `nix
  shell`. In the aarch64-linux container this compiles NATIVELY for the Pi (same
  arch), which is enough to prove the deploy loop.
- BundledToolchain (Phase 3, the shipped app): direct paths to the bundled clang/
  lld/mlir tools + an aarch64 sysroot, cross-targeting aarch64-unknown-linux-gnu.
  Runs each tool as a plain subprocess (NO shell) so it works on Windows too.

finish.py builds a pipeline of argv commands; each toolchain runs them its own way.
Only the tool resolution + clang cross flags differ.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

from . import _paths

# Tool chatter (nix, clang, glslang) must never touch stdout — the GUI sidecar
# uses stdout for its JSON protocol. Route it to stderr.
_STDERR = {"stdout": sys.stderr}


class Toolchain:
    """Interface. clang_flags are appended to the clang codegen invocation (cross
    target/sysroot for the bundled toolchain; empty for a native build)."""
    clang_flags: list[str] = []

    def run_pipeline(self, commands: list[list[str]]) -> None:
        """Run a sequence of argv commands (mlir-opt/mlir-translate/clang), each
        with the toolchain's tools resolvable. Stops on the first failure."""
        raise NotImplementedError

    def run_gles(self, art_dir: str) -> None:
        """Translate shaders to GLSL ES with glslang/spirv-cross available."""
        raise NotImplementedError


class NixToolchain(Toolchain):
    clang_flags = []  # native (aarch64 container == Pi arch)

    def __init__(self, repo_root: str | None = None):
        self.repo = repo_root or _paths.REPO_ROOT
        self._shell = os.path.join(self.repo, "compiler", "nix", "shell.sh")
        self._env = {**os.environ, "PATH": "/nix/var/nix/profiles/default/bin:" + os.environ.get("PATH", "")}

    def run_pipeline(self, commands: list[list[str]]) -> None:
        # One nix shell for the whole pipeline (fast): join argv into a bash script.
        script = "set -e\n" + "\n".join(shlex.join(cmd) for cmd in commands)
        subprocess.run([self._shell, "bash", "-c", script], check=True, env=self._env, **_STDERR)

    def run_gles(self, art_dir: str) -> None:
        translate = os.path.join(self.repo, "compiler", "translate_gles.py")
        subprocess.run(
            ["nix", "shell", "nixpkgs#glslang", "nixpkgs#spirv-cross", "nixpkgs#python3",
             "--command", "python3", translate, art_dir],
            check=True, env=self._env, **_STDERR,
        )


class BundledToolchain(Toolchain):
    """Phase 3: direct bundled binaries + aarch64 cross flags. tools_dir holds
    bin/{mlir-opt,mlir-translate,clang,ld.lld,glslang,spirv-cross} + sysroot/.
    No shell — each command is a direct subprocess, so this runs on Windows."""
    def __init__(self, tools_dir: str, triple: str = "aarch64-unknown-linux-gnu"):
        self.tools = tools_dir
        self.bindir = os.path.join(tools_dir, "bin")
        self.sysroot = os.path.join(tools_dir, "sysroot")
        self.clang_flags = [f"--target={triple}", f"--sysroot={self.sysroot}", "-fuse-ld=lld"]

    def _resolve(self, tool: str) -> str:
        """Map a bare tool name to the bundled binary (with an .exe fallback)."""
        cand = os.path.join(self.bindir, tool)
        if os.path.exists(cand):
            return cand
        if os.path.exists(cand + ".exe"):
            return cand + ".exe"
        return tool  # let the OS resolve it (dev convenience)

    def _env(self) -> dict:
        return {**os.environ, "PATH": self.bindir + os.pathsep + os.environ.get("PATH", "")}

    def run_pipeline(self, commands: list[list[str]]) -> None:
        env = self._env()
        for cmd in commands:
            resolved = [self._resolve(cmd[0]), *cmd[1:]]
            subprocess.run(resolved, check=True, env=env, **_STDERR)

    def run_gles(self, art_dir: str) -> None:
        # Import the reused translator in-process (no python3 needed when frozen);
        # its glslang/spirv-cross subprocesses inherit the bundled bin on PATH.
        _paths.ensure_on_path()
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bindir + os.pathsep + old
        try:
            import translate_gles  # from compiler/, put on path by ensure_on_path
            translate_gles.main(art_dir)
        finally:
            os.environ["PATH"] = old


def default_toolchain() -> Toolchain:
    """Pick the toolchain for the current runtime: the bundled cross-toolchain when
    it's present (frozen app, or TOXC_TOOLCHAIN_DIR), else the Nix dev toolchain."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cand = os.path.join(sys._MEIPASS, "toolchain")  # type: ignore[attr-defined]
        if os.path.isdir(cand):
            return BundledToolchain(cand)
    env = os.environ.get("TOXC_TOOLCHAIN_DIR")
    if env and os.path.isdir(env):
        return BundledToolchain(env)
    return NixToolchain()
