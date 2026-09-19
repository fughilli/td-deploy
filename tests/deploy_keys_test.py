"""Tests for the ssh-keygen-backed deploy-key store.

Key generation and public-key derivation are delegated to `ssh-keygen`, so those
paths are only exercised when ssh-keygen is available (skipped otherwise — e.g. in
the hermetic bazel sandbox). The store's management logic (list / active set /
login / sourced / delete) and the pure-Python fingerprint helper are tested
unconditionally using fabricated key files.
"""

import base64
import hashlib
import importlib.util
import os
import shutil
import tempfile
import unittest


def _load():
    # Loaded by file path (the bazel target only data-deps the file; deploy_engine
    # isn't an importable package here). Mirrors tests/fixit_test.py.
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), here, os.path.dirname(here)):
        p = os.path.join(base, "app", "deploy_engine", "deploy_keys.py")
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("deploy_keys", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise FileNotFoundError("app/deploy_engine/deploy_keys.py")


dk = _load()

_HAS_KEYGEN = shutil.which("ssh-keygen") is not None


def _fake_generated(store, name, pub_line):
    """A generated key: private + .pub under the store dir (no ssh-keygen needed —
    public_line() reads the .pub)."""
    with open(os.path.join(store.dir, name), "w") as f:
        f.write("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n")
    with open(os.path.join(store.dir, name + ".pub"), "w") as f:
        f.write(pub_line + "\n")


class FingerprintTest(unittest.TestCase):
    def test_hashes_the_blob_field(self):
        blob = b"an arbitrary key blob"
        line = "ssh-ed25519 " + base64.b64encode(blob).decode() + " user@host"
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        self.assertEqual(dk.fingerprint_of_line(line), expected)

    def test_malformed_line_is_empty(self):
        self.assertEqual(dk.fingerprint_of_line("not-a-key-line"), "")


class PublicLineTest(unittest.TestCase):
    def test_prefers_sibling_pub(self):
        d = tempfile.mkdtemp()
        priv = os.path.join(d, "id")
        open(priv, "w").close()
        with open(priv + ".pub", "w") as f:
            f.write("ssh-ed25519 AAAAC3Nz sibling\n")
        self.assertEqual(dk.public_line_from_private(priv), "ssh-ed25519 AAAAC3Nz sibling")


class KeyStoreManagementTest(unittest.TestCase):
    def setUp(self):
        self.ks = dk.KeyStore(tempfile.mkdtemp())

    def test_active_set_and_login(self):
        _fake_generated(self.ks, "alpha", "ssh-ed25519 AAAAaaaa alpha")
        _fake_generated(self.ks, "beta", "ssh-ed25519 AAAAbbbb beta")
        self.assertEqual([k["name"] for k in self.ks.list()], ["alpha", "beta"])
        self.assertEqual(self.ks.active(), [])
        self.assertIsNone(self.ks.login())

        self.ks.set_active("alpha", True)
        self.ks.set_active("beta", True)
        self.assertEqual(sorted(self.ks.active()), ["alpha", "beta"])
        # both active -> both lines written to the card's authorized_keys
        self.assertEqual(
            sorted(self.ks.active_public_lines()),
            ["ssh-ed25519 AAAAaaaa alpha", "ssh-ed25519 AAAAbbbb beta"],
        )

        self.ks.set_login("beta")
        self.assertEqual(self.ks.login(), "beta")
        self.assertTrue(next(k for k in self.ks.list() if k["name"] == "beta")["login"])
        self.assertEqual(self.ks.login_private_path(), os.path.join(self.ks.dir, "beta"))

        # deactivating the login key clears/moves login (login must stay trusted)
        self.ks.set_active("beta", False)
        self.assertNotIn("beta", self.ks.active())
        self.assertEqual(self.ks.login(), "alpha")  # fell back to the remaining active key

    def test_set_login_autoactivates(self):
        _fake_generated(self.ks, "k", "ssh-ed25519 AAAAkkkk k")
        self.ks.set_login("k")
        self.assertIn("k", self.ks.active())  # login implies trusted

    def test_delete_generated_removes_files(self):
        _fake_generated(self.ks, "g", "ssh-ed25519 AAAAgggg g")
        self.ks.set_login("g")
        self.ks.delete("g")
        self.assertEqual(self.ks.list(), [])
        self.assertIsNone(self.ks.login())
        self.assertFalse(os.path.exists(os.path.join(self.ks.dir, "g")))

    def test_sourced_key_is_referenced_not_copied(self):
        ext_dir = tempfile.mkdtemp()
        ext = os.path.join(ext_dir, "id_ed25519")
        open(ext, "w").close()
        with open(ext + ".pub", "w") as f:
            f.write("ssh-ed25519 AAAAsrc sourced\n")
        info = self.ks.add_sourced("mine", ext)
        self.assertEqual(info["kind"], "sourced")
        self.assertEqual(info["path"], ext)
        self.assertEqual(info["dir"], ext_dir)  # folder icon opens the source dir
        self.assertIn("mine", self.ks.active())
        # deleting a sourced key unregisters it but leaves the external file
        self.ks.delete("mine")
        self.assertEqual(self.ks.list(), [])
        self.assertTrue(os.path.exists(ext))


@unittest.skipUnless(_HAS_KEYGEN, "ssh-keygen not available")
class SshKeygenTest(unittest.TestCase):
    def test_generate(self):
        ks = dk.KeyStore(tempfile.mkdtemp())
        info = ks.generate("deploy")
        self.assertEqual(info["name"], "deploy")
        self.assertEqual(info["kind"], "generated")
        self.assertTrue(info["pub"].startswith("ssh-ed25519 "))
        self.assertTrue(info["active"])  # new key is active
        self.assertEqual(ks.login(), "deploy")  # and becomes login when none set
        priv = ks.private_path("deploy")
        self.assertEqual(os.stat(priv).st_mode & 0o777, 0o600)

    def test_duplicate_rejected(self):
        ks = dk.KeyStore(tempfile.mkdtemp())
        ks.generate("dup")
        with self.assertRaises(FileExistsError):
            ks.generate("dup")


if __name__ == "__main__":
    unittest.main()
