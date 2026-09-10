"""Resolve the LLVM/MLIR + GLES tools used to finish an artifact (mlir.mlir -> .so,
GLSL -> GLSL ES). Two implementations:

- NixToolchain (dev / this container): runs the pinned nixpkgs mlir-opt/
  mlir-translate/clang (compiler/nix/shell.sh) + glslang/spirv-cross via `nix
  shell`. In the aarch64-linux container this compiles NATIVELY for the Pi (same
  arch), which is enough to prove the deploy loop.
- BundledToolchain (Phase 3, the shipped app): direct paths to the bundled clang/
  lld/mlir tools + an aarch64 sysroot, cross-targeting aarch64-unknown-linux-gnu.

finish.py composes the same command sequence for both; only the tool resolution +
clang cross flags differ.
"""
from __future__ import annotations

import os
import subprocess

from . import _paths


class Toolchain:
    """Interface. clang_flags are appended to the clang codegen invocation (cross
    target/sysroot for the bundled toolchain; empty for a native build)."""
    clang_flags: list[str] = []

    def run_llvm_script(self, script: str) -> None:
        """Run a bash script with mlir-opt/mlir-translate/clang on PATH."""
        raise NotImplementedError

    def run_gles(self, art_dir: str) -> None:
        """Run compiler/translate_gles.py with glslang/spirv-cross on PATH."""
        raise NotImplementedError


class NixToolchain(Toolchain):
    clang_flags = []  # native (aarch64 container == Pi arch)

    def __init__(self, repo_root: str | None = None):
        self.repo = repo_root or _paths.REPO_ROOT
        self._shell = os.path.join(self.repo, "compiler", "nix", "shell.sh")
        self._env = {**os.environ, "PATH": "/nix/var/nix/profiles/default/bin:" + os.environ.get("PATH", "")}

    def run_llvm_script(self, script: str) -> None:
        subprocess.run([self._shell, "bash", "-c", script], check=True, env=self._env)

    def run_gles(self, art_dir: str) -> None:
        translate = os.path.join(self.repo, "compiler", "translate_gles.py")
        subprocess.run(
            ["nix", "shell", "nixpkgs#glslang", "nixpkgs#spirv-cross", "nixpkgs#python3",
             "--command", "python3", translate, art_dir],
            check=True, env=self._env,
        )


class BundledToolchain(Toolchain):
    """Phase 3: direct bundled binaries + aarch64 cross flags. tools_dir holds
    mlir-opt, mlir-translate, clang, ld.lld, glslang, spirv-cross + sysroot/."""
    def __init__(self, tools_dir: str, triple: str = "aarch64-unknown-linux-gnu"):
        self.tools = tools_dir
        self.sysroot = os.path.join(tools_dir, "sysroot")
        self.clang_flags = [f"--target={triple}", f"--sysroot={self.sysroot}", "-fuse-ld=lld"]

    def _bin(self, name: str) -> str:
        return os.path.join(self.tools, "bin", name)

    def run_llvm_script(self, script: str) -> None:
        env = {**os.environ, "PATH": os.path.join(self.tools, "bin") + os.pathsep + os.environ.get("PATH", "")}
        subprocess.run(["bash", "-c", script], check=True, env=env)

    def run_gles(self, art_dir: str) -> None:
        translate = os.path.join(_paths.REPO_ROOT, "compiler", "translate_gles.py")
        env = {**os.environ, "PATH": os.path.join(self.tools, "bin") + os.pathsep + os.environ.get("PATH", "")}
        subprocess.run(["python3", translate, art_dir], check=True, env=env)
