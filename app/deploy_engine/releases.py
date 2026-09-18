"""List published GitHub releases for the base-image version picker.

The desktop app's flash modal offers a dropdown of every release of the repo so
the user can pick which base image to flash. This module fetches the releases list
from the GitHub API and normalizes it into a small, JSON-serializable shape
(`{tag_name, name, published_at, prerelease}`), newest-first.

stdlib only. Network failure is caught by the caller (the sidecar) so the UI still
works with the CI-stamped default tag. The JSON->list parsing is split out
(`parse_releases`) so it is unit-testable without hitting the network.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Any

REPO = os.environ.get("TDDEPLOY_REPO", "fughilli/td-deploy")
_API = "https://api.github.com/repos/{repo}/releases"


def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that works in the PyInstaller-frozen app (mirrors
    download._ssl_context): the frozen interpreter has no OpenSSL CA store, so use
    the bundled `certifi` roots when available; SSL_CERT_FILE overrides either."""
    if not os.environ.get("SSL_CERT_FILE"):
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
    return ssl.create_default_context()


_SSL = _ssl_context()


def parse_releases(data: Any) -> list[dict]:
    """Normalize the GitHub releases JSON into `{tag_name, name, published_at,
    prerelease}` dicts, newest-first.

    Accepts either the decoded JSON (a list) or a raw JSON string/bytes. Drafts and
    entries without a tag are skipped. Ordering: GitHub already returns newest-first,
    but we sort defensively by `published_at` descending (entries missing a timestamp
    sink to the bottom while keeping their relative order)."""
    if isinstance(data, (str, bytes, bytearray)):
        data = json.loads(data)
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        tag = r.get("tag_name")
        if not tag:
            continue
        out.append(
            {
                "tag_name": tag,
                "name": r.get("name") or tag,
                "published_at": r.get("published_at") or "",
                "prerelease": bool(r.get("prerelease")),
            }
        )
    out.sort(key=lambda r: r["published_at"], reverse=True)
    return out


def _get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "td-deploy-studio",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:  # noqa: S310 - github https
        return r.read()


def list_releases(repo: str = REPO, *, per_page: int = 100) -> list[dict]:
    """Fetch and parse the repo's releases (newest-first). Raises on network error;
    the caller is expected to handle failure and fall back to the stamped default."""
    url = f"{_API.format(repo=repo)}?per_page={int(per_page)}"
    return parse_releases(_get(url))
