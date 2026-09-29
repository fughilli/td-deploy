"""deploy/nix/tdplayer-prepare.py — the on-player step that builds a Python-host
artifact's venv before it goes live (baked into the x86_64 image by
deploy/nix/python-host.nix; run over ssh by the app's push).

Exercised offline against a staged artifact whose project ships a one-wheel
wheelhouse (a wheel built here), under a scratch TDPLAYER_ROOT.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "deploy", "nix", "tdplayer-prepare.py")


def _wheel(dirpath, name="tinypkg", version="1.0"):
    """A minimal pure-Python wheel: <name>/__init__.py + dist-info."""
    fn = os.path.join(dirpath, f"{name}-{version}-py3-none-any.whl")
    di = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": b"VALUE = 42\n",
        f"{di}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{di}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    with zipfile.ZipFile(fn, "w") as z:
        for path, data in files.items():
            z.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{path},sha256={digest},{len(data)}")
        record.append(f"{di}/RECORD,,")
        z.writestr(f"{di}/RECORD", "\n".join(record) + "\n")
    return fn


def _staging(root, reqs="tinypkg==1.0\n"):
    st = tempfile.mkdtemp(dir=root, prefix="staging-")
    proj = os.path.join(st, "project")
    os.makedirs(os.path.join(proj, "wheels"))
    _wheel(os.path.join(proj, "wheels"))
    with open(os.path.join(proj, "requirements-linux.txt"), "w") as fh:
        fh.write(reqs)
    sched = {
        "format": "toxc-host/1",
        "host": {"project_folder": "project", "python": {"requirements": "requirements-linux.txt"}},
    }
    with open(os.path.join(st, "schedule.json"), "w") as fh:
        json.dump(sched, fh)
    return st


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.env = dict(os.environ, TDPLAYER_ROOT=self.root, PIP_NO_INPUT="1")

    def run_prepare(self, staging):
        return subprocess.run(
            [sys.executable, SCRIPT, staging],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_builds_links_and_reuses(self):
        st = _staging(self.root)
        r = self.run_prepare(st)
        self.assertEqual(r.returncode, 0, r.stderr)
        link = os.path.join(st, "project", ".venv")
        self.assertTrue(os.path.islink(link))
        py = os.path.join(link, "bin", "python")
        out = subprocess.run(
            [py, "-c", "import tinypkg; print(tinypkg.VALUE)"], capture_output=True, text=True
        )
        self.assertEqual(out.stdout.strip(), "42", out.stderr)
        # the next deploy with the same requirements reuses the venv
        st2 = _staging(self.root)
        r2 = self.run_prepare(st2)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("reusing venv", r2.stderr)
        self.assertEqual(
            os.path.realpath(os.path.join(st2, "project", ".venv")), os.path.realpath(link)
        )

    def test_failed_install_leaves_nothing_behind(self):
        st = _staging(self.root, reqs="doesnotexist==9.9\n")
        r = self.run_prepare(st)
        self.assertNotEqual(r.returncode, 0)
        venvs = os.path.join(self.root, "venvs")
        self.assertEqual([n for n in os.listdir(venvs) if not n.startswith(".")], [])
        self.assertFalse(os.path.exists(os.path.join(st, "project", ".venv")))

    def test_classic_artifact_is_a_no_op(self):
        st = tempfile.mkdtemp(dir=self.root)
        with open(os.path.join(st, "schedule.json"), "w") as fh:
            json.dump({"nodes": []}, fh)
        r = self.run_prepare(st)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, "venvs")))


if __name__ == "__main__":
    unittest.main()
