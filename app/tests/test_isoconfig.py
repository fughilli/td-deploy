"""The x86 installer USB's flash-time config: TDCONFIG.JSN written into the
hybrid ISO's EFI FAT partition as block patches the raw writer applies in-stream.

Images are built here (a tiny FAT formatter + MBR/GPT writers mirroring what
xorriso -isohybrid-mbr -isohybrid-gpt-basdat produces), so this needs no mkfs or
ISO tooling; where mtools / fsck.vfat are installed they cross-check the result
(skipped otherwise). Stdlib only, no hardware.
"""

import json
import os
import shutil
import struct
import subprocess
import tempfile

import pytest
from deploy_engine.flasher import is_installer_image, isoconfig, rawwrite

SECTOR = 512


# ------------------------------------------------------------------ fixtures
HAVE_MKFS = bool(shutil.which("mkfs.vfat") and shutil.which("mcopy"))


def make_fat(total_sectors, bits, spc, label="EFIBOOT", files=()):
    """A FAT12/16/32 volume (bytes) with 8.3 files in its root: made by mkfs.vfat +
    mcopy when installed (the real thing, like nixpkgs' efiboot.img), else by the
    minimal formatter below."""
    if HAVE_MKFS:
        return _make_fat_mkfs(total_sectors, bits, spc, label, files)
    return _make_fat_builtin(total_sectors, bits, spc, label, files)


