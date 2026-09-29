"""td-deploy engine: .toe -> optimized artifact (host codegen) -> live player update.

    from deploy_engine import deploy
    deploy("project.toe", "tdplayer.local", progress=cli_progress())

Detect (ssh: which kind of player — Pi or x86_64 mini PC — is at the host) +
compile (Python) + finish (host codegen for that player's arch) + push (rsync +
restart). No Nix or Bazel on the deploy path; the player image (built by CI via
sbc-deploy) already carries the runtime, and just runs the finished artifact.
"""

from __future__ import annotations

import tempfile

from . import players
from .compile import compile_toe
from .detect import PREPARE, Target, probe, resolve_target
from .finish import finish
from .progress import Progress, cli_progress
from .push import DEFAULT_SERVICE, push
from .toolchain import BundledToolchain, NixToolchain, Toolchain, default_toolchain

__all__ = [
    "deploy",
    "probe",
    "players",
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
    target: str = "auto",
    arch: str | None = None,
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
    asset_roots: list[str] | None = None,
    asset_map: dict[str, str] | None = None,
    progress: Progress = Progress(),
) -> dict:
    """Full pipeline: detect -> compile -> finish -> push. Returns {artifact, info,
    staging, player}.

    The player is identified over ssh first (`uname -m`; `arch` skips the probe),
    and decides the build: a Pi gets a `gles2` (or `gles`) artifact with aarch64
    native code; an x86_64 player gets `desktop_gl` with x86_64 native code — or,
    for a project that runs Python, a Python-host artifact whose venv the player
    builds before it goes live (tdplayer-prepare). `target="auto"` takes the
    player's own; an explicit one is honoured where that player can run it.

    `strict_unsupported=False` enables lenient "warn but continue" — unsupported TOP
    operators become placeholders instead of raising. `magic_chop=True` drives
    unsupported CHOPs with random sinusoids so the piece still animates (see
    `compile_toe`)."""
    if arch:
        tgt = Target(pi_host, arch, players.for_arch(arch), has_prepare=True)
        progress.log(f"{pi_host}: {tgt.player.label} (given, not probed)")
    else:
        tgt = probe(pi_host, user=user, key=key, progress=progress)
    player = tgt.player
    gl_target = resolve_target(target, player, progress)
    art = artifact_dir or tempfile.mkdtemp(prefix="toxc_artifact_")
    info = compile_toe(
        toe_path,
        art,
        target=gl_target,
        res=res,
        set_file=set_file,
        bridge=bridge,
        strict_unsupported=strict_unsupported,
        magic_chop=magic_chop,
        asset_roots=asset_roots,
        asset_map=asset_map,
        host_mode="auto" if player.python_host else "off",
        progress=progress,
    )
    pre_restart = None
    if info.get("python_host"):
        # Nothing to codegen: the host path's shaders are desktop GLSL run as-is.
        if not tgt.has_prepare:
            raise RuntimeError(
                f"{pi_host} runs an older {player.label} image without {PREPARE}, so it "
                "can't set up this project's Python — reflash it with the current image"
            )
        pre_restart = PREPARE
        progress.log(
            "the player builds this project's Python environment before it goes live "
            "(the first deploy of a requirements set downloads its wheels — a few minutes)"
        )
    else:
        finish(art, gl_target, toolchain or default_toolchain(player.triple), progress)
    staging = push(
        art,
        pi_host,
        user=user,
        key=key,
        service=service,
        pre_restart=pre_restart,
        progress=progress,
    )
    return {"artifact": art, "info": info, "staging": staging, "player": player.kind}
