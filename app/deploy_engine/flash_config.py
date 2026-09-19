"""Render the flash-time per-card config the boot image consumes.

The base image is a fixed NixOS build, so the hostname and WiFi creds can't be
baked per-card. Instead the desktop app's flasher, right after the raw image
write, drops ready-to-use artifacts onto the FAT `/boot/firmware` partition and
an on-boot oneshot (`deploy/nix/flash-config.nix`, already merged) installs them:

  /boot/firmware/td-hostname                        one line: the chosen hostname
  /boot/firmware/system-connections/*.nmconnection  NetworkManager keyfiles, one
                                                    per WiFi network
  /boot/firmware/authorized_keys                    SSH deploy pubkey line(s); the
                                                    image installs them into root's
                                                    ~/.ssh/authorized_keys on boot

This module is the host-side other half: it validates/normalizes the hostname,
renders the minimal WPA-PSK (or open) NetworkManager keyfiles, and writes both
onto an *already-mounted* boot directory. It is intentionally pure filesystem
(no mounting, no elevation) and stdlib-only, so it is unit-testable with a
tmpdir and safe to import from the frozen sidecar.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional

# RFC1123 label: 1..63 chars, lowercase [a-z0-9-], no leading/trailing hyphen.
# Mirrors the guard baked into deploy/nix/flash-config.nix so what the app accepts
# is exactly what the on-boot oneshot will apply.
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_hostname(name: str) -> str:
    """Lowercase and strip surrounding whitespace (matching the image's `tr`)."""
    return (name or "").strip().lower()


def valid_hostname(name: str) -> bool:
    """True iff `name` (after normalization) is a valid RFC1123 hostname label."""
    return bool(_HOSTNAME_RE.match(normalize_hostname(name)))


def _slug(ssid: str) -> str:
    """A filesystem-safe filename stem for an SSID's keyfile.

    SSIDs can contain spaces/slashes/etc., so collapse anything outside [a-z0-9-_]
    to '-'. The profile *id* inside the file stays the human `seed-<ssid>`; only the
    on-disk filename is slugged."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (ssid or "").strip()).strip("-._")
    return s.lower() or "network"


def render_nmconnection(ssid: str, psk: Optional[str] = None) -> str:
    """Render a minimal NetworkManager keyfile for `ssid`.

    WPA-PSK when `psk` is truthy; an OPEN network (no `[wifi-security]`) otherwise.
    The profile id is `seed-<ssid>`, matching the `seed_wifi` naming the repo
    already uses (see deploy/README.md)."""
    lines = [
        "[connection]",
        f"id=seed-{ssid}",
        "type=wifi",
        "[wifi]",
        f"ssid={ssid}",
    ]
    if psk:
        lines += [
            "[wifi-security]",
            "key-mgmt=wpa-psk",
            f"psk={psk}",
        ]
    lines += [
        "[ipv4]",
        "method=auto",
        "[ipv6]",
        "method=auto",
    ]
    return "\n".join(lines) + "\n"


def write_boot_config(
    mount_dir: str,
    hostname: Optional[str] = None,
    networks: Optional[Iterable[dict]] = None,
    authorized_keys: Optional[Iterable[str]] = None,
) -> List[str]:
    """Write the flash-time artifacts under an already-mounted boot dir.

    `mount_dir` is the FAT boot partition's mount point (its `/boot/firmware`).
    Writes `td-hostname` (only when `hostname` is a valid label), one
    `system-connections/<slug>.nmconnection` per network in `networks` (each a
    dict {ssid, psk?}), and `authorized_keys` (one SSH pubkey line per entry in
    `authorized_keys`). Networks without an SSID and blank key lines are skipped.
    Returns the list of absolute paths written.

    The `authorized_keys` file is what the (already-merged) image first-boot
    oneshot installs into root's ~/.ssh/authorized_keys — so the flashed Pi trusts
    the app's deploy key. Pure filesystem — no mounting, no elevation. The image
    reinstalls the WiFi keyfiles with 0600 root perms on boot; we still write them
    0600 here so the plaintext PSK isn't world-readable on the card meanwhile.
    """
    written: List[str] = []

    if hostname is not None:
        norm = normalize_hostname(hostname)
        if valid_hostname(norm):
            path = os.path.join(mount_dir, "td-hostname")
            with open(path, "w", encoding="utf-8") as f:
                f.write(norm + "\n")
            written.append(path)

    seen: set[str] = set()
    for net in networks or []:
        ssid = (net.get("ssid") or "").strip()
        if not ssid:
            continue
        stem = _slug(ssid)
        fname = stem
        n = 1
        while fname in seen:  # distinct filenames for slug collisions
            n += 1
            fname = f"{stem}-{n}"
        seen.add(fname)

        conn_dir = os.path.join(mount_dir, "system-connections")
        os.makedirs(conn_dir, exist_ok=True)
        path = os.path.join(conn_dir, f"{fname}.nmconnection")
        content = render_nmconnection(ssid, net.get("psk") or None)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            os.chmod(path, 0o600)  # plaintext PSK: not world-readable
        except OSError:
            pass
        written.append(path)

    keys = [k.strip() for k in (authorized_keys or []) if k and k.strip()]
    if keys:
        path = os.path.join(mount_dir, "authorized_keys")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(keys) + "\n")
        try:
            os.chmod(path, 0o600)  # match the perms the image installs
        except OSError:
            pass
        written.append(path)

    return written


__all__ = [
    "normalize_hostname",
    "valid_hostname",
    "render_nmconnection",
    "write_boot_config",
]
