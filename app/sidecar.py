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
  {"cmd":"list_releases"}                               # enumerate base-image releases
  {"cmd":"flash","disk_id":"...","tag":"latest",       # download base img + flash SD
      "hostname":"tdplayer","networks":[{"ssid":..,"psk":..}]}  # optional per-card config
      # the ACTIVE deploy key's public line is added automatically -> the card's
      # /boot/firmware/authorized_keys, so the flashed Pi trusts it at first boot.
  {"cmd":"gen_deploy_key","name":"...","comment":null}  # new ed25519 keypair, made active
  {"cmd":"list_deploy_keys"}                            # names + fingerprints + active
  {"cmd":"select_deploy_key","name":"..."}             # set the active key
  {"cmd":"ping"}

Events (stdout):
  {"type":"ready"} {"type":"settings",...} {"type":"start","toe":...}
  {"type":"deploy_keys","keys":[{"name":..,"fingerprint":..,"active":bool}],"active":..}
  {"type":"deploy_key_generated","name":..,"fingerprint":..,"pub":..}
  {"type":"progress","phase":...,"frac":..,"overall":..,"message":...}
  {"type":"log","line":...} {"type":"done","ok":true,"staging":...}
  {"type":"error","message":...,"fixPrompt":...} {"type":"watch","enabled":bool}
  {"type":"warning","message":...,"fixPrompt":...}   # deployed with substitutions
  {"type":"assets","missing":[{"path":...,"node":...,"searched":[...]}]} {"type":"pong"}
  {"type":"disks","disks":[{"id":..,"name":..,"size_gb":..,"bus":..}]}
  {"type":"releases","releases":[{"tag_name":..,"name":..,"published_at":..,"prerelease":bool}]}
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
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_engine import Progress, deploy  # noqa: E402
from deploy_engine.fixit import UnsupportedOperatorError, build_fix_prompt  # noqa: E402
from deploy_engine.progress import PHASES  # noqa: E402

_out_lock = threading.Lock()


def emit(obj: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _error_event(evt_type: str, exc: Exception, *, action: str, **ctx) -> dict:
    """Build an error event carrying a copy-paste 'fix and file' agent prompt (message +
    trace + context), so the UI can offer a one-click fix-it button."""
    unsupported = exc.operators if isinstance(exc, UnsupportedOperatorError) else None
    prompt = build_fix_prompt(
        str(exc),
        traceback.format_exc(),
        action=action,
        unsupported=unsupported,
        **ctx,
    )
    return {
        "type": evt_type,
        "message": str(exc),
        "errorKind": "unsupported_operator" if unsupported else "error",
        "fixPrompt": prompt,
    }


def _warning_event(unsupported, chops, magic, *, toe=None, target=None, version=None):
    """A non-blocking warning for a deploy that succeeded but with substitutions —
    unsupported TOP operators degraded to placeholders and/or unsupported CHOPs resolving
    to 0 (or driven by magic sinusoids). Carries the same 'fix and file' prompt. Returns
    None when there's nothing to warn about."""
    if not unsupported and not chops:
        return None
    bits = []
    if unsupported:
        bits.append(f"operator(s) replaced by placeholders: {', '.join(unsupported)}")
    if chops:
        driver = "driven by magic sinusoids" if magic else "resolving to 0"
        bits.append(f"CHOP(s) {driver}: {', '.join(chops)}")
    message = "Deployed with unsupported " + "; ".join(bits) + "."
    prompt = build_fix_prompt(
        message,
        "",
        action="deploying your project",
        toe=toe,
        target=target,
        version=version,
        unsupported=unsupported or None,
        chops=chops or None,
    )
    return {
        "type": "warning",
        "message": message,
        "errorKind": "unsupported_operator",
        "fixPrompt": prompt,
    }


def _overall(phase: str, frac: float) -> float:
    order = {n: i for i, (n, _) in enumerate(PHASES)}
    done = sum(w for n, w in PHASES if order.get(n, 1e9) < order.get(phase, -1))
    cur = next((w for n, w in PHASES if n == phase), 0.0)
    total = sum(w for _, w in PHASES)
    return (done + cur * max(0.0, min(1.0, frac))) / total


def _base_image_tag() -> str:
    """The base image tag this build was stamped with (build_app writes version.json
    next to the frozen bundle / repo root); 'latest' when unstamped."""
    from deploy_engine import _paths

    for cand in (
        os.path.join(_paths.REPO_ROOT, "version.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.json"),
    ):
        try:
            with open(cand) as f:
                return json.load(f).get("base_image_tag") or "latest"
        except (OSError, ValueError):
            continue
    return os.environ.get("TDDEPLOY_BASE_IMAGE_TAG", "latest")


def _default_config_dir() -> str:
    """Where deploy keys live when Electron hasn't passed app.getPath('userData').

    Electron sets `config_dir` in set_settings; this stdlib fallback keeps the
    sidecar usable standalone (dev / CLI / tests). Mirrors the OS conventions
    Electron's userData uses."""
    env = os.environ.get("TDDEPLOY_CONFIG_DIR")
    if env:
        return env
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/td-deploy")
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "td-deploy")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "td-deploy")


