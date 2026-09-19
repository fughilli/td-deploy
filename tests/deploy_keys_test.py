"""Unit tests for the pure-Python ed25519 deploy-key module.

Safety-critical: a wrong key silently breaks deploy (the flashed card would trust
the wrong pubkey, or push.py would offer the wrong private key). So the curve math
is pinned to the RFC 8032 §7.1 test vectors, and the OpenSSH encoding is verified
both by our own round-trip and — where it's on PATH — cross-checked against
`ssh-keygen -y` on our generated private key.

Loaded by file path (stdlib-only module) so the test stays hermetic.
"""

import base64
import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import unittest


def _load():
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


# Authoritative vectors copied verbatim from RFC 8032, Section 7.1
# ("Test 2" and "Test 3"): (secret-seed hex, public-key hex). These pin the
# seed -> public-key curve math; the RFC's Test 2 *signature* is checked separately
# in test_rfc8032_signature_vector (that vector also exercises the full sign path).
_VECTORS = [
    # RFC 8032 §7.1 Test 2
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    ),
    # RFC 8032 §7.1 Test 3
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
    ),
    # RFC 8032 §7.1 Test 1024 (the 1023-byte-message vector's keypair).
    (
        "f5e5767cf153319517630f226876b86c8160cc583bc013744c6bf255f5cc0ee5",
        "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",
    ),
]


class RFC8032VectorTest(unittest.TestCase):
    """The curve math: seed -> public key must match the published vectors EXACTLY."""

    def test_public_key_derivation(self):
        for seed_hex, pub_hex in _VECTORS:
            seed = bytes.fromhex(seed_hex)
            got = dk.public_key_from_seed(seed)
            self.assertEqual(got.hex(), pub_hex, f"seed {seed_hex}")


class SignVerifyTest(unittest.TestCase):
    """Optional signing path — self-consistency + RFC 8032 §7.1 signature vector."""

    def test_sign_verify_roundtrip(self):
        seed = os.urandom(32)
        pub = dk.public_key_from_seed(seed)
        msg = b"deploy-key self test"
        sig = dk.sign(seed, msg)
        self.assertTrue(dk.verify(pub, msg, sig))
        self.assertFalse(dk.verify(pub, msg + b"x", sig))

    def test_rfc8032_signature_vector(self):
        # Test 2 (single-byte message 0x72) from RFC 8032 §7.1.
        seed = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
        msg = bytes.fromhex("72")
        expect = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540"
            "a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c"
            "387b2eaeb4302aeeb00d291612bb0c00"
        )
        self.assertEqual(dk.sign(seed, msg), expect)


class OpenSSHEncodingTest(unittest.TestCase):
    def test_roundtrip_pubkey(self):
        pem, akline, fp, pubkey = dk.generate_keypair("test@td")
        # parse our own private blob back and recover the same pubkey
        recovered = dk.parse_openssh_private_key(pem)
        self.assertEqual(recovered, pubkey)
        # the authorized_keys line's embedded pubkey equals the private key's pubkey
        blob = base64.b64decode(akline.split()[1])
        # blob = string("ssh-ed25519") + string(pubkey)
        import struct

        (n,) = struct.unpack(">I", blob[:4])
        pos = 4 + n
        (m,) = struct.unpack(">I", blob[pos : pos + 4])
        pos += 4
        self.assertEqual(blob[pos : pos + m], pubkey)
        self.assertTrue(akline.startswith("ssh-ed25519 "))
        self.assertTrue(akline.endswith(" test@td"))
        self.assertTrue(fp.startswith("SHA256:"))

    def test_pem_wrapping(self):
        pem, _ak, _fp, _pub = dk.generate_keypair()
        self.assertTrue(pem.startswith("-----BEGIN OPENSSH PRIVATE KEY-----\n"))
        self.assertTrue(pem.rstrip().endswith("-----END OPENSSH PRIVATE KEY-----"))

    def test_blob_length_framing(self):
        # priv_body must be padded to an 8-byte multiple (cipher "none" blocksize 8).
        pem = dk.openssh_private_key(bytes(32), dk.public_key_from_seed(bytes(32)), "c")
        lines = [ln for ln in pem.splitlines() if not ln.startswith("-----")]
        blob = base64.b64decode("".join(lines))
        import struct

        # locate priv_body length (last string in the blob)
        off = len(b"openssh-key-v1\x00")

        def rd(pos):
            (nn,) = struct.unpack(">I", blob[pos : pos + 4])
            return nn, pos + 4 + nn

        for _ in range(3):  # cipher, kdfname, kdf
            _n, off = rd(off)
        off += 4  # nkeys
        _pn, off = rd(off)  # public blob
        priv_len, _end = rd(off)
        self.assertEqual(priv_len % 8, 0)


