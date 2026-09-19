"""Unit tests for flash-time per-card config (app/deploy_engine/flash_config.py).

Loaded by file path (stdlib-only module) so the test stays hermetic — no deploy
toolchain import, no mounting, no hardware. Covers hostname validation, the
NetworkManager keyfile rendering (WPA vs open), the filesystem drop into a
tmpdir, and the settings-gated password cache (the PSK is persisted only when the
"remember" toggle is on; hostname/SSID always).
"""

import importlib.util
import os
import tempfile
import unittest


def _load():
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), here, os.path.dirname(here)):
        p = os.path.join(base, "app", "deploy_engine", "flash_config.py")
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("flash_config", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise FileNotFoundError("app/deploy_engine/flash_config.py")


fc = _load()


class HostnameTest(unittest.TestCase):
    def test_accepts(self):
        for name in ["tdplayer", "a", "pi-01", "td-player-9", "x" * 63, "abc123"]:
            self.assertTrue(fc.valid_hostname(name), name)

    def test_normalizes_then_accepts(self):
        # Uppercase + surrounding whitespace are normalized (like the image's tr).
        self.assertTrue(fc.valid_hostname("  TDPlayer  "))
        self.assertEqual(fc.normalize_hostname("  TDPlayer  "), "tdplayer")

    def test_rejects(self):
        for name in [
            "",
            "   ",
            "-lead",
            "trail-",
            "has_underscore",
            "has space",
            "dot.dot",
            "x" * 64,
            "bad!char",
        ]:
            self.assertFalse(fc.valid_hostname(name), name)


class RenderTest(unittest.TestCase):
    def test_wpa_psk(self):
        out = fc.render_nmconnection("MyNet", "s3cret")
        self.assertIn("id=seed-MyNet", out)
        self.assertIn("ssid=MyNet", out)
        self.assertIn("[wifi-security]", out)
        self.assertIn("key-mgmt=wpa-psk", out)
        self.assertIn("psk=s3cret", out)
        self.assertIn("[ipv4]\nmethod=auto", out)
        self.assertIn("[ipv6]\nmethod=auto", out)
        self.assertTrue(out.endswith("\n"))

    def test_open_network_omits_security(self):
        for psk in (None, "", 0, False):
            out = fc.render_nmconnection("OpenNet", psk)
            self.assertIn("ssid=OpenNet", out)
            self.assertNotIn("[wifi-security]", out)
            self.assertNotIn("key-mgmt", out)
            self.assertNotIn("psk=", out)

    def test_section_order(self):
        out = fc.render_nmconnection("Net", "pw")
        i = out.index
        self.assertLess(i("[connection]"), i("[wifi]"))
        self.assertLess(i("[wifi]"), i("[wifi-security]"))
        self.assertLess(i("[wifi-security]"), i("[ipv4]"))
        self.assertLess(i("[ipv4]"), i("[ipv6]"))


class WriteBootConfigTest(unittest.TestCase):
    def test_writes_hostname_and_networks(self):
        with tempfile.TemporaryDirectory() as d:
            written = fc.write_boot_config(
                d,
                hostname="TDPlayer",
                networks=[
                    {"ssid": "Home Wi-Fi", "psk": "pw1"},
                    {"ssid": "GuestOpen"},
                ],
            )
            hn = os.path.join(d, "td-hostname")
            self.assertTrue(os.path.exists(hn))
            with open(hn) as f:
                self.assertEqual(f.read(), "tdplayer\n")  # normalized

            conn = os.path.join(d, "system-connections")
            files = sorted(os.listdir(conn))
            # Slugged filenames (space -> '-', lowercased).
            self.assertIn("home-wi-fi.nmconnection", files)
            self.assertIn("guestopen.nmconnection", files)

            with open(os.path.join(conn, "home-wi-fi.nmconnection")) as f:
                self.assertIn("psk=pw1", f.read())
            with open(os.path.join(conn, "guestopen.nmconnection")) as f:
                self.assertNotIn("[wifi-security]", f.read())

            self.assertEqual(len(written), 3)

    def test_perms_0600_on_keyfiles(self):
        with tempfile.TemporaryDirectory() as d:
            fc.write_boot_config(d, networks=[{"ssid": "Net", "psk": "pw"}])
            p = os.path.join(d, "system-connections", "net.nmconnection")
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_invalid_hostname_not_written(self):
        with tempfile.TemporaryDirectory() as d:
            fc.write_boot_config(d, hostname="bad_host!", networks=None)
            self.assertFalse(os.path.exists(os.path.join(d, "td-hostname")))

    def test_no_args_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(fc.write_boot_config(d), [])
            self.assertEqual(os.listdir(d), [])

    def test_blank_ssid_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            written = fc.write_boot_config(d, networks=[{"ssid": "  ", "psk": "x"}])
            self.assertEqual(written, [])
            self.assertFalse(os.path.exists(os.path.join(d, "system-connections")))

    def test_slug_collision_distinct_files(self):
        with tempfile.TemporaryDirectory() as d:
            fc.write_boot_config(
                d,
                networks=[{"ssid": "My Net"}, {"ssid": "My/Net"}],
            )
            conn = os.path.join(d, "system-connections")
            self.assertEqual(len(os.listdir(conn)), 2)


# The password-cache gate, modeled exactly as the renderer persists it: hostname
# and SSID are always saved; the Wi-Fi PASSWORD is saved only when the "remember"
# toggle is on. This is the pure decision the UI encodes (renderer.js saveSettings).
def cache_payload(hostname, ssid, psk, remember_pw):
    return {
        "flashHostname": hostname or "",
        "flashSsid": ssid or "",
        "flashPsk": (psk if remember_pw else "") or "",
    }


class PasswordCacheGateTest(unittest.TestCase):
    def test_psk_persisted_only_when_toggle_on(self):
        on = cache_payload("tdplayer", "Home", "secret", remember_pw=True)
        self.assertEqual(on["flashPsk"], "secret")
        # hostname/SSID always present regardless of the toggle
        self.assertEqual(on["flashHostname"], "tdplayer")
        self.assertEqual(on["flashSsid"], "Home")

    def test_psk_dropped_when_toggle_off(self):
        off = cache_payload("tdplayer", "Home", "secret", remember_pw=False)
        self.assertEqual(off["flashPsk"], "")
        # but hostname + SSID are still remembered
        self.assertEqual(off["flashHostname"], "tdplayer")
        self.assertEqual(off["flashSsid"], "Home")


if __name__ == "__main__":
    unittest.main()
