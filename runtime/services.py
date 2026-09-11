"""
I/O services — the "graph declares its runtime interface" model (docs §6).

An OSC In / MIDI In CHOP in the project becomes a live input service here: it owns
a socket/device and writes channel values into a shared ChopStore. Parameter
expressions like `op('osc1')['rot']` read that store each frame (see runtime.expr),
so external control drives the render in realtime.

OSC parsing is hand-rolled (no dependency). MIDI uses `mido` if present and a port
is available; otherwise it degrades gracefully (there's no MIDI device in the
container — on the Pi a USB controller shows up and this starts working).
"""

from __future__ import annotations

import socket
import struct
import threading


class ChopStore:
    """Thread-safe {chop_name: {channel: float}}."""

    def __init__(self):
        self._lock = threading.Lock()
        self._d: dict[str, dict[str, float]] = {}

    def set(self, name: str, chan: str, val: float) -> None:
        with self._lock:
            self._d.setdefault(name, {})[chan] = float(val)

    def get_chan(self, name: str, chan: str, default: float = 0.0) -> float:
        with self._lock:
            return self._d.get(name, {}).get(str(chan), default)

    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._d.items()}


# ---- OSC (hand-rolled) --------------------------------------------------------
def _osc_string(data: bytes, i: int) -> tuple[str, int]:
    end = data.index(b"\0", i)
    s = data[i:end].decode("ascii", "replace")
    i = end + 1
    i += (4 - (i % 4)) % 4
    return s, i


def parse_osc(data: bytes) -> list[tuple[str, list]]:
    """Parse one OSC message -> [(address, [args])]. Bundles are unwrapped."""
    if data[:8] == b"#bundle\0":
        out = []
        i = 16  # skip '#bundle\0' + 8-byte timetag
        while i < len(data):
            (size,) = struct.unpack_from(">i", data, i)
            i += 4
            out += parse_osc(data[i : i + size])
            i += size
        return out
    if not data[:1] == b"/":
        return []
    addr, i = _osc_string(data, 0)
    if i >= len(data) or data[i : i + 1] != b",":
        return [(addr, [])]
    tags, i = _osc_string(data, i)
    args = []
    for t in tags[1:]:
        if t == "f":
            (v,) = struct.unpack_from(">f", data, i)
            i += 4
            args.append(v)
        elif t == "i":
            (v,) = struct.unpack_from(">i", data, i)
            i += 4
            args.append(v)
        elif t == "d":
            (v,) = struct.unpack_from(">d", data, i)
            i += 8
            args.append(v)
        elif t in "TF":
            args.append(1.0 if t == "T" else 0.0)
        elif t == "s":
            _s, i = _osc_string(data, i)
            args.append(_s)
    return [(addr, args)]


class OscInService:
    def __init__(self, name: str, port: int, store: ChopStore):
        self.name, self.port, self.store = name, int(port), store

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        threading.Thread(target=self._loop, args=(sock,), daemon=True).start()

    def _loop(self, sock: socket.socket) -> None:
        while True:
            data, _ = sock.recvfrom(65535)
            for addr, args in parse_osc(data):
                base = addr.strip("/").replace("/", "_") or "ch"
                nums = [a for a in args if isinstance(a, (int, float))]
                if len(nums) == 1:
                    self.store.set(self.name, base, nums[0])
                else:
                    for k, v in enumerate(nums):
                        self.store.set(self.name, f"{base}{k+1}", v)

    def __repr__(self):
        return f"OscIn({self.name!r} udp:{self.port})"


class MidiInService:
    """MIDI In via mido (if available). Maps note-on velocity and CC value to
    channels 'n<note>' and 'cc<num>' (normalized 0..1). Degrades if no backend/port."""

    def __init__(self, name: str, device: str | None, store: ChopStore):
        self.name, self.device, self.store = name, device, store

    def start(self) -> None:
        import mido  # raises if unavailable -> caller reports

        names = mido.get_input_names()
        port = self.device if self.device in names else (names[0] if names else None)
        if port is None:
            raise RuntimeError(f"no MIDI input ports (available: {names})")
        inport = mido.open_input(port)
        threading.Thread(target=self._loop, args=(inport,), daemon=True).start()

    def _loop(self, inport) -> None:
        for msg in inport:
            if msg.type == "control_change":
                self.store.set(self.name, f"cc{msg.control}", msg.value / 127.0)
            elif msg.type == "note_on":
                self.store.set(self.name, f"n{msg.note}", msg.velocity / 127.0)
            elif msg.type == "note_off":
                self.store.set(self.name, f"n{msg.note}", 0.0)

    def __repr__(self):
        return f"MidiIn({self.name!r} dev:{self.device})"


class ServiceManager:
    def __init__(self, specs: list[dict], store: ChopStore):
        self.store = store
        self.services = []
        for sp in specs or []:
            t = sp.get("type")
            if t == "oscin":
                self.services.append(OscInService(sp["name"], sp.get("port", 7000), store))
            elif t == "midiin":
                self.services.append(MidiInService(sp["name"], sp.get("device"), store))

    def start(self) -> None:
        for s in self.services:
            try:
                s.start()
                print(f"[service] started {s}")
            except Exception as e:  # noqa: BLE001
                print(f"[service] {s} unavailable: {e}")


def collect_services(graph) -> list[dict]:
    """Derive service specs from I/O CHOP nodes in the graph plus any graph.services."""
    specs = list(getattr(graph, "services", []) or [])
    for nid, n in graph.nodes.items():
        if n.op in ("oscin", "midiin"):
            name = nid.split("/")[-1]
            if n.op == "oscin":
                specs.append(
                    {"type": "oscin", "name": name, "port": int(n.params.get("port", 7000))}
                )
            else:
                specs.append({"type": "midiin", "name": name, "device": n.params.get("device")})
    return specs
