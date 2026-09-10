"""Image sources shared by both runtime backends. Returns float32 HxWx4 in [0,1]."""

from __future__ import annotations

import os

import numpy as np


def testcard(w: int, h: int) -> np.ndarray:
    """Deterministic pattern with sharp features (checkerboard, gradients, thin
    cross) so a blur is unambiguously visible and diffable."""
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 4), np.float32)
    img[..., 0] = (((xx // 32) + (yy // 32)) % 2).astype(np.float32)  # R checkerboard
    img[..., 1] = xx / max(1, w - 1)  # G gradient
    img[..., 2] = yy / max(1, h - 1)  # B gradient
    img[h // 2 - 1 : h // 2 + 1, :, :3] = 1.0  # white cross
    img[:, w // 2 - 1 : w // 2 + 1, :3] = 1.0
    img[..., 3] = 1.0
    return img


def load(params: dict) -> np.ndarray:
    w = int(params.get("w", 512))
    h = int(params.get("h", 512))
    path = params.get("path")
    if path and os.path.isfile(path):
        from PIL import Image

        im = Image.open(path).convert("RGBA").resize((w, h))
        return np.asarray(im, np.float32) / 255.0
    return testcard(w, h)  # no file / host-only asset -> deterministic stand-in
