"""
Realtime MJPEG stream of a toxc plan — view live in a browser window on the Mac.

A dedicated render thread owns the GL context, renders frames on a wall-clock
timebase (so animated params move), and publishes the latest JPEG. HTTP handlers
(any thread) just serve the latest frame, so multiple viewers + the page work.

Routes:
    /            fullscreen <img> viewer page
    /stream      multipart/x-mixed-replace MJPEG (the live video)
    /frame.jpg   the latest single frame
    /stats       json {fps, frame, seconds}
"""
from __future__ import annotations
import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

from lowering.lower import RuntimePlan
from runtime.renderer import Renderer


class _Shared:
    def __init__(self):
        self.lock = threading.Condition()
        self.jpeg: bytes | None = None
        self.frame = 0
        self.seconds = 0.0
        self.fps = 0.0


def _render_loop(plan: RuntimePlan, shared: _Shared, fps_cap: float, quality: int, chops):
    renderer = Renderer(plan, chops=chops)    # context becomes current in THIS thread
    t0 = time.monotonic()
    last = t0
    frame = 0
    period = 1.0 / fps_cap if fps_cap > 0 else 0.0
    while True:
        now = time.monotonic()
        t = now - t0
        rgba = renderer.render(t, frame)
        im = Image.fromarray(rgba, "RGBA").convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        dt = now - last
        last = now
        with shared.lock:
            shared.jpeg = buf.getvalue()
            shared.frame = frame
            shared.seconds = t
            shared.fps = (1.0 / dt) if dt > 0 else 0.0
            shared.lock.notify_all()
        frame += 1
        if period:
            sleep = period - (time.monotonic() - now)
            if sleep > 0:
                time.sleep(sleep)


_PAGE = b"""<!doctype html><html><head><meta charset=utf-8><title>toxc live</title>
<style>html,body{margin:0;height:100%;background:#111;display:flex;align-items:center;
justify-content:center}img{max-width:100vw;max-height:100vh;image-rendering:pixelated}
#f{position:fixed;top:8px;left:10px;color:#6f6;font:12px monospace;opacity:.8}</style></head>
<body><img src="/stream"><div id=f></div>
<script>setInterval(async()=>{try{let s=await(await fetch('/stats')).json();
document.getElementById('f').textContent=`frame ${s.frame} | ${s.seconds.toFixed(1)}s | ${s.fps.toFixed(1)} fps`;}catch(e){}},500);</script>
</body></html>"""


def make_handler(shared: _Shared):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/stream"):
                return self._stream()
            if self.path.startswith("/frame.jpg"):
                return self._frame()
            if self.path.startswith("/stats"):
                return self._stats()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)

        def _wait_frame(self, last):
            with shared.lock:
                while shared.frame == last or shared.jpeg is None:
                    shared.lock.wait(timeout=5)
                return shared.jpeg, shared.frame

        def _frame(self):
            with shared.lock:
                data = shared.jpeg
            if not data:
                self.send_error(503); return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stats(self):
            import json
            with shared.lock:
                body = json.dumps({"frame": shared.frame, "seconds": shared.seconds,
                                   "fps": shared.fps}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = -1
            try:
                while True:
                    data, last = self._wait_frame(last)
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(data)).encode()
                                     + b"\r\n\r\n" + data + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return H


def serve(plan: RuntimePlan, host: str = "0.0.0.0", port: int = 8788,
          fps: float = 30.0, quality: int = 80, chops=None) -> None:
    shared = _Shared()
    threading.Thread(target=_render_loop, args=(plan, shared, fps, quality, chops),
                     daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(shared))
    print(f"[stream] live MJPEG on http://{host}:{port}/  (fps cap {fps}, {plan.target})")
    httpd.serve_forever()