class Sidecar:
    def __init__(self) -> None:
        self.settings = {
            "pi": "tdplayer.local",
            "target": "gles2",
            "key": None,
            "set_file": [],
            "bridge": os.environ.get("TOXC_HOST"),
            "user": "root",
            "skip_unsupported": False,  # lenient "warn but continue" mode (TOP ops)
            "magic_chop": False,  # drive unsupported CHOPs with random sinusoids
            "asset_roots": [],  # extra directories to search for movie/image assets
            "asset_map": {},  # explicit substitutions: original path/basename -> local file
            "base_image_tag": _base_image_tag(),
            # Electron passes app.getPath('userData'); deploy keys are stored under it.
            "config_dir": _default_config_dir(),
        }
        self._keystore = None  # lazily built from settings["config_dir"]
        self.toe: str | None = None
        self._deploy_req = threading.Event()
        self._watch = False
        self._watch_stop = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # --- progress -> stdout events ---
    def _progress(self) -> Progress:
        return Progress(
            on_event=lambda ph, fr, msg: emit(
                {
                    "type": "progress",
                    "phase": ph,
                    "frac": fr,
                    "overall": _overall(ph, fr),
                    "message": msg,
                }
            ),
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
                res = deploy(
                    toe,
                    s["pi"],
                    target=s["target"],
                    set_file=s["set_file"],
                    bridge=s["bridge"],
                    user=s["user"],
                    key=s["key"],
                    strict_unsupported=not s.get("skip_unsupported"),
                    magic_chop=bool(s.get("magic_chop")),
                    asset_roots=s.get("asset_roots") or [],
                    asset_map=s.get("asset_map") or {},
                    progress=self._progress(),
                )
                emit({"type": "done", "ok": True, "staging": res["staging"]})
                info = res.get("info") or {}
                missing = info.get("missing_assets") or []
                if missing:
                    # Distinct from the fix-it prompt: a missing local asset is the user's
                    # file, not a code bug — surface it with the roots searched so they can
                    # fix the TD path, add a search folder, or pick a replacement.
                    emit({"type": "assets", "missing": missing})
                # Deploy succeeded but the engine made substitutions — surface a
                # non-blocking warning with the same fix-it prompt.
                warn = _warning_event(
                    info.get("unsupported") or [],
                    info.get("unsupported_chops") or [],
                    info.get("magic_chops") or [],
                    toe=toe,
                    target=s.get("target"),
                    version=s.get("base_image_tag"),
                )
                if warn:
                    emit(warn)
            except Exception as e:  # noqa: BLE001 - surface every failure to the UI
                emit(
                    _error_event(
                        "error",
                        e,
                        action="deploying your project",
                        toe=toe,
                        target=s.get("target"),
                        version=s.get("base_image_tag"),
                    )
                )

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
        if self._watch:
            # Name the file we actually poll. Without this a watch on the wrong path
            # (or on nothing at all) is indistinguishable from a broken watcher.
            emit(
                {
                    "type": "log",
                    "line": (
                        f"watching {os.path.abspath(self.toe)}"
                        if self.toe
                        else "watch enabled, but no project is selected yet — pick a .toe"
                    ),
                }
            )

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
    def _flash_worker(
        self,
        disk_id: str,
        tag: str,
        image: str | None,
        hostname: str | None = None,
        networks: list | None = None,
        authorized_keys: list | None = None,
    ) -> None:
        from deploy_engine import download, flasher

        try:
            disk = flasher.require_removable(disk_id)  # confirm before any work
            emit({"type": "flash_start", "disk": disk.to_dict(), "tag": tag})
            if not image:
                emit({"type": "log", "line": f"fetching base image {tag}"})
                image = download.fetch_base_image(
                    tag,
                    on_progress=lambda f, m: emit(
                        {"type": "flash_progress", "stage": "download", "frac": f, "message": m}
                    ),
                )
            emit({"type": "log", "line": f"writing {image} -> {disk.name}"})
            flasher.flash(
                image,
                disk_id,
                on_progress=lambda f, m: emit(
                    {"type": "flash_progress", "stage": "write", "frac": f, "message": m}
                ),
                hostname=hostname or None,
                networks=networks or None,
                authorized_keys=authorized_keys or None,
                # Boot-config drop is best-effort: the raw write already succeeded,
                # so a failure here is a warning, not a flash error.
                on_warn=lambda m: emit({"type": "warning", "message": m}),
            )
            emit({"type": "flash_done", "disk": disk.to_dict()})
        except Exception as e:  # noqa: BLE001 - surface to UI
            emit(_error_event("flash_error", e, action="flashing an SD card"))

    def start_flash(
        self,
        disk_id: str,
        tag: str,
        image: str | None,
        hostname: str | None = None,
        networks: list | None = None,
    ) -> None:
        # Include the ACTIVE deploy key's public line so the flashed card's
        # /boot/firmware/authorized_keys trusts it at first boot (image half).
        authorized_keys = None
        try:
            active_pub = self._keystore_for().active_public_line()
            if active_pub:
                authorized_keys = [active_pub]
        except Exception:  # noqa: BLE001 - no active key just means none written
            authorized_keys = None
        threading.Thread(
            target=self._flash_worker,
            args=(disk_id, tag, image, hostname, networks, authorized_keys),
            daemon=True,
        ).start()

    def list_disks(self) -> None:
        from deploy_engine import flasher

        try:
            emit({"type": "disks", "disks": [d.to_dict() for d in flasher.list_disks()]})
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "message": f"list_disks: {e}"})

    def list_releases(self) -> None:
        """Enumerate the base-image releases for the flash picker. On any network
        failure emit an empty list (with a message) so the UI stays usable with the
        CI-stamped default tag rather than getting no event at all."""
        from deploy_engine import releases

        try:
            rels = releases.list_releases()
            emit({"type": "releases", "releases": rels})
        except Exception as e:  # noqa: BLE001 - surface to UI but keep it non-fatal
            emit({"type": "releases", "releases": [], "message": f"list_releases: {e}"})

    # --- deploy keys (pure-Python ed25519; stored under config_dir) ---
    def _keystore_for(self):
        """Build (and cache) a KeyStore rooted at the current config_dir. Rebuilt
        if config_dir changed (Electron sends it after userData is known)."""
        from deploy_engine.deploy_keys import KeyStore

        cfg = self.settings.get("config_dir") or _default_config_dir()
        if self._keystore is None or getattr(self._keystore, "_root", None) != cfg:
            ks = KeyStore(cfg)
            ks._root = cfg  # remember for the change check
            self._keystore = ks
        return self._keystore

    def _sync_active_key(self) -> None:
        """Point the deploy-time `key` setting at the active key's private half, so a
        deploy uses the same key the flasher writes to the card."""
        try:
            priv = self._keystore_for().active_private_path()
        except Exception:  # noqa: BLE001 - never let key store errors break deploy
            priv = None
        if priv:
            self.settings["key"] = priv

    def emit_deploy_keys(self) -> None:
        try:
            ks = self._keystore_for()
            emit({"type": "deploy_keys", "keys": ks.list(), "active": ks.active()})
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "message": f"list_deploy_keys: {e}"})

    def gen_deploy_key(self, name: str, comment: str | None) -> None:
        try:
            ks = self._keystore_for()
            info = ks.generate(name, comment=comment)
            self._sync_active_key()  # newly generated key is active -> use it for deploy
            emit(
                {
                    "type": "deploy_key_generated",
                    "name": info["name"],
                    "fingerprint": info["fingerprint"],
                    "pub": info["pub"],
                }
            )
            self.emit_deploy_keys()
            emit({"type": "settings", "settings": self.settings})
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "message": f"gen_deploy_key: {e}"})

    def select_deploy_key(self, name: str) -> None:
        try:
            self._keystore_for().select(name)
            self._sync_active_key()
            self.emit_deploy_keys()
            emit({"type": "settings", "settings": self.settings})
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "message": f"select_deploy_key: {e}"})

    # --- command dispatch ---
    def handle(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "set_settings":
            self.settings.update(msg.get("settings", {}))
            # config_dir may have just arrived (Electron's userData) — if there's an
            # active key, point the deploy `key` at its private half.
            self._sync_active_key()
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
        elif cmd == "list_releases":
            self.list_releases()
        elif cmd == "flash":
            self.start_flash(
                msg["disk_id"],
                msg.get("tag", "latest"),
                msg.get("image"),
                msg.get("hostname"),
                msg.get("networks"),
            )
        elif cmd == "gen_deploy_key":
            self.gen_deploy_key(msg.get("name"), msg.get("comment"))
        elif cmd == "list_deploy_keys":
            self.emit_deploy_keys()
        elif cmd == "select_deploy_key":
            self.select_deploy_key(msg.get("name"))
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
