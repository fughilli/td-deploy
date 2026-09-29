"""Stream a camera, video file or URL from this machine into a player's virtual
camera (/dev/video10, deploy/nix/virtual-camera.nix) — for testing camera-driven
pieces on a player without a webcam plugged into it.

    from deploy_engine import camstream
    camstream.run("tricksterplayer", source="0")          # this laptop's camera
    camstream.run("tricksterplayer", source="clip.mp4")   # a recording, looped

Frames are decoded with PyAV (FFmpeg): a camera index opens the platform capture
device (AVFoundation / V4L2 / DirectShow), anything else is opened as a file or
URL (files loop). Each frame is scaled, JPEG-encoded and written to the player's
feeder over an SSH local port-forward using the deploy key — the feeder only
listens on the player's loopback, and nothing listens on this machine.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from fractions import Fraction

from .progress import Progress
from .push import _ssh_base, default_key

FEEDER_PORT = 8090  # td-camfeed.service, on the player's 127.0.0.1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def open_source(source: str, width: int, height: int, fps: int):
    """(container, is_live) for a camera index, file or URL."""
    import av

    if source.isdigit():
        if sys.platform == "darwin":
            # AVFoundation wants a size/rate the camera offers; try the common
            # pixel formats (FaceTime cameras: nv12 / uyvy422 / yuyv422).
            last = None
            for pix in ("nv12", "uyvy422", "yuyv422", None):
                opts = {"framerate": str(fps), "video_size": f"{width}x{height}"}
                if pix:
                    opts["pixel_format"] = pix
                try:
                    return av.open(source, format="avfoundation", options=opts), True
                except Exception as e:  # noqa: BLE001 - try the next format
                    last = e
            raise RuntimeError(f"could not open camera {source}: {last}")
        if sys.platform.startswith("linux"):
            opts = {
                "framerate": str(fps),
                "video_size": f"{width}x{height}",
                "input_format": "mjpeg",
            }
            return av.open(f"/dev/video{source}", format="v4l2", options=opts), True
        return av.open(f"video={source}", format="dshow"), True
    live = "://" in source and not source.startswith("file://")
    return (
        av.open(source, options={"rtsp_transport": "tcp"} if source.startswith("rtsp") else {}),
        live,
    )


def frames(source: str, width: int, height: int, fps: int, loop: bool = True):
    """Yield scaled yuvj420p frames, paced to the source rate for files."""
    while True:
        container, live = open_source(source, width, height, fps)
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        rate = float(stream.average_rate or fps) or fps
        t0, n = time.time(), 0
        for frame in container.decode(stream):
            yield frame.reformat(width=width, height=height, format="yuvj420p")
            n += 1
            if not live:
                delay = t0 + n / rate - time.time()
                if delay > 0:
                    time.sleep(delay)
        container.close()
        if live or not loop:
            return


def run(
    host: str,
    *,
    source: str = "0",
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    quality: int = 5,
    user: str = "root",
    key: str | None = None,
    progress: Progress = Progress(),
) -> None:
    """Stream until interrupted (Ctrl-C) or the source ends (a live one)."""
    import av

    key = key or default_key()
    lport = _free_port()
    tunnel = subprocess.Popen(
        _ssh_base(key)
        + ["-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=10"]
        + ["-L", f"127.0.0.1:{lport}:127.0.0.1:{FEEDER_PORT}", f"{user}@{host}"],
        stdin=subprocess.DEVNULL,
    )
    try:

        def connect():
            for _ in range(100):  # the tunnel / a restarting feeder takes a moment
                if tunnel.poll() is not None:
                    raise RuntimeError(
                        f"ssh tunnel to {user}@{host} failed (exit {tunnel.returncode})"
                    )
                try:
                    c = socket.create_connection(("127.0.0.1", lport), timeout=2)
                    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    return c
                except OSError:
                    time.sleep(0.1)
            raise RuntimeError("could not reach the player's camera feeder through the tunnel")

        sock = connect()
        enc = av.CodecContext.create("mjpeg", "w")
        enc.width, enc.height, enc.pix_fmt = width, height, "yuvj420p"
        enc.time_base = Fraction(1, fps)
        enc.options = {"qmin": str(quality), "qmax": str(quality)}
        progress.log(f"streaming {source} -> {host}:/dev/video10 ({width}x{height}@{fps})")
        sent, t_rep, nbytes = 0, time.time(), 0
        for n, frame in enumerate(frames(source, width, height, fps)):
            # our own monotonic clock: a looping file (or a camera) restarts its pts
            frame.pts, frame.time_base = n, enc.time_base
            for pkt in enc.encode(frame):
                data = bytes(pkt)
                try:
                    sock.sendall(data)
                except OSError:  # the feeder restarted (e.g. the player rebooted it)
                    progress.log("feeder disconnected; reconnecting")
                    sock.close()
                    sock = connect()
                    continue
                nbytes += len(data)
            sent += 1
            now = time.time()
            if now - t_rep >= 5:
                progress.log(
                    f"{sent / (now - t_rep):.1f} fps, {nbytes / (now - t_rep) / 1e6:.2f} MB/s"
                )
                sent, nbytes, t_rep = 0, 0, now
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()


__all__ = ["run", "frames", "open_source", "FEEDER_PORT"]