class SshKeygenCrossCheckTest(unittest.TestCase):
    def test_ssh_keygen_agrees(self):
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            self.skipTest("ssh-keygen not on PATH")
        pem, akline, _fp, _pub = dk.generate_keypair("xcheck@td")
        with tempfile.TemporaryDirectory() as d:
            kf = os.path.join(d, "id_ed25519")
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, pem.encode())
            os.close(fd)
            proc = subprocess.run(
                [keygen, "-y", "-f", kf],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # ssh-keygen -y prints: "ssh-ed25519 <base64> [comment]"
            keygen_b64 = proc.stdout.split()[1]
            ours_b64 = akline.split()[1]
            self.assertEqual(keygen_b64, ours_b64)


class KeyStoreTest(unittest.TestCase):
    def test_generate_list_select(self):
        with tempfile.TemporaryDirectory() as d:
            ks = dk.KeyStore(d)
            r1 = ks.generate("alpha")
            self.assertEqual(r1["name"], "alpha")
            self.assertTrue(r1["fingerprint"].startswith("SHA256:"))
            self.assertEqual(ks.active(), "alpha")  # first generated is active

            r2 = ks.generate("beta")
            self.assertEqual(r2["name"], "beta")
            self.assertEqual(ks.active(), "beta")  # newest generated is active

            names = {k["name"] for k in ks.list()}
            self.assertEqual(names, {"alpha", "beta"})
            actives = {k["name"] for k in ks.list() if k["active"]}
            self.assertEqual(actives, {"beta"})

            ks.select("alpha")
            self.assertEqual(ks.active(), "alpha")
            self.assertEqual(ks.active_private_path(), ks.private_path("alpha"))
            self.assertTrue(ks.active_public_line().startswith("ssh-ed25519 "))

    def test_private_files_0600(self):
        with tempfile.TemporaryDirectory() as d:
            ks = dk.KeyStore(d)
            ks.generate("k")
            mode = stat.S_IMODE(os.stat(ks.private_path("k")).st_mode)
            self.assertEqual(mode, 0o600)

    def test_select_missing_errors(self):
        with tempfile.TemporaryDirectory() as d:
            ks = dk.KeyStore(d)
            with self.assertRaises(FileNotFoundError):
                ks.select("nope")

    def test_duplicate_name_errors(self):
        with tempfile.TemporaryDirectory() as d:
            ks = dk.KeyStore(d)
            ks.generate("dup")
            with self.assertRaises(FileExistsError):
                ks.generate("dup")

    def test_bad_name_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ks = dk.KeyStore(d)
            for bad in ["", "..", "a/b", "x\\y", "with space"]:
                with self.assertRaises(ValueError):
                    ks.generate(bad)

    def test_stored_pub_matches_private(self):
        with tempfile.TemporaryDirectory() as d:
            ks = dk.KeyStore(d)
            ks.generate("m")
            with open(ks.private_path("m")) as f:
                pem = f.read()
            pub_from_priv = dk.parse_openssh_private_key(pem)
            akline = ks.public_line("m")
            import struct

            blob = base64.b64decode(akline.split()[1])
            (n,) = struct.unpack(">I", blob[:4])
            pos = 4 + n
            (mm,) = struct.unpack(">I", blob[pos : pos + 4])
            pos += 4
            self.assertEqual(blob[pos : pos + mm], pub_from_priv)


if __name__ == "__main__":
    unittest.main()
