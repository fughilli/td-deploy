"""camstream: the laptop side of the player's virtual camera. Decodes a looping
file source and MJPEG-encodes it across the loop boundary (the pts restart that
once broke the encoder); needs PyAV (skipped without it). The SSH tunnel + feeder
path is exercised by hand against a player (see deploy/nix/virtual-camera.nix)."""

import itertools
import os
import tempfile
from fractions import Fraction

import pytest

av = pytest.importorskip("av")

from deploy_engine import camstream  # noqa: E402


def _clip(path, n=6, size=(64, 48)):
    import numpy as np

    with av.open(path, "w") as out:
        s = out.add_stream("mpeg4", rate=30)
        s.width, s.height, s.pix_fmt = size[0], size[1], "yuv420p"
        for i in range(n):
            img = np.full((size[1], size[0], 3), i * 30 % 255, np.uint8)
            for pkt in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(pkt)
        for pkt in s.encode():
            out.mux(pkt)


def test_file_loops_scaled_and_encodes_across_the_loop():
    pytest.importorskip("numpy")
    path = os.path.join(tempfile.mkdtemp(), "clip.mp4")
    _clip(path, n=6)
    got = list(itertools.islice(camstream.frames(path, 160, 90, 30), 15))  # > 2 loops
    assert len(got) == 15
    assert all((f.width, f.height, f.format.name) == (160, 90, "yuvj420p") for f in got)
    enc = av.CodecContext.create("mjpeg", "w")
    enc.width, enc.height, enc.pix_fmt, enc.time_base = 160, 90, "yuvj420p", Fraction(1, 30)
    jpegs = []
    for n, f in enumerate(got):
        f.pts, f.time_base = n, enc.time_base
        jpegs += [bytes(p) for p in enc.encode(f)]
    assert len(jpegs) == 15 and all(j[:2] == b"\xff\xd8" for j in jpegs)


class _EagainContainer:
    """A live device that says EAGAIN before each frame (AVFoundation does)."""

    def __init__(self, frames):
        self.frames, self.calls, self.ready = list(frames), 0, False

    def demux(self, stream):
        self.calls += 1
        while self.frames:
            if not self.ready:  # the read before each frame finds nothing yet
                self.ready = True
                raise BlockingIOError(35, "Resource temporarily unavailable")
            self.ready = False
            yield _Pkt(self.frames.pop(0))


class _Pkt:
    def __init__(self, f):
        self.f = f

    def decode(self):
        return [self.f]


def test_decoded_rides_out_eagain():
    c = _EagainContainer(["a", "b", "c"])
    assert list(camstream._decoded(c, None)) == ["a", "b", "c"]
    assert c.calls > 1  # it demuxed again after EAGAIN
