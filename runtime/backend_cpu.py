"""
CPU reference backend — per-op numpy kernels instead of GLSL. It is (a) a
hardware-free validator and (b) the conformance reference the GL/Pi output is
diffed against. Ops with no CPU kernel (e.g. glsl_top) raise NotImplementedError;
the driver detects GL-only plans and skips the cross-check.

For clamp-to-edge sampling a separable gaussian is *exactly* the 2D outer-product
kernel, so this matches lowering.shaders.gaussian_blur up to float order + 8-bit
quantization.
"""
from __future__ import annotations
import numpy as np

from runtime import sources
from lowering.lower import RuntimePlan


def _sepblur(img: np.ndarray, w1d: list[float], R: int) -> np.ndarray:
    H, W, _ = img.shape
    w = np.asarray(w1d, np.float32)
    px = np.pad(img, ((0, 0), (R, R), (0, 0)), mode="edge")
    ox = np.zeros_like(img)
    for k in range(2 * R + 1):
        ox += w[k] * px[:, k:k + W, :]
    py = np.pad(ox, ((R, R), (0, 0), (0, 0)), mode="edge")
    out = np.zeros_like(img)
    for k in range(2 * R + 1):
        out += w[k] * py[k:k + H, :, :]
    return out


def run(plan: RuntimePlan) -> np.ndarray:
    img_of: dict[str, np.ndarray] = {}
    for st in plan.steps:
        if st.kind == "source":
            img_of[st.node_id] = sources.load(st.params)
        elif st.kind == "passthrough":
            src = st.inputs[0] if st.inputs else None
            img_of[st.node_id] = (img_of[src] if src in img_of
                                  else np.zeros((st.target["h"], st.target["w"], 4), np.float32))
        elif st.kind == "shader":
            if st.op == "gaussian_blur":
                img_of[st.node_id] = _sepblur(img_of[st.inputs[0]],
                                              st.params["_weights"], st.params["_radius"])
            else:
                raise NotImplementedError(f"no CPU kernel for {st.op!r} (GL-only)")
    out = np.clip(img_of[plan.output_id], 0.0, 1.0)
    return (out * 255.0 + 0.5).astype(np.uint8)
