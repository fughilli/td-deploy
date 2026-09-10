"""GL backend — one-shot render at t=0. Shares the persistent renderer's code path
so the one-shot and realtime paths never diverge. See runtime/renderer.py."""

from runtime.renderer import run  # noqa: F401
