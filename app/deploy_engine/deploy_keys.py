"""Generate and manage SSH deploy keys via `ssh-keygen`.

Keys are ed25519, created with `ssh-keygen -t ed25519 -N ''`; the public
(authorized_keys) line comes from the generated `.pub` (or `ssh-keygen -y` for a
key loaded from elsewhere), and the fingerprint is computed from that line. We
never parse key material ourselves — ssh-keygen owns the crypto. ssh-keygen is
bundled with the app (under the toolchain bundle) and resolved bundled-first,
falling back to the system one (present on macOS and Windows 10+ OpenSSH).

Pairs with the merged image half (deploy/nix/flash-config.nix): the flasher drops
EVERY active key's public line to `/boot/firmware/authorized_keys` (installed into
root's `~/.ssh/authorized_keys` at first boot), and push.py logs in with the
LOGIN key's private half. So a freshly flashed card trusts all active keys, and
the app authenticates with the one designated to log in.

A KeyStore persists keypairs under the app config dir. Two kinds:
  * generated — created here; private + .pub live under <config_dir>/deploy_keys/
  * sourced   — an existing private key elsewhere on disk (loaded from file);
                only its path is recorded, never copied.
Index (deploy_keys/index.json): {"active": [names], "login": name, "sourced": {name: path}}
  active — written to the card's authorized_keys; login — used by ssh to log in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import List, Optional


def _bundled_bin_dir() -> Optional[str]:
    """The toolchain bin dir inside the frozen app (where ssh-keygen is bundled)."""
    base = getattr(sys, "_MEIPASS", None)
    return os.path.join(base, "toolchain", "bin") if base else None


def ssh_keygen() -> str:
    """Path to ssh-keygen: the bundled copy if present, else the system one."""
    name = "ssh-keygen.exe" if sys.platform.startswith("win") else "ssh-keygen"
    bd = _bundled_bin_dir()
    if bd:
        cand = os.path.join(bd, name)
        if os.path.exists(cand):
            return cand
    found = shutil.which(name) or shutil.which("ssh-keygen")
    if not found:
        raise RuntimeError("ssh-keygen not found (not bundled and not on PATH)")
    return found


def fingerprint_of_line(pub_line: str) -> str:
    """OpenSSH-style `SHA256:<base64-no-pad>` fingerprint of an authorized_keys line."""
    try:
        blob = base64.b64decode(pub_line.split()[1])
    except (IndexError, ValueError):
        return ""
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def public_line_from_private(priv_path: str) -> str:
    """The authorized_keys line for a private key: prefer a sibling `.pub`, else
    derive it with `ssh-keygen -y` (works for any key type; needs the key
    unencrypted or loaded in the agent)."""
    pub = priv_path + ".pub"
    if os.path.exists(pub):
        with open(pub) as f:
            line = f.read().strip()
        if line:
            return line
    out = subprocess.run(
        [ssh_keygen(), "-y", "-f", priv_path],
        capture_output=True,
        text=True,
        check=True,
    )
    line = out.stdout.strip()
    if not line:
        raise RuntimeError(f"could not read a public key from {priv_path}")
    return line


_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _safe_name(name: str) -> str:
    """A filesystem-safe key name (no path traversal, no separators)."""
    name = (name or "").strip()
    if not name or any(c not in _NAME_OK for c in name) or name in (".", ".."):
        raise ValueError(f"invalid deploy-key name {name!r}")
    return name


class KeyStore:
    """Keypairs under a config dir: multiple 'active' (trusted on the card) plus a
    single 'login' key (used to ssh in). Keys are generated here or sourced from an
    existing file elsewhere."""

    def __init__(self, config_dir: str) -> None:
        self.dir = os.path.join(config_dir, "deploy_keys")
        os.makedirs(self.dir, exist_ok=True)
        self.index_path = os.path.join(self.dir, "index.json")

    # -- index --
    def _read_index(self) -> dict:
        try:
            with open(self.index_path) as f:
                idx = json.load(f) or {}
        except (OSError, ValueError):
            idx = {}
        idx.setdefault("active", [])
        idx.setdefault("login", None)
        idx.setdefault("sourced", {})
        return idx

    def _write_index(self, idx: dict) -> None:
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(idx, f)

    # -- names / paths --
    def _generated_names(self) -> List[str]:
        out = []
        for fn in os.listdir(self.dir):
            if fn.endswith(".pub") or fn == "index.json":
                continue
            out.append(fn)
        return out

    def _names(self, idx: Optional[dict] = None) -> List[str]:
        idx = idx or self._read_index()
        return sorted(set(self._generated_names()) | set(idx.get("sourced", {})))

    def private_path(self, name: str, idx: Optional[dict] = None) -> str:
        idx = idx or self._read_index()
        src = idx.get("sourced", {})
        if name in src:
            return src[name]
        return os.path.join(self.dir, _safe_name(name))

    def public_path(self, name: str) -> str:
        return os.path.join(self.dir, _safe_name(name) + ".pub")

    def key_dir(self, name: str, idx: Optional[dict] = None) -> str:
        """The directory containing the key (for 'reveal in file manager')."""
        return os.path.dirname(os.path.abspath(self.private_path(name, idx)))

    def _kind(self, name: str, idx: Optional[dict] = None) -> str:
        idx = idx or self._read_index()
        return "sourced" if name in idx.get("sourced", {}) else "generated"

    def public_line(self, name: str, idx: Optional[dict] = None) -> str:
        return public_line_from_private(self.private_path(name, idx))

    # -- queries --
    def active(self) -> List[str]:
        idx = self._read_index()
        names = set(self._names(idx))
        return [n for n in idx.get("active", []) if n in names]

    def login(self) -> Optional[str]:
        idx = self._read_index()
        name = idx.get("login")
        return name if name in set(self._names(idx)) else None

    def list(self) -> List[dict]:
        """[{name, kind, path, dir, active, login, fingerprint, pub}] sorted by name."""
        idx = self._read_index()
        active = set(idx.get("active", []))
        login = idx.get("login")
        out: List[dict] = []
        for name in self._names(idx):
            try:
                pub = self.public_line(name, idx)
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
                pub = ""
            out.append(
                {
                    "name": name,
                    "kind": self._kind(name, idx),
                    "path": self.private_path(name, idx),
                    "dir": self.key_dir(name, idx),
                    "active": name in active,
                    "login": name == login,
                    "fingerprint": fingerprint_of_line(pub) if pub else "",
                    "pub": pub,
                }
            )
        return out

    def active_public_lines(self) -> List[str]:
        """authorized_keys lines for every active key (skips any that can't derive)."""
        idx = self._read_index()
        active = [n for n in idx.get("active", []) if n in set(self._names(idx))]
        lines = []
        for name in active:
            try:
                lines.append(self.public_line(name, idx))
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
                pass
        # de-dupe while preserving order
        seen = set()
        return [ln for ln in lines if not (ln in seen or seen.add(ln))]

    def login_private_path(self) -> Optional[str]:
        name = self.login()
        if not name:
            return None
        p = self.private_path(name)
        return p if os.path.exists(p) else None

    # -- mutations --
    def _add(self, idx: dict, name: str) -> None:
        """Make `name` active, and the login key if there isn't one yet."""
        if name not in idx["active"]:
            idx["active"].append(name)
        if not idx.get("login"):
            idx["login"] = name

    def generate(self, name: str, comment: Optional[str] = None) -> dict:
        name = _safe_name(name)
        idx = self._read_index()
        if name in set(self._names(idx)):
            raise FileExistsError(f"deploy key {name!r} already exists")
        priv = os.path.join(self.dir, name)
        comment = comment if comment is not None else name
        subprocess.run(
            [ssh_keygen(), "-t", "ed25519", "-N", "", "-C", comment, "-f", priv],
            capture_output=True,
            text=True,
            check=True,
        )
        try:
            os.chmod(priv, 0o600)
        except OSError:
            pass
        self._add(idx, name)
        self._write_index(idx)
        return self._info(name, idx)

    def add_sourced(self, name: str, path: str) -> dict:
        """Register an existing private key at `path` (not copied). Verifies its
        public line can be derived (ssh-keygen -y / sibling .pub)."""
        name = _safe_name(name)
        path = os.path.abspath(os.path.expanduser(path))
        idx = self._read_index()
        if name in set(self._names(idx)):
            raise FileExistsError(f"deploy key {name!r} already exists")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no such key file: {path}")
        public_line_from_private(path)  # validate up front (raises if unreadable)
        idx["sourced"][name] = path
        self._add(idx, name)
        self._write_index(idx)
        return self._info(name, idx)

    def set_active(self, name: str, active: bool) -> None:
        idx = self._read_index()
        if name not in set(self._names(idx)):
            raise FileNotFoundError(f"deploy key {name!r} does not exist")
        if active and name not in idx["active"]:
            idx["active"].append(name)
        elif not active and name in idx["active"]:
            idx["active"].remove(name)
            if idx.get("login") == name:  # the login key must stay trusted
                idx["login"] = idx["active"][0] if idx["active"] else None
        self._write_index(idx)

    def set_login(self, name: str) -> None:
        """Designate the deploy login key (auto-activates it — login must be trusted)."""
        idx = self._read_index()
        if name not in set(self._names(idx)):
            raise FileNotFoundError(f"deploy key {name!r} does not exist")
        if name not in idx["active"]:
            idx["active"].append(name)
        idx["login"] = name
        self._write_index(idx)

    def delete(self, name: str) -> None:
        """Remove key `name`. Generated keys' files are deleted; sourced keys are
        only unregistered (the external file is left alone). Idempotent."""
        idx = self._read_index()
        if name in idx.get("sourced", {}):
            idx["sourced"].pop(name, None)
        else:
            for p in (os.path.join(self.dir, name), os.path.join(self.dir, name + ".pub")):
                try:
                    os.remove(p)
                except (FileNotFoundError, OSError):
                    pass
        if name in idx["active"]:
            idx["active"].remove(name)
        if idx.get("login") == name:
            idx["login"] = idx["active"][0] if idx["active"] else None
        self._write_index(idx)

    def _info(self, name: str, idx: dict) -> dict:
        try:
            pub = self.public_line(name, idx)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            pub = ""
        return {
            "name": name,
            "kind": self._kind(name, idx),
            "path": self.private_path(name, idx),
            "dir": self.key_dir(name, idx),
            "active": name in idx.get("active", []),
            "login": name == idx.get("login"),
            "fingerprint": fingerprint_of_line(pub) if pub else "",
            "pub": pub,
        }


__all__ = [
    "KeyStore",
    "ssh_keygen",
    "fingerprint_of_line",
    "public_line_from_private",
]
