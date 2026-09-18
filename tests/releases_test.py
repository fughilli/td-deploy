"""Unit tests for base-image release parsing (app/deploy_engine/releases.py).

Loaded by file path (stdlib-only module) so the test stays hermetic — no network,
no deploy toolchain import. Exercises the JSON->list normalization: shape, order
(newest-first), the prerelease flag, and that drafts / tagless entries are dropped.
"""

import importlib.util
import os
import unittest


def _load():
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), here, os.path.dirname(here)):
        p = os.path.join(base, "app", "deploy_engine", "releases.py")
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("releases", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise FileNotFoundError("app/deploy_engine/releases.py")


releases = _load()

# A representative slice of the GitHub /releases response, intentionally NOT sorted
# newest-first and containing a draft + a tagless entry to exercise filtering/order.
SAMPLE = [
    {
        "tag_name": "v1.0.0",
        "name": "First stable",
        "published_at": "2026-01-10T00:00:00Z",
        "prerelease": False,
        "draft": False,
    },
    {
        "tag_name": "v1.1.0-rc1",
        "name": "Release candidate",
        "published_at": "2026-02-01T00:00:00Z",
        "prerelease": True,
        "draft": False,
    },
    {
        "tag_name": "v0.9.0",
        "name": None,
        "published_at": "2025-12-01T00:00:00Z",
        "prerelease": False,
        "draft": False,
    },
    {  # a draft: must be dropped
        "tag_name": "v2.0.0-draft",
        "name": "WIP",
        "published_at": "2026-03-01T00:00:00Z",
        "prerelease": False,
        "draft": True,
    },
    {  # no tag: must be dropped
        "name": "orphan",
        "published_at": "2026-02-15T00:00:00Z",
        "prerelease": False,
    },
]


class ParseReleasesTest(unittest.TestCase):
    def test_shape_and_keys(self):
        out = releases.parse_releases(SAMPLE)
        self.assertTrue(out)
        for r in out:
            self.assertEqual(set(r.keys()), {"tag_name", "name", "published_at", "prerelease"})

    def test_newest_first(self):
        tags = [r["tag_name"] for r in releases.parse_releases(SAMPLE)]
        self.assertEqual(tags, ["v1.1.0-rc1", "v1.0.0", "v0.9.0"])

    def test_prerelease_flag(self):
        by_tag = {r["tag_name"]: r for r in releases.parse_releases(SAMPLE)}
        self.assertTrue(by_tag["v1.1.0-rc1"]["prerelease"])
        self.assertFalse(by_tag["v1.0.0"]["prerelease"])

    def test_drops_drafts_and_tagless(self):
        tags = [r["tag_name"] for r in releases.parse_releases(SAMPLE)]
        self.assertNotIn("v2.0.0-draft", tags)
        self.assertEqual(len(tags), 3)

    def test_name_falls_back_to_tag(self):
        by_tag = {r["tag_name"]: r for r in releases.parse_releases(SAMPLE)}
        self.assertEqual(by_tag["v0.9.0"]["name"], "v0.9.0")

    def test_accepts_json_string(self):
        import json

        out = releases.parse_releases(json.dumps(SAMPLE))
        self.assertEqual([r["tag_name"] for r in out], ["v1.1.0-rc1", "v1.0.0", "v0.9.0"])

    def test_non_list_returns_empty(self):
        self.assertEqual(releases.parse_releases({"message": "Not Found"}), [])
        self.assertEqual(releases.parse_releases(None), [])


if __name__ == "__main__":
    unittest.main()