def _make_fat_mkfs(total_sectors, bits, spc, label, files):
    d = tempfile.mkdtemp()
    try:
        img = os.path.join(d, "fat.img")
        with open(img, "wb") as fh:
            fh.truncate(total_sectors * SECTOR)
        subprocess.run(
            ["mkfs.vfat", "-F", str(bits), "-s", str(spc), "-n", label, "-i", "12345678", img],
            check=True,
            capture_output=True,
        )
        env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
        for name, data in files:
            src = _write(os.path.join(d, name), data)
            subprocess.run(["mcopy", "-i", img, src, "::/" + name], check=True, env=env)
        with open(img, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _make_fat_builtin(total_sectors, bits, spc, label, files):
    bps, rsvd, nfats = SECTOR, (32 if bits == 32 else 1), 2
    root_ent = 0 if bits == 32 else 512
    root_secs = root_ent * 32 // bps
    # FAT size: iterate to a fixed point
    fatsz = 1
    while True:
        data_secs = total_sectors - rsvd - nfats * fatsz - root_secs
        clusters = data_secs // spc
        need = {12: (clusters + 2) * 3 // 2 + 1, 16: (clusters + 2) * 2, 32: (clusters + 2) * 4}[
            bits
        ]
        n = (need + bps - 1) // bps
        if n <= fatsz:
            break
        fatsz = n
    img = bytearray(total_sectors * bps)
    bs = bytearray(512)
    bs[0:3] = b"\xeb\x3c\x90"
    bs[3:11] = b"TESTFAT "
    struct.pack_into(
        "<HBHBHHBH",
        bs,
        11,
        bps,
        spc,
        rsvd,
        nfats,
        root_ent,
        total_sectors if total_sectors < 65536 and bits != 32 else 0,
        0xF8,
        fatsz if bits != 32 else 0,
    )
    struct.pack_into(
        "<HHII", bs, 24, 32, 64, 0, total_sectors if (total_sectors >= 65536 or bits == 32) else 0
    )
    if bits == 32:
        struct.pack_into("<IHHIHH", bs, 36, fatsz, 0, 0, 2, 1, 6)
        bs[64] = 0x80
        bs[66] = 0x29
        bs[71:82] = label.ljust(11).encode()
        bs[82:90] = b"FAT32   "
    else:
        bs[36] = 0x80
        bs[38] = 0x29
        bs[43:54] = label.ljust(11).encode()
        bs[54:62] = b"FAT12   " if bits == 12 else b"FAT16   "
    bs[510:512] = b"\x55\xaa"
    img[0:512] = bs
    if bits == 32:  # FSInfo
        fs = bytearray(512)
        fs[0:4] = b"RRaA"
        fs[484:488] = b"rrAa"
        struct.pack_into("<II", fs, 488, 0xFFFFFFFF, 0xFFFFFFFF)
        fs[510:512] = b"\x55\xaa"
        img[512:1024] = fs
    ov_fat = [rsvd * bps + i * fatsz * bps for i in range(nfats)]

    def fat_set(n, v):
        for base in ov_fat:
            if bits == 12:
                off = base + n + n // 2
                cur = struct.unpack_from("<H", img, off)[0]
                new = (cur & 0x000F) | (v << 4) if n & 1 else (cur & 0xF000) | (v & 0xFFF)
                struct.pack_into("<H", img, off, new & 0xFFFF)
            elif bits == 16:
                struct.pack_into("<H", img, base + n * 2, v & 0xFFFF)
            else:
                struct.pack_into("<I", img, base + n * 4, v & 0x0FFFFFFF)

    eoc = {12: 0xFFF, 16: 0xFFFF, 32: 0x0FFFFFFF}[bits]
    fat_set(0, {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[bits])
    fat_set(1, eoc)
    first_data = rsvd + nfats * fatsz + root_secs
    cbytes = spc * bps
    nxt = 2
    root_off = (rsvd + nfats * fatsz) * bps
    if bits == 32:
        fat_set(2, eoc)  # root dir cluster
        root_off = first_data * bps
        nxt = 3
    for i, (name, data) in enumerate(files):
        n = max(1, -(-len(data) // cbytes))
        cl = list(range(nxt, nxt + n))
        nxt += n
        for a, b in zip(cl, cl[1:]):
            fat_set(a, b)
        fat_set(cl[-1], eoc)
        for j, c in enumerate(cl):
            off = (first_data + (c - 2) * spc) * bps
            chunk = data[j * cbytes : (j + 1) * cbytes]
            img[off : off + len(chunk)] = chunk
        e = isoconfig._name83(name) + struct.pack(
            "<BBBHHHHHHHI",
            0x20,
            0,
            0,
            0,
            0x21,
            0x21,
            cl[0] >> 16,
            0,
            0x21,
            cl[0] & 0xFFFF,
            len(data),
        )
        img[root_off + 32 * i : root_off + 32 * (i + 1)] = e
    return bytes(img)


def make_hybrid_iso(fat: bytes, *, gpt=True, pad_after=64 * 1024):
    """An 'ISO' with the FAT embedded at a 2 KiB-aligned offset, described by an MBR
    (type 0xEF) and, like xorriso's -isohybrid-gpt-basdat, a GPT."""
    fat_lba = 136  # where xorriso typically lands the El Torito EFI image
    size = fat_lba * SECTOR + len(fat) + pad_after
    img = bytearray(size)
    img[0x8001:0x8006] = b"CD001"  # ISO 9660 PVD signature
    img[fat_lba * SECTOR : fat_lba * SECTOR + len(fat)] = fat
    mbr = bytearray(512)
    struct.pack_into("<B3sB3sII", mbr, 446, 0x80, b"\0\0\0", 0x00, b"\0\0\0", 0, size // SECTOR)
    struct.pack_into(
        "<B3sB3sII", mbr, 462, 0, b"\0\0\0", 0xEF, b"\0\0\0", fat_lba, len(fat) // SECTOR
    )
    mbr[510:512] = b"\x55\xaa"
    img[0:512] = mbr
    if gpt:
        hdr = bytearray(92)
        hdr[0:8] = b"EFI PART"
        struct.pack_into("<QII", hdr, 72, 2, 4, 128)
        img[512:604] = hdr
        e0 = bytearray(128)
        e0[0:16] = b"\x11" * 16
        struct.pack_into("<QQ", e0, 32, 64, size // SECTOR - 1)
        e1 = bytearray(128)
        e1[0:16] = b"\x22" * 16
        struct.pack_into("<QQ", e1, 32, fat_lba, fat_lba + len(fat) // SECTOR - 1)
        img[1024:1152] = e0
        img[1152:1280] = e1
    return bytes(img), fat_lba * SECTOR


@pytest.fixture()
def tmp():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _stream(image, patches):
    """What rawwrite puts on the device: the image in chunks, patches overlaid."""
    out = bytearray()
    with open(image, "rb") as fh:
        done = 0
        while True:
            c = fh.read(1 << 16)  # small chunks: patches straddle chunk edges
            if not c:
                break
            out += rawwrite.apply_patches(c, done, patches)
            done += len(c)
    return bytes(out)


def _read_back(data, fat_off, name=isoconfig.CONFIG_NAME):
    p = os.path.join(tempfile.mkdtemp(), "x.img")
    _write(p, data)
    with open(p, "rb") as fh:
        ov = isoconfig.Overlay(fh, len(data))
        return isoconfig.Fat(ov, fat_off).read_file(name)


# ------------------------------------------------------------------ tests
@pytest.mark.parametrize(
    "bits,sectors,spc",
    [(12, 8192, 4), (16, 65536, 1), (32, 140000, 1)],
)
def test_config_lands_in_every_fat_flavour(tmp, bits, sectors, spc):
    efi = b"MZ" + os.urandom(70000)
    fat = make_fat(sectors, bits, spc, files=[("BOOTX64.EFI", efi)])
    iso, fat_off = make_hybrid_iso(fat)
    image = _write(os.path.join(tmp, "installer.iso"), iso)
    payload = isoconfig.render_payload(
        "Stage-Left", [{"ssid": "Venue WiFi", "psk": "hunter22"}], ["ssh-ed25519 AAAAC3 me@mac"]
    )
    patches = isoconfig.config_patches(image, payload)

    assert all(off % isoconfig.BLOCK == 0 and len(d) == isoconfig.BLOCK for off, d in patches)
    written = _stream(image, patches)
    assert len(written) == len(iso)
    got = _read_back(written, fat_off)
    cfg = json.loads(got)
    assert cfg["hostname"] == "stage-left"
    assert cfg["authorized_keys"] == ["ssh-ed25519 AAAAC3 me@mac"]
    assert "psk=hunter22" in cfg["connections"]["venue-wifi.nmconnection"]
    # the boot loader is untouched, and nothing outside the FAT changed
    assert _read_back(written, fat_off, "BOOTX64.EFI") == efi
    assert written[:fat_off] == iso[:fat_off]
    assert written[fat_off + len(fat) :] == iso[fat_off + len(fat) :]
    _cross_check(tmp, written[fat_off : fat_off + len(fat)])


def test_rewrite_replaces_the_file_and_frees_its_clusters(tmp):
    fat = make_fat(8192, 12, 4, files=[("BOOTX64.EFI", b"x" * 5000)])
    iso, fat_off = make_hybrid_iso(fat)
    image = _write(os.path.join(tmp, "a.iso"), iso)
    big = json.dumps({"hostname": "a", "pad": "y" * 9000}).encode()
    once = _write(os.path.join(tmp, "b.iso"), _stream(image, isoconfig.config_patches(image, big)))
    twice = _stream(once, isoconfig.config_patches(once, b'{"hostname": "b"}'))
    assert json.loads(_read_back(twice, fat_off)) == {"hostname": "b"}
    with open(once, "rb") as fh:
        before = isoconfig.Fat(isoconfig.Overlay(fh, len(iso)), fat_off)
        used_before = sum(1 for c in range(2, before.nclusters + 2) if before.get(c))
    p = _write(os.path.join(tmp, "c.iso"), twice)
    with open(p, "rb") as fh:
        after = isoconfig.Fat(isoconfig.Overlay(fh, len(iso)), fat_off)
        used_after = sum(1 for c in range(2, after.nclusters + 2) if after.get(c))
    assert used_after < used_before  # the 9 KB file's chain was released


def test_mbr_only_image_and_efi_label_preference(tmp):
    other = make_fat(4096, 12, 4, label="DATA")
    efi = make_fat(8192, 12, 4, label="EFIBOOT")
    iso, efi_off = make_hybrid_iso(efi, gpt=False)
    # a second FAT (not EFIBOOT) listed first in the MBR must not be picked
    iso = bytearray(iso + bytes(len(other)))
    other_off = len(iso) - len(other)
    iso[other_off:] = other
    struct.pack_into(
        "<B3sB3sII",
        iso,
        446,
        0,
        b"\0\0\0",
        0x0C,
        b"\0\0\0",
        other_off // SECTOR,
        len(other) // SECTOR,
    )
    image = _write(os.path.join(tmp, "m.iso"), bytes(iso))
    with open(image, "rb") as fh:
        assert isoconfig.find_efi_fat(isoconfig.Overlay(fh, len(iso))) == efi_off


def test_no_fat_partition_is_an_error(tmp):
    image = _write(os.path.join(tmp, "blank.iso"), bytes(1 << 20))
    with pytest.raises(ValueError):
        isoconfig.config_patches(image, b"{}")


def test_nothing_to_write_means_no_payload():
    assert isoconfig.render_payload(None, [], []) is None
    assert isoconfig.render_payload("bad_name!", None, None) is None


def test_raw_write_applies_a_patch_file(tmp):
    fat = make_fat(8192, 12, 4)
    iso, fat_off = make_hybrid_iso(fat)
    image = _write(os.path.join(tmp, "i.iso"), iso)
    patches = isoconfig.config_patches(image, b'{"hostname": "x"}')
    pf = os.path.join(tmp, "p.patch")
    isoconfig.write_patch_file(pf, patches)
    assert rawwrite.read_patch_file(pf) == patches
    dev = _write(os.path.join(tmp, "dev"), bytes(len(iso) + 4096))
    prog = os.path.join(tmp, "prog")
    assert rawwrite.main([image, dev, prog, pf]) == 0
    with open(dev, "rb") as fh:
        data = fh.read(len(iso))
    assert json.loads(_read_back(data, fat_off)) == {"hostname": "x"}


def test_installer_detection(tmp):
    iso, _ = make_hybrid_iso(make_fat(8192, 12, 4))
    assert is_installer_image(_write(os.path.join(tmp, "renamed.img"), iso))
    assert is_installer_image(os.path.join(tmp, "whatever.iso"))
    assert not is_installer_image(_write(os.path.join(tmp, "sd.img"), bytes(64 * 1024)))


def _cross_check(tmp, fat_bytes):
    """Independent readers, when installed: fsck.vfat and mtools (on a volume
    mkfs.vfat made — the built-in fixture is minimal, not fsck-clean)."""
    if not HAVE_MKFS:
        return
    part = _write(os.path.join(tmp, "part.img"), fat_bytes)
    if shutil.which("fsck.vfat"):
        r = subprocess.run(["fsck.vfat", "-n", part], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
    if shutil.which("mtype"):
        r = subprocess.run(
            ["mtype", "-i", part, "::/TDCONFIG.JSN"],
            capture_output=True,
            env=dict(os.environ, MTOOLS_SKIP_CHECK="1"),
        )
        assert r.returncode == 0 and json.loads(r.stdout)["hostname"] == "stage-left"
