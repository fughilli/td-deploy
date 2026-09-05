"""
Movie/video source for `moviefilein` when the file is a video.

Decodes frames up front (bounded by max_frames + max_dim to cap memory) and hands
back the frame for a given wall-clock time with looping. This mirrors TD's Movie
File In playing a clip; the renderer re-uploads the current frame each render(t).

Long clips are truncated (logged) — streaming/seek-based decode is a future
refinement; for realtime preview a bounded in-RAM ring is simplest and smooth.
"""
from __future__ import annotations
import numpy as np

VIDEO_EXTS = (".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg")


def is_video(path: str | None) -> bool:
    return bool(path) and path.lower().endswith(VIDEO_EXTS)


def probe_size(path: str) -> tuple[int, int]:
    import av
    with av.open(path) as c:
        s = c.streams.video[0]
        return int(s.codec_context.width), int(s.codec_context.height)


class VideoSource:
    def __init__(self, path: str, max_frames: int = 300, max_dim: int = 512):
        import av
        from PIL import Image
        self.frames: list[np.ndarray] = []
        with av.open(path) as container:
            stream = container.streams.video[0]
            self.fps = float(stream.average_rate or 30.0)
            truncated = False
            for i, frame in enumerate(container.decode(stream)):
                if i >= max_frames:
                    truncated = True
                    break
                im = Image.fromarray(frame.to_ndarray(format="rgb24")).convert("RGBA")
                w, h = im.size
                scale = min(1.0, max_dim / max(w, h))
                if scale < 1.0:
                    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
                self.frames.append(np.ascontiguousarray(np.asarray(im, np.uint8)))
        if not self.frames:
            raise RuntimeError(f"no frames decoded from {path}")
        self.height, self.width = self.frames[0].shape[:2]
        self.n = len(self.frames)
        self.duration = self.n / self.fps
        note = f" (truncated to {self.n})" if truncated else ""
        print(f"[video] {path}: {self.n} frames @ {self.fps:.1f}fps, "
              f"{self.width}x{self.height}{note}")

    def frame_at(self, t: float) -> np.ndarray:
        return self.frames[int(t * self.fps) % self.n]
