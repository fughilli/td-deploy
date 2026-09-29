"""Player kinds and per-deploy architecture detection: what `uname -m` on the box
means for the build (target, native triple, Python host), and the image a
release carries for each kind. Offline: ssh / compile / push are stubbed."""

import deploy_engine as de
import pytest
from deploy_engine import detect, download, players


def test_arch_aliases():
    assert players.for_arch("aarch64").kind == "pi3"
    assert players.for_arch("arm64").kind == "pi3"
    assert players.for_arch("x86_64\n").kind == "amd64"
    assert players.for_arch("AMD64").kind == "amd64"
    with pytest.raises(ValueError):
        players.for_arch("armv7l")


def test_probe_parsing():
    t = detect.parse_probe("box", "x86_64\nprepare\n")
    assert t.player.kind == "amd64" and t.has_prepare
    t = detect.parse_probe("pi", "aarch64\n-\n")
    assert t.player.kind == "pi3" and not t.has_prepare
    with pytest.raises(RuntimeError):
        detect.parse_probe("x", "")


def test_target_resolution():
    pi, x86 = players.PLAYERS["pi3"], players.PLAYERS["amd64"]
    assert detect.resolve_target("auto", pi) == "gles2"
    assert detect.resolve_target(None, x86) == "desktop_gl"
    assert detect.resolve_target("gles", pi) == "gles"  # Pi 4/5 choice honoured
    assert detect.resolve_target("gles2", x86) == "desktop_gl"  # can't run there
    assert detect.resolve_target("desktop_gl", pi) == "gles2"


def test_release_image_selection():
    A = download.Asset
    rel = download.Release(
        "v3",
        [
            A("tdplayer-pi3.img.zst", "u", 1, None),
            A("tdplayer-amd64.iso.zst.part01", "b", 1, None),
            A("tdplayer-amd64.iso.zst.part00", "a", 1, None),
            A("tdplayer-amd64.iso.zst.sha256", "s", 1, None),
        ],
    )
    assert [a.name for a in download.select_image(rel, "pi3")] == ["tdplayer-pi3.img.zst"]
    parts = download.select_image(rel, "amd64")
    assert [a.name for a in parts] == [
        "tdplayer-amd64.iso.zst.part00",
        "tdplayer-amd64.iso.zst.part01",
    ]
    assert download._whole_name(parts) == "tdplayer-amd64.iso.zst"
    legacy = download.Release("v1", [A("tdplayer.img.zst", "u", 1, None)])
    assert download.select_image(legacy, "pi3")[0].name == "tdplayer.img.zst"
    with pytest.raises(FileNotFoundError):
        download.select_image(legacy, "amd64")


class _Calls:
    def __init__(self, monkeypatch, machine, has_prepare=True, python_host=False):
        self.compiled = self.finished = self.pushed = None
        monkeypatch.setattr(
            de,
            "probe",
            lambda host, **kw: detect.Target(host, machine, players.for_arch(machine), has_prepare),
        )

        def compile_toe(toe, art, **kw):
            self.compiled = kw
            return {"python_host": python_host and kw["host_mode"] == "auto"}

        def finish(art, target, tc, progress):
            self.finished = (target, tc.triple)

        def push(art, host, **kw):
            self.pushed = kw
            return "/var/lib/tdplayer/staging-x"

        monkeypatch.setattr(de, "compile_toe", compile_toe)
        monkeypatch.setattr(de, "finish", finish)
        monkeypatch.setattr(de, "push", push)


def test_deploy_to_a_pi(monkeypatch, tmp_path):
    c = _Calls(monkeypatch, "aarch64", python_host=True)
    res = de.deploy("p.toe", "tdplayer.local", artifact_dir=str(tmp_path))
    assert res["player"] == "pi3"
    assert c.compiled["target"] == "gles2" and c.compiled["host_mode"] == "off"
    assert c.finished == ("gles2", "aarch64-unknown-linux-gnu")
    assert c.pushed["pre_restart"] is None


def test_deploy_to_an_x86_box(monkeypatch, tmp_path):
    c = _Calls(monkeypatch, "x86_64")
    res = de.deploy("p.toe", "showbox.local", target="gles2", artifact_dir=str(tmp_path))
    assert res["player"] == "amd64"
    assert c.compiled["target"] == "desktop_gl" and c.compiled["host_mode"] == "auto"
    assert c.finished == ("desktop_gl", "x86_64-unknown-linux-gnu")
    assert c.pushed["pre_restart"] is None


def test_python_host_project_on_x86(monkeypatch, tmp_path):
    c = _Calls(monkeypatch, "x86_64", python_host=True)
    de.deploy("p.toe", "showbox.local", artifact_dir=str(tmp_path))
    assert c.finished is None  # nothing to codegen for a host artifact
    assert c.pushed["pre_restart"] == detect.PREPARE


def test_python_host_needs_a_current_image(monkeypatch, tmp_path):
    _Calls(monkeypatch, "x86_64", has_prepare=False, python_host=True)
    with pytest.raises(RuntimeError, match="reflash"):
        de.deploy("p.toe", "showbox.local", artifact_dir=str(tmp_path))
