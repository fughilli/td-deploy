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

from lowering.lower import RuntimePlan
from runtime import sources


def _sepblur(img: np.ndarray, w1d: list[float], R: int) -> np.ndarray:
    H, W, _ = img.shape
    w = np.asarray(w1d, np.float32)
    px = np.pad(img, ((0, 0), (R, R), (0, 0)), mode="edge")
    ox = np.zeros_like(img)
    for k in range(2 * R + 1):
        ox += w[k] * px[:, k : k + W, :]
    py = np.pad(ox, ((R, R), (0, 0), (0, 0)), mode="edge")
    out = np.zeros_like(img)
    for k in range(2 * R + 1):
        out += w[k] * py[k : k + H, :, :]
    return out


# Mirrors lowering.shaders._MATH_COMBINE so GL and CPU fold inputs identically.
_MATH_FOLD = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "subtract": lambda a, b: a - b,
    "mult": lambda a, b: a * b,
    "multiply": lambda a, b: a * b,
    "div": lambda a, b: a / np.maximum(b, 1e-6),
    "divide": lambda a, b: a / np.maximum(b, 1e-6),
    "max": np.maximum,
    "maximum": np.maximum,
    "min": np.minimum,
    "minimum": np.minimum,
    "diff": lambda a, b: np.abs(a - b),
    "difference": lambda a, b: np.abs(a - b),
    "average": lambda a, b: a + b,
}


def _combine_of(st) -> str:
    c = str(st.params.get("op", "add")).split()
    c = c[0].strip('"').lower() if c else "add"
    if c in ("no_op", "off", ""):
        c = "add"
    return c if c in _MATH_FOLD else "add"


def _tu(st, name: str, default: float) -> float:
    """A math-TOP scalar from the step's time_uniforms. The CPU oracle renders a
    single frame at t=0, so only literal (non-expression) values are honored."""
    spec = st.time_uniforms.get(name)
    if spec is None:
        return default
    try:
        return float(spec["expr"]) * spec.get("mul", 1.0)
    except (TypeError, ValueError):
        return default


def run(plan: RuntimePlan) -> np.ndarray:
    img_of: dict[str, np.ndarray] = {}
    for st in plan.steps:
        if st.kind == "source":
            img_of[st.node_id] = sources.load(st.params)
        elif st.kind == "passthrough":
            src = st.inputs[0] if st.inputs else None
            img_of[st.node_id] = (
                img_of[src]
                if src in img_of
                else np.zeros((st.target["h"], st.target["w"], 4), np.float32)
            )
        elif st.kind == "shader":
            if st.op == "gaussian_blur":
                img_of[st.node_id] = _sepblur(
                    img_of[st.inputs[0]], st.params["_weights"], st.params["_radius"]
                )
            elif st.op == "add":
                acc = img_of[st.inputs[0]].copy()
                for src in st.inputs[1:]:
                    acc = acc + img_of[src]
                img_of[st.node_id] = acc
            elif st.op == "math":
                acc = img_of[st.inputs[0]].copy()
                for src in st.inputs[1:]:
                    acc = _MATH_FOLD[_combine_of(st)](acc, img_of[src])
                if _combine_of(st) == "average" and len(st.inputs) > 1:
                    acc = acc / float(len(st.inputs))
                pre = _tu(st, "uPreOff", 0.0)
                gain = _tu(st, "uGain", 1.0)
                post = _tu(st, "uPostOff", 0.0)
                img_of[st.node_id] = (acc + pre) * gain + post
            else:
                raise NotImplementedError(f"no CPU kernel for {st.op!r} (GL-only)")
    out = np.clip(img_of[plan.output_id], 0.0, 1.0)
    return (out * 255.0 + 0.5).astype(np.uint8)
