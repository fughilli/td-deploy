"""Ask a running player what it is before building for it.

Players share a name (a Pi and an x86 mini PC both come up as tdplayer.local),
and a box can be reflashed from one kind to the other, so every deploy asks —
one ssh round trip — rather than trusting a setting:

    `uname -m`                      -> the CPU architecture -> players.for_arch()
    `command -v tdplayer-prepare`   -> the image can build Python-host venvs

and deploy() compiles the matching artifact: GLSL ES + aarch64 native code for a
Pi, desktop GL + x86_64 native code (or a Python-host artifact) for a mini PC.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from . import players
from .progress import Progress
from .push import _ssh_base, default_key

PREPARE = "tdplayer-prepare"

_PROBE = f"uname -m; command -v {PREPARE} >/dev/null 2>&1 && echo prepare || echo -"


@dataclass(frozen=True)
class Target:
    host: str
    machine: str  # raw `uname -m`
    player: players.Player
    has_prepare: bool  # the image ships tdplayer-prepare (Python-host capable)


def parse_probe(host: str, out: str) -> Target:
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(f"{host}: empty reply to the architecture probe")
    machine = lines[0]
    return Target(
        host=host,
        machine=machine,
        player=players.for_arch(machine),
        has_prepare=len(lines) > 1 and lines[1] == "prepare",
    )


def probe(
    host: str,
    *,
    user: str = "root",
    key: str | None = None,
    progress: Progress = Progress(),
) -> Target:
    """ssh to the player and identify it. Raises with an actionable message when
    the box is unreachable (the push would fail the same way anyway)."""
    key = key or default_key()
    progress.phase("detect", 0.0, f"{user}@{host}")
    try:
        r = subprocess.run(
            _ssh_base(key) + [f"{user}@{host}", _PROBE],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"timed out reaching {host} to detect its architecture") from e
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(
            f"could not reach {user}@{host} to detect its architecture"
            + (f": {detail[-1]}" if detail else "")
        )
    t = parse_probe(host, r.stdout)
    progress.log(f"{host}: {t.machine} -> {t.player.label}")
    progress.phase("detect", 1.0, t.player.label)
    return t


def resolve_target(
    requested: str | None, player: players.Player, progress: Progress = Progress()
) -> str:
    """The compile target for this player. "auto" (or unset) takes the player's
    own; an explicit choice is honoured where the player can run it (gles vs gles2
    on a Pi), and replaced — with a note — where it can't."""
    if not requested or requested == "auto":
        return player.target
    ok = {"aarch64": {"gles2", "gles"}, "x86_64": {"desktop_gl"}}.get(player.arch, {player.target})
    if requested in ok:
        return requested
    progress.log(f"target {requested} can't run on {player.label}; using {player.target}")
    return player.target
