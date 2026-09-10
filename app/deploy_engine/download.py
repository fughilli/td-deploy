"""Fetch the Pi base image from GitHub Releases and verify it.

The desktop app never builds the base image (that's CI's job); it downloads the
published `.img` by release tag and flashes it. Assets are resolved via the GitHub
releases API so we don't hard-code asset filenames, and the download is verified
against the accompanying `.sha256` (or an asset digest) before it is ever written
to a disk.

stdlib only. Progress is reported through a plain `on_progress(frac, msg)` callback
so this is independent of the deploy `Progress` phase machinery.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

REPO = os.environ.get("TDDEPLOY_REPO", "fughilli/td-deploy")
_API = "https://api.github.com/repos/{repo}/releases/{ref}"
OnProgress = Callable[[float, str], None]


def _noop(_frac: float, _msg: str) -> None:
    pass


@dataclass
class Asset:
    name: str
    url: str  # browser_download_url
    size: int
    digest: Optional[str]  # "sha256:..." if GitHub reports one, else None


@dataclass
class Release:
    tag: str
    assets: list[Asset]

    def find(self, *suffixes: str) -> Optional[Asset]:
        for a in self.assets:
            if any(a.name.endswith(s) for s in suffixes):
                return a
        return None


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "td-deploy-studio",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - github https
        return r.read()


def get_release(tag: str = "latest", repo: str = REPO) -> Release:
    """Resolve a release by tag ('latest' for the newest) into its asset list."""
    ref = "latest" if tag == "latest" else f"tags/{tag}"
    data = json.loads(_get(_API.format(repo=repo, ref=ref)))
    assets = [
        Asset(
            name=a["name"],
            url=a["browser_download_url"],
            size=int(a.get("size", 0)),
            digest=a.get("digest"),
        )
        for a in data.get("assets", [])
    ]
    return Release(tag=data.get("tag_name", tag), assets=assets)


def _download(asset: Asset, dest: str, on_progress: OnProgress) -> None:
    req = urllib.request.Request(asset.url, headers={"User-Agent": "td-deploy-studio"})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - github https
        total = int(r.headers.get("Content-Length") or asset.size or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                frac = done / total if total else 0.0
                on_progress(frac, f"{done >> 20} / {total >> 20} MiB")
    os.replace(tmp, dest)


def _sha256(path: str, on_progress: OnProgress) -> str:
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            done += len(chunk)
            on_progress(done / total if total else 1.0, "verifying")
    return h.hexdigest()


def _expected_sha(rel: Release, img: Asset) -> Optional[str]:
    """Prefer GitHub's asset digest; else fetch a sibling `<name>.sha256`."""
    if img.digest and img.digest.startswith("sha256:"):
        return img.digest.split(":", 1)[1].strip()
    sidecar = rel.find(img.name + ".sha256", ".sha256", ".sha256sum")
    if sidecar:
        text = _get(sidecar.url, accept="application/octet-stream").decode("utf-8", "replace")
        # accept "<hex>" or "<hex>  filename"
        return text.strip().split()[0] if text.strip() else None
    return None


def default_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "td-deploy-studio", "images")


def fetch_base_image(
    tag: str = "latest",
    *,
    repo: str = REPO,
    cache_dir: Optional[str] = None,
    on_progress: OnProgress = _noop,
    verify: bool = True,
) -> str:
    """Download (or reuse cached) verified base `.img` for `tag`; return its path.

    A cached image whose sha256 already matches the release is returned without
    re-downloading. Raises on checksum mismatch (the bad file is removed).
    """
    rel = get_release(tag, repo)
    img = rel.find(".img", ".img.raw", ".iso")
    if img is None:
        raise FileNotFoundError(f"release {rel.tag!r} has no .img asset")
    cache = cache_dir or default_cache_dir()
    os.makedirs(cache, exist_ok=True)
    dest = os.path.join(cache, f"{rel.tag}-{img.name}")
    expected = _expected_sha(rel, img) if verify else None

    if os.path.exists(dest) and expected:
        on_progress(0.0, "checking cached image")
        if _sha256(dest, on_progress) == expected.lower():
            on_progress(1.0, "cached")
            return dest  # already good
    if os.path.exists(dest) and not expected:
        return dest

    on_progress(0.0, f"downloading {img.name}")
    _download(img, dest, on_progress)
    if expected:
        got = _sha256(dest, on_progress)
        if got.lower() != expected.lower():
            os.remove(dest)
            raise ValueError(f"sha256 mismatch: got {got}, expected {expected}")
    on_progress(1.0, "ready")
    return dest
