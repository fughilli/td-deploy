"""Unit tests for asset resolution (app/deploy_engine/assets.py).

Loaded by file path (stdlib-only module) so the test stays hermetic. Exercises the
configurable search roots and the explicit substitution map with real temp files.
"""

import importlib.util
import os
import tempfile
import unittest


def _load():
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), here, os.path.dirname(here)):
        p = os.path.join(base, "app", "deploy_engine", "assets.py")
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("assets", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise FileNotFoundError("app/deploy_engine/assets.py")


assets = _load()


class ResolveAssetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _touch(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").close()
        return p

    def test_missing_returns_none_with_searched_dirs(self):
        got, searched = assets.resolve_local_asset("Banana.tif", "/proj/x.toe")
        self.assertIsNone(got)
        self.assertIn("/proj", searched)  # the .toe dir is among the roots searched

    def test_extra_root_by_basename(self):
        real = self._touch("media", "Banana.tif")
        got, _ = assets.resolve_local_asset(
            "assets/Banana.tif", "/proj/x.toe", roots=[os.path.join(self.tmp, "media")]
        )
        self.assertEqual(got, os.path.abspath(real))

    def test_substitution_map_by_basename_wins(self):
        real = self._touch("Replacement.png")
        got, searched = assets.resolve_local_asset(
            "orig/Banana.tif", "/proj/x.toe", asset_map={"Banana.tif": real}
        )
        self.assertEqual(got, os.path.abspath(real))
        self.assertEqual(searched, [])  # short-circuits the search

    def test_substitution_map_by_full_path(self):
        real = self._touch("R.png")
        got, _ = assets.resolve_local_asset(
            "toxc/Banana.tif", "/proj/x.toe", asset_map={"toxc/Banana.tif": real}
        )
        self.assertEqual(got, os.path.abspath(real))

    def test_stale_substitution_falls_through_to_search(self):
        got, searched = assets.resolve_local_asset(
            "Banana.tif", "/proj/x.toe", asset_map={"Banana.tif": "/does/not/exist.png"}
        )
        self.assertIsNone(got)
        self.assertTrue(searched)  # bad substitution -> normal search ran


if __name__ == "__main__":
    unittest.main()
