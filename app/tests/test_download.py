"""Tests for base-image resolution from a GitHub release (offline: no network)."""

from deploy_engine import download as dl


def _release():
    return dl.Release(
        tag="v1",
        assets=[
            dl.Asset("tdplayer-pi3.img", "u1", 100, "sha256:abc123"),
            dl.Asset("tdplayer-pi3.img.sha256", "u2", 64, None),
        ],
    )


def test_find_prefers_img():
    assert _release().find(".img").name == "tdplayer-pi3.img"


def test_expected_sha_uses_github_digest():
    rel = _release()
    assert dl._expected_sha(rel, rel.find(".img")) == "abc123"


def test_find_falls_back_to_sidecar_sha():
    rel = dl.Release(
        tag="v2",
        assets=[
            dl.Asset("x.img", "u", 1, None),
            dl.Asset("x.img.sha256", "u2", 1, None),
        ],
    )
    assert rel.find("x.img.sha256", ".sha256").name == "x.img.sha256"


def test_default_cache_dir_under_home():
    assert dl.default_cache_dir().endswith("td-deploy-studio/images")
