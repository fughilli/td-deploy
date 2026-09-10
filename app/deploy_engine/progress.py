"""Progress reporting shared by the CLI and the GUI sidecar.

A deploy is a fixed sequence of phases; each phase reports a 0..1 fraction and log
lines. `Progress` is a tiny sink the engine calls; the CLI prints, the sidecar
serializes to JSON lines for Electron.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Ordered phases of a full deploy, with a rough weight for an overall bar.
PHASES: list[tuple[str, float]] = [
    ("expand", 0.10),    # toeexpand the .toe
    ("import", 0.05),    # tree -> IR
    ("optimize", 0.05),  # graph passes
    ("lower", 0.05),     # IR -> plan (shaders)
    ("emit", 0.10),      # write artifact (schedule/shaders/assets/mlir)
    ("finish", 0.35),    # host codegen: mlir->.so (aarch64) + gles translate
    ("push", 0.20),      # rsync artifact to the Pi
    ("restart", 0.10),   # swap + restart service
]
_PHASE_ORDER = {name: i for i, (name, _) in enumerate(PHASES)}


@dataclass
class Progress:
    """Callbacks: `on_event(phase, frac, message)` for progress, `on_log(line)` for
    free-text log. Defaults print nothing (engine stays quiet unless wired up)."""
    on_event: Callable[[str, float, str], None] = lambda phase, frac, msg: None
    on_log: Callable[[str], None] = lambda line: None

    def phase(self, name: str, frac: float = 0.0, message: str = "") -> None:
        self.on_event(name, max(0.0, min(1.0, frac)), message)

    def log(self, line: str) -> None:
        self.on_log(line)

    def overall(self, phase: str, frac_in_phase: float) -> float:
        """Map (phase, in-phase fraction) to a 0..1 overall fraction using PHASES
        weights, for a single monotonic progress bar."""
        done = sum(w for n, w in PHASES if _PHASE_ORDER.get(n, 1e9) < _PHASE_ORDER.get(phase, -1))
        cur = next((w for n, w in PHASES if n == phase), 0.0)
        total = sum(w for _, w in PHASES)
        return (done + cur * max(0.0, min(1.0, frac_in_phase))) / total


def cli_progress(verbose: bool = True) -> Progress:
    """A Progress that prints phase transitions + logs to stdout."""
    def ev(phase: str, frac: float, msg: str) -> None:
        pct = int(100 * Progress().overall(phase, frac))
        print(f"[{pct:3d}%] {phase:<9} {msg}")

    def lg(line: str) -> None:
        if verbose:
            print("        " + line.rstrip())

    return Progress(on_event=ev, on_log=lg)
