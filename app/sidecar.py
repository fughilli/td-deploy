#!/usr/bin/env python3
"""td-deploy Studio sidecar: the long-lived process Electron talks to.

Protocol: newline-delimited JSON. Electron writes one command object per line on
stdin; the sidecar writes event objects on stdout.

Commands (stdin):
  {"cmd":"set_settings","settings":{"pi":"tdplayer.local","target":"gles2",
      "key":null,"set_file":[...],"bridge":null}}
  {"cmd":"pick_toe","toe":"/path/project.toe"}        # set the current project
  {"cmd":"deploy"}                                     # deploy the current .toe now
  {"cmd":"watch","enable":true}                        # auto-deploy on save
  {"cmd":"list_disks"}                                 # enumerate removable disks
  {"cmd":"flash","disk_id":"...","tag":"latest"}       # download base img + flash SD
  {"cmd":"ping"}

Events (stdout):
  {"type":"ready"} {"type":"settings",...} {"type":"start","toe":...}
  {"type":"progress","phase":...,"frac":..,"overall":..,"message":...}
  {"type":"log","line":...} {"type":"done","ok":true,"staging":...}
  {"type":"error","message":...} {"type":"watch","enabled":bool} {"type":"pong"}
  {"type":"disks","disks":[{"id":..,"name":..,"size_gb":..,"bus":..}]}
  {"type":"flash_start","disk":..,"tag":..}
  {"type":"flash_progress","stage":"download|write","frac":..,"message":..}
  {"type":"flash_done","disk":..} {"type":"flash_error","message":..}

Re-entry: `sidecar --raw-write <image> <device> <progress_file>` runs the tiny
privileged raw-write worker (see deploy_engine.flasher.rawwrite) — this is how the
frozen binary flashes as root without shipping a separate interpreter.

All engine work runs on a single worker thread (coalescing: rapid saves collapse to
one deploy); stdout writes are serialized so events never interleave.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_engine import Progress, deploy  # noqa: E402
from deploy_engine.progress import PHASES  # noqa: E402

_out_lock = threading.Lock()


def emit(obj: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _overall(phase: str, frac: float) -> float:
    order = {n: i for i, (n, _) in enumerate(PHASES)}
    done = sum(w for n, w in PHASES if order.get(n, 1e9) < order.get(phase, -1))
    cur = next((w for n, w in PHASES if n == phase), 0.0)
    total = sum(w for _, w in PHASES)
    return (done + cur * max(0.0, min(1.0, frac))) / total


class Sidecar:
    def __init__(self) -> None:
        self.settings = {
            "pi": "tdplayer.local", "target": "gles2", "key": None,
            "set_file": [], "bridge": os.environ.get("TOXC_HOST"),
            "user": "root",
        }
        self.toe: str | None = None
        self._deploy_req = threading.Event()
        self._watch = False
        self._watch_stop = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # --- progress -> stdout events ---
    def _progress(self) -> Progress:
        return Progress(
            on_event=lambda ph, fr, msg: emit({
                "type": "progress", "phase": ph, "frac": fr,
                "overall": _overall(ph, fr), "message": msg}),
            on_log=lambda line: emit({"type": "log", "line": line.rstrip()}),
        )

    # --- deploy worker (coalescing) ---
    def _worker(self) -> None:
        while True:
            self._deploy_req.wait()
            self._deploy_req.clear()
            toe = self.toe
            if not toe:
                continue
            s = dict(self.settings)
            emit({"type": "start", "toe": toe})
            try:
                res = deploy(toe, s["pi"], target=s["target"], set_file=s["set_file"],
                             bridge=s["bridge"], user=s["user"], key=s["key"],
                             progress=self._progress())
                emit({"type": "done", "ok": True, "staging": res["staging"]})
            except Exception as e:  # noqa: BLE001 - surface every failure to the UI
                emit({"type": "error", "message": str(e)})

    def request_deploy(self) -> None:
        self._deploy_req.set()

    # --- file watcher (mtime poll + debounce; no third-party dep) ---
    def _set_watch(self, enable: bool) -> None:
        if enable and not self._watch:
            self._watch = True
            self._watch_stop.clear()
            threading.Thread(target=self._watch_loop, daemon=True).start()
        elif not enable:
            self._watch = False
            self._watch_stop.set()
        emit({"type": "watch", "enabled": self._watch})

    def _watch_loop(self) -> None:
        last = self._mtime()
        settle = None
        while self._watch and not self._watch_stop.wait(0.4):
            m = self._mtime()
            if m != last:
                last = m
                settle = time.monotonic() + 0.5  # debounce TD's multi-write save
            elif settle and time.monotonic() >= settle:
                settle = None
                emit({"type": "log", "line": "saved -> deploy"})
                self.request_deploy()

    def _mtime(self) -> float:
        try:
            return os.path.getmtime(self.toe) if self.toe else 0.0
        except OSError:
            return 0.0

    # --- SD flashing (own thread; download base image then raw-write) ---
    def _flash_worker(self, disk_id: str, tag: str, image: str | None) -> None:
        from deploy_engine import download, flasher
        try:
            disk = flasher.require_removable(disk_id)  # confirm before any work
            emit({"type": "flash_start", "disk": disk.to_dict(), "tag": tag})
            if not image:
                emit({"type": "log", "line": f"fetching base image {tag}"})
                image = download.fetch_base_image(
                    tag, on_progress=lambda f, m: emit({
                        "type": "flash_progress", "stage": "download", "frac": f, "message": m}))
            emit({"type": "log", "line": f"writing {image} -> {disk.name}"})
            flasher.flash(image, disk_id, on_progress=lambda f, m: emit({
                "type": "flash_progress", "stage": "write", "frac": f, "message": m}))
            emit({"type": "flash_done", "disk": disk.to_dict()})
        except Exception as e:  # noqa: BLE001 - surface to UI
            emit({"type": "flash_error", "message": str(e)})

    def start_flash(self, disk_id: str, tag: str, image: str | None) -> None:
        threading.Thread(target=self._flash_worker,
                         args=(disk_id, tag, image), daemon=True).start()

    def list_disks(self) -> None:
        from deploy_engine import flasher
        try:
            emit({"type": "disks", "disks": [d.to_dict() for d in flasher.list_disks()]})
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "message": f"list_disks: {e}"})

    # --- command dispatch ---
    def handle(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "set_settings":
            self.settings.update(msg.get("settings", {}))
            emit({"type": "settings", "settings": self.settings})
        elif cmd == "pick_toe":
            self.toe = msg.get("toe")
            emit({"type": "toe", "toe": self.toe})
        elif cmd == "deploy":
            if msg.get("toe"):
                self.toe = msg["toe"]
            self.request_deploy()
        elif cmd == "watch":
            if msg.get("toe"):
                self.toe = msg["toe"]
            self._set_watch(bool(msg.get("enable")))
        elif cmd == "list_disks":
            self.list_disks()
        elif cmd == "flash":
            self.start_flash(msg["disk_id"], msg.get("tag", "latest"), msg.get("image"))
        elif cmd == "ping":
            emit({"type": "pong"})
        else:
            emit({"type": "error", "message": f"unknown command {cmd!r}"})


def main() -> int:
    # Privileged re-entry: when launched elevated by the flasher, act as the raw
    # writer and nothing else. Must be handled before anything touches stdout.
    if len(sys.argv) >= 2 and sys.argv[1] == "--raw-write":
        from deploy_engine.flasher import rawwrite
        return rawwrite.main(sys.argv[2:])

    sc = Sidecar()
    emit({"type": "ready", "settings": sc.settings})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            sc.handle(json.loads(line))
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "message": f"bad command: {e}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
