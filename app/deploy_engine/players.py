"""The kinds of player td-deploy targets, and what each one needs.

One table drives both halves of the app:

* flashing — which base image to fetch from a release (`image`) and what it goes
  onto (`media`): the Pi's SD image, or the x86_64 mini PC's install USB;
* deploying — what a running box's CPU architecture (`uname -m`, see
  deploy_engine.detect) means for the build: which GL target the artifact is
  compiled for, the triple its native code is finished for, and whether a
  Python-host artifact can run there.

Images are published by .github/workflows/build-image.yml as
`<image>.zst` (+ `.sha256`), or `<image>.zst.partNN` pieces when over GitHub's
asset size cap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class Player:
    kind: str  # "pi3" | "amd64"
    label: str  # human name for the flash picker
    media: str  # what the image is written to
    arch: str  # normalized `uname -m`: "aarch64" | "x86_64"
    image: str  # release asset stem (".zst" / ".zst.partNN" appended)
    target: str  # compile target: gles2 (Pi GPU) | desktop_gl (Mesa iris/radeonsi)
    triple: str  # native-code triple the artifact is finished for
    python_host: bool  # can run toxc-host/1 artifacts (Python baked into the image)

    def to_dict(self) -> dict:
        return asdict(self)


PLAYERS: dict[str, Player] = {
    "pi3": Player(
        kind="pi3",
        label="Raspberry Pi 3",
        media="SD card",
        arch="aarch64",
        image="tdplayer-pi3.img",
        target="gles2",
        triple="aarch64-unknown-linux-gnu",
        python_host=False,
    ),
    "amd64": Player(
        kind="amd64",
        label="x86_64 mini PC (Intel/AMD graphics)",
        media="USB installer",
        arch="x86_64",
        image="tdplayer-amd64.iso",
        target="desktop_gl",
        triple="x86_64-unknown-linux-gnu",
        python_host=True,
    ),
}

DEFAULT_KIND = "pi3"

_ARCH_ALIASES = {
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "armv8l": "aarch64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
}


def normalize_arch(machine: str) -> Optional[str]:
    return _ARCH_ALIASES.get((machine or "").strip().lower())


def for_arch(machine: str) -> Player:
    """The player profile for a box reporting `uname -m` == machine."""
    arch = normalize_arch(machine)
    for p in PLAYERS.values():
        if p.arch == arch:
            return p
    raise ValueError(
        f"unsupported player architecture {machine!r} (supported: "
        + ", ".join(sorted({p.arch for p in PLAYERS.values()}))
        + ")"
    )


def get(kind: Optional[str]) -> Player:
    try:
        return PLAYERS[kind or DEFAULT_KIND]
    except KeyError:
        raise ValueError(f"unknown player kind {kind!r} (one of {', '.join(PLAYERS)})") from None


def kinds_in(asset_names) -> list[str]:
    """Which player images a release carries (by asset name)."""
    names = list(asset_names)
    out = []
    for p in PLAYERS.values():
        if any(
            n == p.image + ".zst" or n.startswith(p.image + ".zst.part") or n == p.image
            for n in names
        ):
            out.append(p.kind)
    # Releases from before the x86 player shipped a single Pi image.
    if not out and any(n.endswith((".img.zst", ".img")) for n in names):
        out.append("pi3")
    return out
