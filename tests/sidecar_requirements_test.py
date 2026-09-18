"""Regression: the frozen sidecar must ship the compile path's numpy dependency.

The deploy-engine sidecar (app/packaging/sidecar.spec) freezes the reused
compiler pipeline with PyInstaller. During deploy it runs
`compiler/emit_artifact.emit`, which for a *video* source extracts the clip's
first frame via `runtime.video.VideoSource` — and that decode uses numpy.

numpy was missing from `app/packaging/requirements.txt`, so it never got frozen
into the sidecar and deploy died with:

    ModuleNotFoundError: No module named 'numpy'

This asserts numpy (and the other host asset-decode deps it sits alongside) stay
declared in the sidecar's packaging requirements, so the frozen binary keeps
them. Hermetic: it only parses the requirements file.
"""

import os
import unittest


def _find(rel: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), here, os.path.dirname(here)):
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(rel)


def _declared_packages(path: str) -> set[str]:
    names = set()
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            # Strip version specifiers / extras / markers: numpy>=1.24 -> numpy
            name = line.split(";", 1)[0]
            for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", " "):
                name = name.split(sep, 1)[0]
            names.add(name.strip().lower())
    return names


class SidecarRequirementsTest(unittest.TestCase):
    def setUp(self):
        self.reqs = _declared_packages(_find("app/packaging/requirements.txt"))

    def test_numpy_is_declared(self):
        self.assertIn(
            "numpy",
            self.reqs,
            "numpy must be in the sidecar requirements — emit_artifact's video "
            "first-frame decode (runtime.video.VideoSource) imports it, and the "
            "frozen sidecar dies with 'No module named numpy' without it.",
        )

    def test_asset_decode_deps_present(self):
        # Guards the parser and the rest of the host asset-decode surface numpy
        # sits alongside; these three move together for emit_artifact.
        for pkg in ("pillow", "av", "numpy"):
            self.assertIn(pkg, self.reqs)


if __name__ == "__main__":
    unittest.main()
