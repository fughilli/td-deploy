"""Tests for the SD flasher — the safety-critical path.

These cover the parsers (removable-only enumeration from captured OS output), the
privileged raw-write worker (byte parity + refusing a missing device), and the
top-level guard that refuses anything that isn't a live removable disk. All
stdlib-only, no hardware.
"""

import os
import plistlib
import tempfile

from deploy_engine.flasher import disks, flash, rawwrite


def test_macos_parser_surfaces_external_only():
    list_pl = plistlib.dumps({"WholeDisks": ["disk4"]})
    info4 = plistlib.dumps(
        {
            "Internal": False,
            "Ejectable": True,
            "MediaName": "SD Card Reader",
            "TotalSize": 32_000_000_000,
            "BusProtocol": "USB",
            "RemovableMediaOrExternalDevice": True,
        }
    )
    ds = disks.parse_macos(list_pl, {"disk4": info4})
    assert len(ds) == 1
    assert ds[0].id == "/dev/rdisk4"  # raw node for fast writes
    assert ds[0].bus == "USB"


def test_macos_parser_drops_internal_disk():
    list_pl = plistlib.dumps({"WholeDisks": ["disk0"]})
    info0 = plistlib.dumps({"Internal": True, "TotalSize": 1, "MediaName": "APPLE SSD"})
    assert disks.parse_macos(list_pl, {"disk0": info0}) == []


def test_windows_parser_drops_system_disk():
    js = (
        '[{"Number":1,"FriendlyName":"SanDisk Ultra","Size":32000000000,'
        '"BusType":"USB","IsSystem":false,"IsBoot":false},'
        '{"Number":0,"FriendlyName":"NVMe","Size":512000000000,'
        '"BusType":"USB","IsSystem":true,"IsBoot":true}]'
    )
    dw = disks.parse_windows(js)
    assert len(dw) == 1
    assert dw[0].id == r"\\.\PhysicalDrive1"


def test_windows_parser_accepts_single_object():
    one = (
        '{"Number":2,"FriendlyName":"SD","Size":8000000000,'
        '"BusType":"SD","IsSystem":false,"IsBoot":false}'
    )
    assert len(disks.parse_windows(one)) == 1


def test_linux_parser_removable_only():
    lj = (
        '{"blockdevices":['
        '{"name":"sda","model":"WDC","size":500000000000,"rm":false,'
        '"hotplug":false,"type":"disk"},'
        '{"name":"sdb","model":"Cruzer","size":16000000000,"rm":true,'
        '"hotplug":true,"type":"disk"}]}'
    )
    dl = disks.parse_linux(lj)
    assert len(dl) == 1
    assert dl[0].id == "/dev/sdb"


def test_rawwrite_byte_parity():
    img = tempfile.mktemp()
    dev = tempfile.mktemp()
    prog = tempfile.mktemp()
    with open(img, "wb") as f:
        f.write(os.urandom(20 << 20))
    open(dev, "wb").close()  # the device already exists in the real world
    assert rawwrite.raw_write(img, dev, prog) == 0
    assert open(img, "rb").read() == open(dev, "rb").read()
    lines = open(prog).read().splitlines()
    assert lines[-1].startswith("DONE")
    assert any(line.split()[0].isdigit() for line in lines[:-1])


def test_rawwrite_refuses_missing_device():
    img = tempfile.mktemp()
    prog = tempfile.mktemp()
    with open(img, "wb") as f:
        f.write(b"x")
    assert rawwrite.main([img, "/tmp/does-not-exist-dev-xyz", prog]) == 1
    assert "ERROR" in open(prog).read()


def test_flash_refuses_unknown_disk():
    try:
        flash("/nonexistent.img", "/dev/definitely-not-a-disk")
    except (ValueError, FileNotFoundError):
        return
    raise AssertionError("flash() should refuse a non-removable / missing disk")
