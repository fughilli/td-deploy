"""Generate and manage SSH ed25519 deploy keys — pure stdlib, no crypto deps.

The frozen sidecar is stdlib-only (no `cryptography`/`PyNaCl`), so this module
implements ed25519 keygen from scratch per RFC 8032, on edwards25519, using only
`os.urandom` (32-byte seed) and `hashlib.sha512`. From the seed it derives the
32-byte public key, then serializes the private half in the standard **unencrypted
OpenSSH** private-key format (`-----BEGIN OPENSSH PRIVATE KEY-----` wrapping the
`openssh-key-v1\0` blob) so `ssh -i <file>` and `ssh-keygen -y -f <file>` accept it
verbatim, and emits the matching `authorized_keys` line.

Why this pairs with the (already-merged) image half: the base image's first-boot
oneshot installs `/boot/firmware/authorized_keys` into root's
`~/.ssh/authorized_keys` (deploy/nix/flash-config.nix). The flasher drops the
ACTIVE key's public line there (via flash_config.write_boot_config), and at deploy
time push.py logs in with the same key's PRIVATE half. So a freshly flashed card
trusts exactly the key the app will use.

Correctness is safety-critical (a wrong key silently breaks deploy), so the math is
pinned to the RFC 8032 §7.1 test vectors and cross-checked against `ssh-keygen`
where available — see tests/deploy_keys_test.py.

A KeyStore persists multiple keypairs under the app config dir and tracks which one
is active:

  <config_dir>/deploy_keys/<name>        private key, OpenSSH format, 0600
  <config_dir>/deploy_keys/<name>.pub    authorized_keys line
  <config_dir>/deploy_keys/index.json    {"active": "<name>"}
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from typing import List, Optional

# --- edwards25519 field / group arithmetic (RFC 8032) ----------------------

_b = 256
_p = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493  # group order
_d = (-121665 * pow(121666, _p - 2, _p)) % _p  # curve constant -121665/121666
_I = pow(2, (_p - 1) // 4, _p)  # sqrt(-1) mod p


def _inv(x: int) -> int:
    return pow(x, _p - 2, _p)


def _x_recover(y: int) -> int:
    """Recover the x-coordinate from y on the curve (RFC 8032 point decoding)."""
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_By = (4 * _inv(5)) % _p
_Bx = _x_recover(_By)
_B = (_Bx % _p, _By % _p, 1, (_Bx * _By) % _p)  # base point, extended coords


def _edwards_add(P, Q):
    """Add two points in extended homogeneous coordinates (x, y, z, t)."""
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = ((y1 - x1) * (y2 - x2)) % _p
    b = ((y1 + x1) * (y2 + x2)) % _p
    c = (t1 * 2 * _d * t2) % _p
    dd = (z1 * 2 * z2) % _p
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    x3 = (e * f) % _p
    y3 = (g * h) % _p
    t3 = (e * h) % _p
    z3 = (f * g) % _p
    return (x3, y3, z3, t3)


def _scalarmult(P, e: int):
    """Compute e*P via double-and-add over the extended coordinates."""
    if e == 0:
        return (0, 1, 1, 0)
    Q = _scalarmult(P, e // 2)
    Q = _edwards_add(Q, Q)
    if e & 1:
        Q = _edwards_add(Q, P)
    return Q


def _encode_point(P) -> bytes:
    """Compress a point to 32 bytes: little-endian y with x's low bit in the top bit."""
    x, y, z, _t = P
    zi = _inv(z)
    x = (x * zi) % _p
    y = (y * zi) % _p
    bits = [(y >> i) & 1 for i in range(_b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_b // 8))


def _clamp(h32: bytes) -> int:
    """Derive the ed25519 scalar from the first 32 bytes of SHA-512(seed)."""
    a = int.from_bytes(h32, "little")
    a &= (1 << 254) - 8  # clear low 3 bits, clear bit 255
    a |= 1 << 254  # set bit 254
    return a


def public_key_from_seed(seed: bytes) -> bytes:
    """Derive the 32-byte ed25519 public key from a 32-byte seed (RFC 8032)."""
    if len(seed) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = _clamp(h[:32])
    A = _scalarmult(_B, a)
    return _encode_point(A)


def sign(seed: bytes, msg: bytes) -> bytes:
    """Sign `msg` with the ed25519 private `seed` (RFC 8032). Returns 64-byte sig.

    Provided for self-consistency testing; the deploy path uses ssh(1) to sign, not
    this function."""
    if len(seed) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = _clamp(h[:32])
    A = _encode_point(_scalarmult(_B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % _L
    R = _encode_point(_scalarmult(_B, r))
    k = int.from_bytes(hashlib.sha512(R + A + msg).digest(), "little") % _L
    s = (r + k * a) % _L
    return R + s.to_bytes(32, "little")


def verify(pubkey: bytes, msg: bytes, sig: bytes) -> bool:
    """Verify a 64-byte ed25519 signature. Used only by the self-consistency test."""
    if len(sig) != 64 or len(pubkey) != 32:
        return False
    R = sig[:32]
    s = int.from_bytes(sig[32:], "little")
    A = _decode_point(pubkey)
    if A is None:
        return False
    k = int.from_bytes(hashlib.sha512(R + pubkey + msg).digest(), "little") % _L
    left = _scalarmult(_B, s)
    right = _edwards_add(_decode_point(R), _scalarmult(A, k))
    return _encode_point(left) == _encode_point(right)


def _decode_point(comp: bytes):
    """Decompress a 32-byte point encoding back to extended coords, or None."""
    y = int.from_bytes(comp, "little") & ((1 << (_b - 1)) - 1)
    if y >= _p:
        return None
    x = _x_recover(y)
    if (comp[-1] >> 7) != (x & 1):
        x = _p - x
    return (x % _p, y % _p, 1, (x * y) % _p)


# --- SSH wire encoding -----------------------------------------------------


def _ssh_string(b: bytes) -> bytes:
    """uint32-be length prefix + bytes — the SSH `string` framing (RFC 4251)."""
    return struct.pack(">I", len(b)) + b


def _pubkey_blob(pubkey: bytes) -> bytes:
    """The SSH public-key blob for ed25519: string("ssh-ed25519") + string(pubkey)."""
    return _ssh_string(b"ssh-ed25519") + _ssh_string(pubkey)


def authorized_keys_line(pubkey: bytes, comment: str = "") -> str:
    """`ssh-ed25519 <base64(blob)> <comment>` — one authorized_keys / .pub line."""
    b64 = base64.b64encode(_pubkey_blob(pubkey)).decode("ascii")
    line = f"ssh-ed25519 {b64}"
    if comment:
        line += " " + comment
    return line


def fingerprint(pubkey: bytes) -> str:
    """OpenSSH-style `SHA256:<base64-no-pad>` fingerprint of the public-key blob."""
    digest = hashlib.sha256(_pubkey_blob(pubkey)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def openssh_private_key(seed: bytes, pubkey: bytes, comment: str = "") -> str:
    """Serialize an unencrypted OpenSSH ed25519 private key (PEM text).

    Layout (PROTOCOL.key): "openssh-key-v1\\0", ciphername/kdfname "none", empty
    kdf, 1 key, the public blob, then a padded private section: two equal random
    check-ints, ssh-ed25519, the 32-byte pubkey, the 64-byte private (seed||pubkey),
    the comment, and 1..8 incrementing padding bytes (\\x01\\x02...) to an 8-byte
    multiple (cipher "none" has blocksize 8).
    """
    if len(seed) != 32 or len(pubkey) != 32:
        raise ValueError("seed and pubkey must both be 32 bytes")

    magic = b"openssh-key-v1\x00"
    header = (
        _ssh_string(b"none")  # ciphername
        + _ssh_string(b"none")  # kdfname
        + _ssh_string(b"")  # kdf options (empty)
        + struct.pack(">I", 1)  # number of keys
        + _ssh_string(_pubkey_blob(pubkey))
    )

    check = os.urandom(4)
    private = seed + pubkey  # OpenSSH stores seed||pubkey as the 64-byte "private"
    priv_body = (
        check
        + check  # two identical check-ints (integrity guard on decrypt)
        + _pubkey_blob(pubkey)
        + _ssh_string(private)
        + _ssh_string(comment.encode("utf-8"))
    )
    pad = 0
    while len(priv_body) % 8 != 0:
        pad += 1
        priv_body += bytes([pad])  # 1,2,3,... up to blocksize-1

    blob = magic + header + _ssh_string(priv_body)
    b64 = base64.b64encode(blob).decode("ascii")
    wrapped = "\n".join(b64[i : i + 70] for i in range(0, len(b64), 70))
    return (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n" + wrapped + "\n-----END OPENSSH PRIVATE KEY-----\n"
    )


def parse_openssh_private_key(pem: str) -> bytes:
    """Parse our own OpenSSH private-key PEM and return the embedded 32-byte pubkey.

    A round-trip check (and the basis of the encoding test); not a full loader."""
    lines = [ln for ln in pem.splitlines() if ln and not ln.startswith("-----")]
    blob = base64.b64decode("".join(lines))
    off = len(b"openssh-key-v1\x00")
    if blob[:off] != b"openssh-key-v1\x00":
        raise ValueError("bad OpenSSH magic")

    def rd_string(buf, pos):
        (n,) = struct.unpack(">I", buf[pos : pos + 4])
        pos += 4
        return buf[pos : pos + n], pos + n

    _cipher, off = rd_string(blob, off)
    _kdfname, off = rd_string(blob, off)
    _kdf, off = rd_string(blob, off)
    (nkeys,) = struct.unpack(">I", blob[off : off + 4])
    off += 4
    if nkeys != 1:
        raise ValueError("expected exactly one key")
    pub_blob, off = rd_string(blob, off)
    priv_body, off = rd_string(blob, off)

    # public blob: string("ssh-ed25519") + string(pubkey)
    _kt, p = rd_string(pub_blob, 0)
    pub_from_public, _ = rd_string(pub_blob, p)
    return pub_from_public


def generate_keypair(comment: str = ""):
    """Generate a fresh ed25519 keypair.

    Returns (private_pem, authorized_keys_line, fingerprint, pubkey_bytes).
    """
    seed = os.urandom(32)
    pubkey = public_key_from_seed(seed)
    return (
        openssh_private_key(seed, pubkey, comment),
        authorized_keys_line(pubkey, comment),
        fingerprint(pubkey),
        pubkey,
    )


# --- persistent key store --------------------------------------------------

_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _safe_name(name: str) -> str:
    """A filesystem-safe key name (no path traversal, no separators)."""
    name = (name or "").strip()
    if not name or any(c not in _NAME_OK for c in name) or name in (".", ".."):
        raise ValueError(f"invalid deploy-key name {name!r}")
    return name


class KeyStore:
    """Multiple named ed25519 keypairs under a config dir, with an active pointer.

    <config_dir>/deploy_keys/<name>       OpenSSH private key, 0600
    <config_dir>/deploy_keys/<name>.pub   authorized_keys line
    <config_dir>/deploy_keys/index.json   {"active": "<name>"}
    """

    def __init__(self, config_dir: str) -> None:
        self.dir = os.path.join(config_dir, "deploy_keys")
        os.makedirs(self.dir, exist_ok=True)
        self.index_path = os.path.join(self.dir, "index.json")

    # -- paths --
    def private_path(self, name: str) -> str:
        return os.path.join(self.dir, _safe_name(name))

    def public_path(self, name: str) -> str:
        return os.path.join(self.dir, _safe_name(name) + ".pub")

    # -- index / active --
    def _read_index(self) -> dict:
        try:
            with open(self.index_path) as f:
                return json.load(f) or {}
        except (OSError, ValueError):
            return {}

    def _write_index(self, idx: dict) -> None:
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(idx, f)

    def active(self) -> Optional[str]:
        name = self._read_index().get("active")
        if name and os.path.exists(self.private_path(name)):
            return name
        return None

    def select(self, name: str) -> str:
        name = _safe_name(name)
        if not os.path.exists(self.private_path(name)):
            raise FileNotFoundError(f"deploy key {name!r} does not exist")
        idx = self._read_index()
        idx["active"] = name
        self._write_index(idx)
        return name

    # -- mutations --
    def generate(self, name: str, comment: Optional[str] = None) -> dict:
        """Create a new keypair `name` (private 0600 + .pub), make it active, and
        return {name, fingerprint, pub, active}."""
        name = _safe_name(name)
        if os.path.exists(self.private_path(name)):
            raise FileExistsError(f"deploy key {name!r} already exists")
        comment = comment if comment is not None else name
        pem, akline, fp, _pubkey = generate_keypair(comment)

        priv = self.private_path(name)
        # Create private key 0600 from the start (never briefly world-readable).
        fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, pem.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(priv, 0o600)
        except OSError:
            pass

        with open(self.public_path(name), "w", encoding="utf-8") as f:
            f.write(akline + "\n")

        idx = self._read_index()
        idx["active"] = name  # newly generated key becomes active
        self._write_index(idx)
        return {"name": name, "fingerprint": fp, "pub": akline, "active": name}

    def public_line(self, name: str) -> str:
        """Return the authorized_keys line for `name` (from its .pub file)."""
        with open(self.public_path(name)) as f:
            return f.read().strip()

    def list(self) -> List[dict]:
        """List stored keys: [{name, fingerprint, pub, active}] sorted by name."""
        active = self.active()
        out: List[dict] = []
        for fn in sorted(os.listdir(self.dir)):
            if fn.endswith(".pub") or fn == "index.json":
                continue
            name = fn
            pub_path = self.public_path(name)
            if not os.path.exists(pub_path):
                continue
            akline = self.public_line(name)
            # fingerprint from the stored pub line (decode the blob's pubkey).
            try:
                blob = base64.b64decode(akline.split()[1])
                pubkey = _rd_pubkey_from_blob(blob)
                fp = fingerprint(pubkey)
            except (IndexError, ValueError):
                fp = ""
            out.append(
                {
                    "name": name,
                    "fingerprint": fp,
                    "pub": akline,
                    "active": name == active,
                }
            )
        return out

    def active_public_line(self) -> Optional[str]:
        name = self.active()
        return self.public_line(name) if name else None

    def active_private_path(self) -> Optional[str]:
        name = self.active()
        return self.private_path(name) if name else None


def _rd_pubkey_from_blob(blob: bytes) -> bytes:
    """Extract the 32-byte pubkey from an ssh-ed25519 public-key blob."""
    (n,) = struct.unpack(">I", blob[:4])
    pos = 4 + n
    (m,) = struct.unpack(">I", blob[pos : pos + 4])
    pos += 4
    return blob[pos : pos + m]


__all__ = [
    "public_key_from_seed",
    "sign",
    "verify",
    "authorized_keys_line",
    "fingerprint",
    "openssh_private_key",
    "parse_openssh_private_key",
    "generate_keypair",
    "KeyStore",
]
