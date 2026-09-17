"""td-deploy engine: .toe -> optimized artifact (host codegen) -> live Pi update.

    from deploy_engine import deploy
    deploy("project.toe", "tdplayer.local", target="gles2", progress=cli_progress())

Compile (Python) + finish (host aarch64 codegen) + push (rsync + restart). No Nix
or Bazel on the deploy path; the Pi just runs the finished artifact.
"""

from __future__ import annotations

import tempfile

from .compile import compile_toe
from .finish import finish
from .progress import Progress, cli_progress
from .push import DEFAULT_SERVICE, push
from .toolchain import BundledToolchain, NixToolchain, Toolchain, default_toolchain

__all__ = [
    "deploy",
    "compile_toe",
    "finish",
    "push",
    "Progress",
    "cli_progress",
    "Toolchain",
    "NixToolchain",
    "BundledToolchain",
    "default_toolchain",
]


def deploy(
    toe_path: str,
    pi_host: str,
    *,
    target: str = "gles2",
    res: int = 256,
    set_file: list[str] | None = None,
    bridge: str | None = None,
    user: str = "root",
    key: str | None = None,
    service: str = DEFAULT_SERVICE,
    toolchain: Toolchain | None = None,
    artifact_dir: str | None = None,
    strict_unsupported: bool = True,
    magic_chop: bool = False,
    progress: Progress = Progress(),
) -> dict:
    """Full pipeline: compile -> finish -> push. Returns {artifact, info, staging}.

    `strict_unsupported=False` enables lenient "warn but continue" — unsupported TOP
    operators become placeholders instead of raising. `magic_chop=True` drives
    unsupported CHOPs with random sinusoids so the piece still animates (see
    `compile_toe`)."""
    art = artifact_dir or tempfile.mkdtemp(prefix="toxc_artifact_")
    info = compile_toe(
        toe_path,
        art,
        target=target,
        res=res,
        set_file=set_file,
        bridge=bridge,
        strict_unsupported=strict_unsupported,
        magic_chop=magic_chop,
        progress=progress,
    )
    finish(art, target, toolchain or default_toolchain(), progress)
    staging = push(art, pi_host, user=user, key=key, service=service, progress=progress)
    return {"artifact": art, "info": info, "staging": staging}
