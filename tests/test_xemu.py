"""xemu disc parsing, HDD image handling, and FATX save sync.

The FATX tests run against a real image built by pyfatx rather than a stub:
libfatx compares path names byte for byte, and that detail is exactly what the
inject and extract hooks have to get right.
"""

import gc
import logging
import os
import signal
import struct
import tomllib
from pathlib import Path
from typing import Any, NoReturn, Optional

import pytest
from pyfatx import Fatx

from webstation_broker import settings
from webstation_broker.emulators import xemu

SECTOR = 2048


# ── Disc images ──────────────────────────────────────────────────────────────


def _xiso(path: Path, title_id: int = 0x4D530064, *, base: int = 0,
          xbe_name: bytes = b"default.xbe") -> Path:
    """Write a minimal XISO image and return its path.

    The image holds a volume descriptor, a one-entry root directory table, and
    an XBE whose certificate carries `title_id`.

    Args:
        path: Where the image is written.
        title_id: Title id stamped into the XBE certificate.
        base: Byte offset of the game partition, mimicking a disc that carries a
            video partition up front.
        xbe_name: Name of the single root directory entry.

    Returns:
        The path the image was written to.
    """
    root_sector, xbe_sector, xbe_size = 33, 34, 0x200
    image = bytearray((base + (xbe_sector + 1) * SECTOR))

    vd = bytearray(SECTOR)
    vd[: len(xemu._XISO_MAGIC)] = xemu._XISO_MAGIC
    vd[20:24] = struct.pack("<I", root_sector)
    vd[24:28] = struct.pack("<I", SECTOR)
    image[base + 32 * SECTOR : base + 33 * SECTOR] = vd

    entry = bytearray(SECTOR)
    entry[0:2] = struct.pack("<H", 0)  # left
    entry[2:4] = struct.pack("<H", 0)  # right
    entry[4:8] = struct.pack("<I", xbe_sector)
    entry[8:12] = struct.pack("<I", xbe_size)
    entry[13] = len(xbe_name)
    entry[14 : 14 + len(xbe_name)] = xbe_name
    image[base + root_sector * SECTOR : base + (root_sector + 1) * SECTOR] = entry

    xbe = bytearray(xbe_size)
    xbe[0:4] = b"XBEH"
    xbe[0x104:0x108] = struct.pack("<I", 0x10000)
    xbe[0x118:0x11C] = struct.pack("<I", 0x10100)
    xbe[0x108:0x10C] = struct.pack("<I", title_id)  # certificate title id
    off = base + xbe_sector * SECTOR
    image[off : off + xbe_size] = xbe

    path.write_bytes(bytes(image))
    return path


def test_title_id_is_read_from_the_xbe_certificate(tmp_path: Path) -> None:
    """The title id is read out of the XBE certificate on the disc."""
    assert xemu._disc_title_id(_xiso(tmp_path / "g.iso")) == "4D530064"


def test_title_id_is_uppercase_like_the_dashboard_writes_it(tmp_path: Path) -> None:
    """The title id is formatted in uppercase, the way the dashboard names save directories.

    The dashboard creates E:/UDATA/<id> in uppercase, and a directory this
    code creates has to be named the same way.
    """
    assert xemu._disc_title_id(_xiso(tmp_path / "g.iso", 0x0000ABCD)) == "0000ABCD"


def test_title_id_reads_a_disc_with_a_video_partition_up_front(tmp_path: Path) -> None:
    """A disc whose game partition sits behind a video partition still yields its title id."""
    disc = _xiso(tmp_path / "g.iso", base=0x18300000)
    assert xemu._disc_title_id(disc) == "4D530064"


def test_title_id_is_none_when_the_disc_has_no_volume_descriptor(tmp_path: Path) -> None:
    """A file with no XISO volume descriptor yields no title id."""
    junk = tmp_path / "junk.iso"
    junk.write_bytes(b"\x00" * (40 * SECTOR))
    assert xemu._disc_title_id(junk) is None


def test_title_id_is_none_when_the_root_holds_no_default_xbe(tmp_path: Path) -> None:
    """A disc whose root directory lacks default.xbe yields no title id."""
    disc = _xiso(tmp_path / "g.iso", xbe_name=b"nothere.xbe")
    assert xemu._disc_title_id(disc) is None


def test_title_id_is_none_for_a_missing_file(tmp_path: Path) -> None:
    """A missing disc file yields no title id."""
    assert xemu._disc_title_id(tmp_path / "gone.iso") is None


@pytest.mark.parametrize("claimed", [0xFFFFFFFF, xemu.XISO_MAX_DIR_BYTES + 1, 0])
def test_a_disc_claiming_an_absurd_root_directory_is_not_read(tmp_path: Path, claimed: int) -> None:
    """A disc whose root directory size is out of range is refused, not allocated for.

    The size is a 32-bit field taken straight from the image, so reading it
    verbatim lets a hostile ISO ask for a 4 GiB buffer.
    """
    disc = _xiso(tmp_path / "g.iso")
    raw = bytearray(disc.read_bytes())
    off = 32 * SECTOR + 24
    raw[off:off + 4] = struct.pack("<I", claimed)
    disc.write_bytes(bytes(raw))

    assert xemu._disc_title_id(disc) is None


# ── ROM resolution ───────────────────────────────────────────────────────────


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the xemu ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(settings, "ROM_ROOT", root)
    return root


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Game.iso", 1),
        ("Game (Disc 2).iso", 2),
        ("Game.disk3.iso", 3),
        ("Game_cd-4.iso", 4),
    ],
)
def test_disc_number_is_read_from_the_name(name: str, expected: int) -> None:
    """The disc number is parsed from the usual markers in a file name."""
    assert xemu._disc_number(Path(name)) == expected


def test_a_disc_set_boots_disc_one(rom_root: Path) -> None:
    """A folder holding several discs resolves to disc one."""
    folder = rom_root / "Game"
    folder.mkdir()
    for name in ("Game (Disc 2).iso", "Game (Disc 1).iso"):
        (folder / name).write_bytes(b"x")
    assert xemu.Xemu.resolve_rom_file(None, folder).name == "Game (Disc 1).iso"


def test_a_file_path_resolves_to_itself(rom_root: Path) -> None:
    """A path that is already a file resolves to itself."""
    disc = rom_root / "Game.iso"
    disc.write_bytes(b"x")
    assert xemu.Xemu.resolve_rom_file(None, disc) == disc


def test_non_iso_files_are_not_bootable(rom_root: Path) -> None:
    """A folder holding no .iso resolves to nothing."""
    folder = rom_root / "Game"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"x")
    assert xemu.Xemu.resolve_rom_file(None, folder) is None


def test_a_symlink_out_of_the_rom_root_is_rejected(rom_root: Path, tmp_path: Path) -> None:
    """A disc symlinked from outside the ROM root is not bootable."""
    outside = tmp_path / "elsewhere.iso"
    outside.write_bytes(b"x")
    folder = rom_root / "Game"
    folder.mkdir()
    (folder / "Game.iso").symlink_to(outside)
    assert xemu.Xemu.resolve_rom_file(None, folder) is None


def test_a_direct_path_that_is_a_symlink_out_of_the_rom_root_is_rejected(
    rom_root: Path, tmp_path: Path
) -> None:
    """A direct path that is a symlink out of the ROM root is rejected."""
    outside = tmp_path / "elsewhere.iso"
    outside.write_bytes(b"x")
    linked = rom_root / "Game.iso"
    linked.symlink_to(outside)
    assert xemu.Xemu.resolve_rom_file(None, linked) is None


# ── HDD image location ───────────────────────────────────────────────────────


def _toml(tmp_path: Path, hdd_path: str) -> Path:
    """Write a minimal xemu.toml that names `hdd_path` as the HDD image.

    Args:
        tmp_path: Directory the config is written into.
        hdd_path: Value written to the hdd_path key.

    Returns:
        The path of the written config.
    """
    cfg = tmp_path / "xemu.toml"
    cfg.write_text(f'[sys.files]\nhdd_path = "{hdd_path}"\n')
    return cfg


def test_the_hdd_path_comes_from_the_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The HDD image path is read from the user's xemu.toml."""
    monkeypatch.setattr(xemu, "XEMU_TOML", _toml(tmp_path, "/config/xemu/mine.qcow2"))
    assert xemu._hdd_image_path() == Path("/config/xemu/mine.qcow2")


@pytest.mark.parametrize("hdd_path", ["", "relative.qcow2", "/atroot.qcow2"])
def test_an_unusable_hdd_path_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hdd_path: str
) -> None:
    """An empty, relative, or root-level hdd_path falls back to the default image."""
    monkeypatch.setattr(xemu, "XEMU_TOML", _toml(tmp_path, hdd_path))
    assert xemu._hdd_image_path() == xemu.FALLBACK_HDD_IMAGE


def test_a_missing_config_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing xemu.toml falls back to the default image."""
    monkeypatch.setattr(xemu, "XEMU_TOML", tmp_path / "nope.toml")
    assert xemu._hdd_image_path() == xemu.FALLBACK_HDD_IMAGE


def test_an_unparseable_config_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A xemu.toml that does not parse falls back to the default image."""
    cfg = tmp_path / "xemu.toml"
    cfg.write_text("[sys.files\nhdd_path =")
    monkeypatch.setattr(xemu, "XEMU_TOML", cfg)
    assert xemu._hdd_image_path() == xemu.FALLBACK_HDD_IMAGE


# ── One-time raw conversion ──────────────────────────────────────────────────


def test_a_raw_image_is_left_alone(tmp_path: Path) -> None:
    """An image that is already raw is neither converted nor backed up."""
    image = tmp_path / "hdd.qcow2"
    image.write_bytes(b"not a qcow2 header")
    assert xemu._ensure_raw_image(image) is True
    assert image.read_bytes() == b"not a qcow2 header"
    assert not image.with_name("hdd.qcow2.backup").exists()


def test_a_missing_image_is_not_convertible(tmp_path: Path) -> None:
    """A missing image cannot be made raw."""
    assert xemu._ensure_raw_image(tmp_path / "gone.qcow2") is False


def test_a_qcow2_is_converted_in_place_and_backed_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A qcow2 image is converted to raw in place, with the original kept as a backup."""
    image = tmp_path / "hdd.qcow2"
    image.write_bytes(xemu.QCOW2_MAGIC + b"rest of the qcow2")

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        Path(cmd[-1]).write_bytes(b"raw content")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(xemu.subprocess, "run", fake_run)
    assert xemu._ensure_raw_image(image) is True
    assert image.read_bytes() == b"raw content"
    assert image.with_name("hdd.qcow2.backup").read_bytes().startswith(xemu.QCOW2_MAGIC)


def test_a_failed_conversion_leaves_the_qcow2_playable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed conversion leaves the original qcow2 untouched.

    Losing save sync is survivable; losing the disk is not.
    """
    image = tmp_path / "hdd.qcow2"
    original = xemu.QCOW2_MAGIC + b"rest of the qcow2"
    image.write_bytes(original)

    def fake_run(cmd: list[str], **kwargs: object) -> NoReturn:
        raise xemu.subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(xemu.subprocess, "run", fake_run)
    assert xemu._ensure_raw_image(image) is False
    assert image.read_bytes() == original


# ── Display settings pin ─────────────────────────────────────────────────────


# A config shaped like the one xemu writes: comments, several tables, and a
# [display.quality] subtable right after the [display] body the pin edits.
FULL_TOML = """\
[general]
show_welcome = false

[display]
renderer = 'VULKAN'
# how the guest frame is fitted to the window
ui_scale = 2

[display.quality]
surface_scale = 1

[sys.files]
hdd_path = '/config/xemu/xbox_hdd.qcow2'
"""


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the pin at a throwaway config and default it to OpenGL.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The path of the config the pin will edit; the file does not exist yet.
    """
    cfg = tmp_path / "xemu.toml"
    monkeypatch.setattr(xemu, "XEMU_TOML", cfg)
    monkeypatch.setattr(xemu, "XEMU_RENDERER", "OPENGL")
    return cfg


def _renderer_of(cfg: Path) -> str:
    """Read the display renderer out of a config file.

    Args:
        cfg: The xemu.toml to parse.

    Returns:
        The value of [display] renderer.
    """
    return tomllib.loads(cfg.read_text())["display"]["renderer"]


def _fullscreen_of(cfg: Path) -> bool:
    """Read the fullscreen-on-startup flag out of a config file.

    Args:
        cfg: The xemu.toml to parse.

    Returns:
        The value of [display.window] fullscreen_on_startup.
    """
    return tomllib.loads(cfg.read_text())["display"]["window"]["fullscreen_on_startup"]


def test_a_vulkan_config_is_pinned_back_to_opengl(pinned: Path) -> None:
    """A config set to Vulkan is pinned back to the configured OpenGL renderer."""
    pinned.write_text(FULL_TOML)
    xemu._pin_display_settings()
    assert _renderer_of(pinned) == "OPENGL"


def test_a_windowed_config_is_pinned_to_fullscreen(pinned: Path) -> None:
    """A config that starts windowed is pinned to fullscreen on startup."""
    pinned.write_text(FULL_TOML.replace(
        "[display.quality]", "[display.window]\nfullscreen_on_startup = false\n\n[display.quality]"))
    xemu._pin_display_settings()
    assert _fullscreen_of(pinned) is True


def test_pinning_leaves_the_rest_of_the_config_alone(pinned: Path) -> None:
    """Pinning rewrites only the renderer and fullscreen keys.

    The file belongs to xemu; the pin owns exactly two keys in it.
    """
    pinned.write_text(FULL_TOML)
    xemu._pin_display_settings()
    after = pinned.read_text()
    assert after == (
        FULL_TOML.replace("renderer = 'VULKAN'", "renderer = 'OPENGL'").rstrip("\n")
        + "\n\n[display.window]\nfullscreen_on_startup = true\n"
    )
    assert "# how the guest frame is fitted to the window" in after
    assert tomllib.loads(after)["display"]["quality"]["surface_scale"] == 1


def test_a_display_section_without_a_renderer_gains_one(pinned: Path) -> None:
    """A [display] section with no renderer key gains one, leaving other tables alone."""
    pinned.write_text("[display]\nui_scale = 2\n\n[sys]\nmem = 64\n")
    xemu._pin_display_settings()
    assert _renderer_of(pinned) == "OPENGL"
    assert tomllib.loads(pinned.read_text())["sys"]["mem"] == 64


def test_a_config_with_no_display_section_gains_one(pinned: Path) -> None:
    """A config with no [display] section gains one carrying both pinned keys."""
    pinned.write_text("[general]\nshow_welcome = false\n")
    xemu._pin_display_settings()
    assert _renderer_of(pinned) == "OPENGL"
    assert _fullscreen_of(pinned) is True


def test_the_display_and_display_window_sections_stay_distinct(pinned: Path) -> None:
    """The pin writes [display.window] as its own table rather than a key inside [display]."""
    pinned.write_text("[general]\nshow_welcome = false\n")
    xemu._pin_display_settings()
    display = tomllib.loads(pinned.read_text())["display"]
    assert display["renderer"] == "OPENGL"
    assert display["window"] == {"fullscreen_on_startup": True}


def test_the_renderer_is_configurable_for_hardware_where_vulkan_works(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned renderer follows XEMU_RENDERER, so Vulkan can be chosen where it works."""
    monkeypatch.setattr(xemu, "XEMU_RENDERER", "VULKAN")
    pinned.write_text("[display]\nrenderer = 'OPENGL'\n")
    xemu._pin_display_settings()
    assert _renderer_of(pinned) == "VULKAN"


@pytest.mark.parametrize("setting", ["KEEP", ""])
def test_the_renderer_pin_can_be_turned_off(
    pinned: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    """Turning the renderer pin off leaves the renderer alone, not fullscreen."""
    monkeypatch.setattr(xemu, "XEMU_RENDERER", setting)
    pinned.write_text(FULL_TOML)
    xemu._pin_display_settings()
    assert _renderer_of(pinned) == "VULKAN"
    assert _fullscreen_of(pinned) is True


def test_a_missing_config_is_not_created(pinned: Path) -> None:
    """A missing config is left for xemu to create.

    xemu writes the file itself, and its own default is already OpenGL.
    """
    xemu._pin_display_settings()
    assert not pinned.exists()


def test_an_unwritable_config_does_not_stop_the_launch(pinned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config that cannot be written is left as it was and the pin does not raise."""
    pinned.write_text(FULL_TOML)

    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("read-only file system")

    monkeypatch.setattr(xemu.tempfile, "mkstemp", fail)
    xemu._pin_display_settings()
    assert _renderer_of(pinned) == "VULKAN"


def test_a_pin_that_would_not_parse_is_never_written(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned edit that does not parse leaves the user's config alone.

    xemu.toml holds every setting the user has ever changed, so a bad edit
    written over it costs them all of them.
    """
    pinned.write_text(FULL_TOML)
    monkeypatch.setattr(xemu, "_pin_toml_key",
                        lambda text, section, key, value: "[display\nrenderer = ")

    xemu._pin_display_settings()
    assert pinned.read_text() == FULL_TOML


def test_the_config_is_swapped_in_rather_than_truncated_in_place(pinned: Path) -> None:
    """The pin replaces the config by rename, leaving no temp file behind.

    A truncating write that fails halfway leaves the config unparseable;
    the replacement is written beside it and renamed over it instead.
    """
    pinned.write_text(FULL_TOML)
    xemu._pin_display_settings()

    assert _renderer_of(pinned) == "OPENGL"
    assert [p.name for p in pinned.parent.iterdir()] == [pinned.name]


def test_the_config_keeps_its_permissions_across_a_pin(pinned: Path) -> None:
    """The replaced config carries the mode the original had.

    A temp file created by mkstemp is 0600, which would lock xemu itself out
    of a config it shares.
    """
    pinned.write_text(FULL_TOML)
    os.chmod(pinned, 0o640)
    xemu._pin_display_settings()

    assert pinned.stat().st_mode & 0o777 == 0o640


def _spawned_env(emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Launch with the process spawn stubbed out and hand back the env xemu would have been given.

    Args:
        emulator: The Xemu instance to launch.
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: Directory the throwaway disc image is written into.

    Returns:
        The environment mapping passed to the stubbed spawn.
    """
    monkeypatch.setattr(xemu, "_reap_strays", lambda: None)
    monkeypatch.setattr(xemu.Xemu, "stop", lambda self: None)
    captured: list[dict] = []
    monkeypatch.setattr(xemu.Xemu, "_spawn",
                        lambda self, cmd, env: captured.append(env))
    emulator.launch(_xiso(tmp_path / "g.iso"), None)
    assert captured, "launch did not spawn xemu"
    return captured[0]


def test_software_gl_is_off_by_default(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LIBGL_ALWAYS_SOFTWARE is absent from the launch env unless asked for.

    CPU rendering is a workaround for broken drivers, not a default.
    """
    monkeypatch.setattr(xemu, "XEMU_SOFTWARE_GL", False)
    assert "LIBGL_ALWAYS_SOFTWARE" not in _spawned_env(emulator, monkeypatch, tmp_path)


def test_software_gl_reaches_xemu_and_nothing_else(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The software GL switch lands in xemu's own launch env and not the broker's.

    Set on xemu's own launch env, so the rest of the container keeps the GPU.
    """
    monkeypatch.setattr(xemu, "XEMU_SOFTWARE_GL", True)
    env = _spawned_env(emulator, monkeypatch, tmp_path)
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"
    assert "LIBGL_ALWAYS_SOFTWARE" not in os.environ


@pytest.mark.parametrize("setting,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("", False),
])
def test_the_software_gl_switch_reads_the_usual_spellings(setting: str, expected: bool) -> None:
    """The truthy parser accepts the common spellings of on and off."""
    assert xemu._truthy(setting) is expected


def test_launch_pins_the_display_settings_before_spawning(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch pins the display settings before the process is spawned.

    The pin is worthless if a launch can get past it.
    """
    cfg = xemu.XEMU_TOML
    cfg.write_text(cfg.read_text() + "\n[display]\nrenderer = 'VULKAN'\n")
    monkeypatch.setattr(xemu, "XEMU_RENDERER", "OPENGL")
    monkeypatch.setattr(xemu, "_reap_strays", lambda: None)
    monkeypatch.setattr(xemu.Xemu, "stop", lambda self: None)

    spawned: list[list[str]] = []
    monkeypatch.setattr(xemu.Xemu, "_spawn",
                        lambda self, cmd, env: spawned.append(cmd))

    emulator.launch(_xiso(tmp_path / "g.iso"), None)
    assert spawned, "launch did not spawn xemu"
    assert _renderer_of(cfg) == "OPENGL"
    assert _fullscreen_of(cfg) is True


# ── Stray process reaping ────────────────────────────────────────────────────


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record what the reaper would signal instead of signalling it.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The list deliveries are appended to, as (pid, signal) pairs.
    """
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(xemu.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(xemu.time, "sleep", lambda seconds: None)
    return sent


def test_the_process_scan_sees_this_process() -> None:
    """The /proc scan reports real pids."""
    assert os.getpid() in xemu._proc_pids()


def test_a_process_that_only_names_the_binary_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    """A process that merely mentions the xemu path is not signalled.

    A substring match over whole command lines also hits a shell, a grep or a
    log tail carrying the path, and killed them.
    """
    monkeypatch.setattr(xemu, "_proc_pids", lambda: [4321])
    monkeypatch.setattr(xemu, "_cmdline",
                        lambda pid: ["/bin/sh", "-c", f"tail -f {xemu.XEMU_BIN}.log"])

    xemu._reap_strays()
    assert signals == []


def test_the_reaper_does_not_signal_itself(
    monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    """The broker's own pid is never a target, whatever its command line reads."""
    monkeypatch.setattr(xemu, "_proc_pids", lambda: [os.getpid()])
    monkeypatch.setattr(xemu, "_cmdline", lambda pid: [xemu.XEMU_BIN])

    xemu._reap_strays()
    assert signals == []


def test_a_stray_is_asked_to_exit_before_it_is_killed(
    monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    """A stray that goes on SIGTERM is never killed.

    QEMU flushes the HDD image on a clean shutdown only, and the save hooks
    read that image seconds later.
    """
    monkeypatch.setattr(xemu, "_proc_pids", lambda: [777])
    monkeypatch.setattr(
        xemu, "_cmdline",
        lambda pid: [] if signals else [xemu.XEMU_BIN, "-dvd_path", "/romm/g.iso"])

    xemu._reap_strays()
    assert signals == [(777, signal.SIGTERM)]


def test_a_stray_that_ignores_sigterm_is_killed(
    monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    """A stray still running after the grace window is killed."""
    monkeypatch.setattr(xemu, "XEMU_STRAY_TERM_WAIT", 0.0)
    monkeypatch.setattr(xemu, "_proc_pids", lambda: [777])
    monkeypatch.setattr(xemu, "_cmdline", lambda pid: [xemu.XEMU_BIN])

    xemu._reap_strays()
    assert signals == [(777, signal.SIGTERM), (777, signal.SIGKILL)]


# ── FATX save sync ───────────────────────────────────────────────────────────


@pytest.fixture
def emulator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> xemu.Xemu:
    """Build an Xemu whose HDD image is a real, freshly formatted FATX disk.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: Directory holding the image and the config that points at it.

    Returns:
        An Xemu with its staging directory already created.
    """
    image = tmp_path / "xbox_hdd.qcow2"
    # pyfatx defaults to an 8GB image; each of this file's 24 FATX tests
    # gets a fresh one, which is enough to exhaust a CI runner's disk.
    Fatx.create(str(image), size=64 * 1024 * 1024)
    monkeypatch.setattr(xemu, "XEMU_TOML", _toml(tmp_path, str(image)))
    em = xemu.Xemu()
    em.staging_dir.mkdir(parents=True, exist_ok=True)
    return em


def _fatx(image: Path) -> Fatx:
    """Open the E: partition of a FATX image.

    Args:
        image: The raw HDD image.

    Returns:
        A Fatx handle on the E: drive.
    """
    return Fatx(str(image), drive="e")


def _seed(image: Path, path: str, data: bytes) -> None:
    """Write a file into the image, creating any missing parent directories.

    Args:
        image: The raw HDD image.
        path: Absolute path of the file inside the E: partition.
        data: The file contents.
    """
    fs = _fatx(image)
    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        d = "/" + "/".join(parts[:i])
        if not xemu._fatx_isdir(fs, d):
            fs.mkdir(d)
    fs.write("/" + "/".join(parts), data)
    del fs


def _stage(em: xemu.Xemu, rel: str, data: bytes) -> None:
    """Write a file under the emulator's staging directory.

    Args:
        em: The Xemu whose staging directory receives the file.
        rel: Path of the file relative to the staging directory.
        data: The file contents.
    """
    p = em.staging_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_extract_pulls_only_the_launched_title(emulator: xemu.Xemu) -> None:
    """Extraction copies the launched title's saves and leaves other titles on the disk."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"mine")
    _seed(emulator.hdd_image, "/UDATA/DEADBEEF/saved.dat", b"someone else")
    emulator._title_id = "4D530064"

    assert emulator._extract_saves() == 1
    assert (emulator.staging_dir / "UDATA/4D530064/saved.dat").read_bytes() == b"mine"
    assert not (emulator.staging_dir / "UDATA/DEADBEEF").exists()


def test_extract_matches_a_save_directory_whatever_its_case(emulator: xemu.Xemu) -> None:
    """Extraction finds the title's save directory even when the id differs in case.

    Regression: the title id used to be formatted lowercase and looked up
    literally, so nothing resolved and every session's saves were lost with
    no error.
    """
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"mine")
    emulator._title_id = "4d530064"

    assert emulator._extract_saves() == 1
    assert (emulator.staging_dir / "UDATA/4D530064/saved.dat").read_bytes() == b"mine"


def test_extract_covers_both_udata_and_tdata(emulator: xemu.Xemu) -> None:
    """Extraction takes the title's files from both UDATA and TDATA."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/a.dat", b"u")
    _seed(emulator.hdd_image, "/TDATA/4D530064/b.dat", b"t")
    emulator._title_id = "4D530064"

    assert emulator._extract_saves() == 2


def test_extract_without_a_title_id_extracts_nothing(
    emulator: xemu.Xemu, caplog: pytest.LogCaptureFixture,
) -> None:
    """Extraction with no title id known copies nothing, not every title's saves.

    Regression: an unparseable disc used to fall back to scoping every
    installed title's UDATA and TDATA, leaking other titles' saves into this
    session's archive.
    """
    _seed(emulator.hdd_image, "/UDATA/4D530064/a.dat", b"one")
    _seed(emulator.hdd_image, "/UDATA/DEADBEEF/b.dat", b"two")
    emulator._title_id = None

    with caplog.at_level(logging.WARNING):
        assert emulator._extract_saves() == 0
    assert not any(emulator.staging_dir.rglob("*"))
    assert any("no disc title id" in r.message for r in caplog.records)


def test_extract_is_empty_when_the_title_never_saved(emulator: xemu.Xemu) -> None:
    """Extraction copies nothing when the launched title has no saves on the disk."""
    _seed(emulator.hdd_image, "/UDATA/DEADBEEF/a.dat", b"two")
    emulator._title_id = "4D530064"

    assert emulator._extract_saves() == 0


def test_extract_into_skips_a_save_path_that_escapes_the_dest_dir(
    emulator: xemu.Xemu, tmp_path: Path,
) -> None:
    """`_extract_into` skips a save path that escapes the destination directory.

    libfatx does not reserve ".." as a directory name, so a save partition can carry a literal ".."
    entry that fs.walk() reports as-is; reassembled into a path and resolved on the staging side, that
    walks straight back out of dest unless the extractor catches it first. Exercised directly against
    `_extract_into` (rather than through `_extract_saves`) so it covers this defense regardless of how
    the caller picked its roots.
    """
    _seed(emulator.hdd_image, "/UDATA/../../evil.dat", b"stolen")
    dest = tmp_path / "dest"
    dest.mkdir()
    fs = _fatx(emulator.hdd_image)

    assert emulator._extract_into(fs, ["/UDATA"], dest) == 0
    assert not (tmp_path / "evil.dat").exists()


def test_inject_writes_staged_files_into_the_image(emulator: xemu.Xemu) -> None:
    """Injection writes each staged file into the image at the same path."""
    _stage(emulator, "UDATA/4D530064/saved.dat", b"restored")

    assert emulator._inject_saves() == 1
    fs = _fatx(emulator.hdd_image)
    assert bytes(fs.read("/UDATA/4D530064/saved.dat")) == b"restored"


def test_inject_lands_in_the_existing_directory_whatever_its_case(emulator: xemu.Xemu) -> None:
    """Injection reuses a directory already on the disk even when the staged case differs.

    An archive captured elsewhere can carry a different case than the disk
    holds; creating a twin directory beside the real one would hide the save
    from the game.
    """
    _seed(emulator.hdd_image, "/UDATA/4D530064/old.dat", b"old")
    _stage(emulator, "udata/4d530064/saved.dat", b"restored")

    assert emulator._inject_saves() == 1
    fs = _fatx(emulator.hdd_image)
    assert bytes(fs.read("/UDATA/4D530064/saved.dat")) == b"restored"
    assert [a.filename for a in fs.listdir("/")] == ["UDATA"]


def test_inject_truncates_a_file_that_shrank(emulator: xemu.Xemu) -> None:
    """Injecting a smaller save over a larger one leaves no stale tail behind.

    pyfatx write() never shortens an existing file, so a smaller save has
    to be truncated or it lands with the old tail still attached.
    """
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"a much longer save")
    _stage(emulator, "UDATA/4D530064/saved.dat", b"short")

    assert emulator._inject_saves() == 1
    fs = _fatx(emulator.hdd_image)
    assert bytes(fs.read("/UDATA/4D530064/saved.dat")) == b"short"


def test_inject_skips_dotfiles(emulator: xemu.Xemu) -> None:
    """Injection ignores dotfiles in the staging directory."""
    _stage(emulator, "UDATA/4D530064/.DS_Store", b"junk")
    assert emulator._inject_saves() == 0


def test_inject_is_a_no_op_with_nothing_staged(emulator: xemu.Xemu) -> None:
    """Injection with an empty staging directory writes nothing."""
    assert emulator._inject_saves() == 0


def test_a_save_round_trips_through_the_image(emulator: xemu.Xemu) -> None:
    """A save extracted at exit is restored intact by the next injection.

    The whole point: what exit extracts is what the next activate injects.
    """
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"progress")
    emulator._title_id = "4D530064"
    assert emulator._extract_saves() == 1

    fs = _fatx(emulator.hdd_image)
    fs.write("/UDATA/4D530064/saved.dat", b"WIPED!!!")
    del fs

    assert emulator._inject_saves() == 1
    fs = _fatx(emulator.hdd_image)
    assert bytes(fs.read("/UDATA/4D530064/saved.dat")) == b"progress"


class _FatxProxy:
    """A Fatx handle that records what the save hooks do and can fail one call.

    The real handle lives in a list the test owns rather than on the proxy:
    a failing call leaves the proxy reachable from the logged exception's
    traceback for as long as pytest keeps the log record, and closing the
    image has to stay under the test's control.

    Attributes:
        calls: Filesystem call names, in the order they were made.
        released: Appended to when the hook drops the proxy.
    """

    def __init__(self, handle: list[Fatx], calls: list[str], released: list[bool],
                 fail: Optional[str] = None,
                 error: type[BaseException] = AssertionError) -> None:
        """Wrap an open handle.

        Args:
            handle: One element list holding the real handle.
            calls: List every call name is appended to.
            released: List a True is appended to when the proxy is dropped.
            fail: Name of the call that raises instead of running.
            error: Exception type that call raises.
        """
        self._handle = handle
        self.calls = calls
        self.released = released
        self._fail = fail
        self._error = error

    def __getattr__(self, name: str) -> Any:
        """Wrap one of the handle's methods so the call is recorded.

        The underlying method is looked up when the call is made, not when the
        wrapper is built, so a wrapper caught in a traceback keeps no reference
        to the real handle.

        Args:
            name: The attribute being looked up.

        Returns:
            A wrapper that records the call and applies the configured failure.
        """
        def recorded(*args: object, **kwargs: object) -> Any:
            self.calls.append(name)
            if name == self._fail:
                raise self._error(f"pyfatx {name} failed")
            return getattr(self._handle[0], name)(*args, **kwargs)

        return recorded

    def __del__(self) -> None:
        """Note the release: the hook dropping its handle is what commits FATX writes."""
        self.released.append(True)


def _proxy_fatx(monkeypatch: pytest.MonkeyPatch,
                **kwargs: Any) -> tuple[list[str], list[bool], list[Fatx]]:
    """Make the save hooks open the image through a recording proxy.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        **kwargs: `fail` and `error`, passed to the proxy.

    Returns:
        The call log, the release log, and the one element list holding the
        real handle: emptying it and collecting closes the image, which is
        what makes a write visible to a second handle.
    """
    calls: list[str] = []
    released: list[bool] = []
    handle: list[Fatx] = []

    def opener(image: Path) -> _FatxProxy:
        handle.append(_fatx(image))
        return _FatxProxy(handle, calls, released, **kwargs)

    monkeypatch.setattr(xemu, "_open_fatx_e", opener)
    return calls, released, handle


def test_a_shrinking_save_is_truncated_before_it_is_written(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale tail is cut before the new save lands, not after.

    Truncating afterwards leaves every failure in between showing the previous
    save's tail welded onto the new one, which the game reads as one save.
    """
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"a much longer save")
    _stage(emulator, "UDATA/4D530064/saved.dat", b"short")
    calls, _released, _handle = _proxy_fatx(monkeypatch)

    assert emulator._inject_saves() == 1
    assert calls.index("truncate") < calls.index("write")


def test_a_failed_write_leaves_no_half_written_save_behind(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails takes the file with it instead of leaving a hybrid.

    The old save's tail under a new save's head still reads as one save to the
    game, so half a write is worse than none.
    """
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"a much longer save")
    _stage(emulator, "UDATA/4D530064/saved.dat", b"short")
    calls, _released, handle = _proxy_fatx(monkeypatch, fail="write")

    assert emulator._inject_saves() == 0
    assert calls.index("write") < calls.index("unlink")
    # Closing the image is what makes the removal visible to a second handle.
    handle.clear()
    gc.collect()
    fs = _fatx(emulator.hdd_image)
    with pytest.raises(AssertionError):
        fs.get_attr("/UDATA/4D530064/saved.dat")


def test_an_unexpected_error_still_releases_the_image(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error the injection does not expect still closes the FATX handle.

    pyfatx has no flush; dropping the handle is what commits the writes, so an
    exception on the way out must not skip it.
    """
    _stage(emulator, "UDATA/4D530064/saved.dat", b"restored")
    _calls, released, _handle = _proxy_fatx(monkeypatch)

    def boom(self: Path) -> NoReturn:
        raise ValueError("staging file vanished")

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(ValueError):
        emulator._inject_saves()
    assert released == [True]


def test_a_failed_extraction_keeps_what_the_session_restored(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An extraction that cannot open the image leaves the staging dir alone.

    The dump ships whatever staging holds, so emptying it on a failed read
    uploads an empty archive over the title's real saves.
    """
    _stage(emulator, "UDATA/4D530064/restored.dat", b"from the archive")
    monkeypatch.setattr(xemu, "_open_fatx_e", lambda image: None)

    assert emulator._extract_saves() is None
    assert (emulator.staging_dir / "UDATA/4D530064/restored.dat").read_bytes() == b"from the archive"


def test_an_image_with_no_save_directories_is_a_failed_read(emulator: xemu.Xemu) -> None:
    """A disk carrying neither UDATA nor TDATA is a failed read, not an empty save set."""
    _stage(emulator, "UDATA/4D530064/restored.dat", b"from the archive")

    assert emulator._extract_saves() is None
    assert (emulator.staging_dir / "UDATA/4D530064/restored.dat").exists()


def test_a_listing_failure_fails_the_whole_extraction(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that cannot be listed fails the extraction rather than staging part of it."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"progress")
    _stage(emulator, "UDATA/4D530064/restored.dat", b"from the archive")
    emulator._title_id = "4D530064"
    _calls, _released, _handle = _proxy_fatx(monkeypatch, fail="walk")

    assert emulator._extract_saves() is None
    assert (emulator.staging_dir / "UDATA/4D530064/restored.dat").exists()
    assert [p.name for p in emulator.staging_dir.parent.iterdir()
            if p.name.startswith(".")] == []


def test_a_successful_extraction_replaces_the_staged_files(emulator: xemu.Xemu) -> None:
    """What the extraction stages is exactly what came off the image."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"progress")
    _stage(emulator, "UDATA/4D530064/stale.dat", b"last session")
    emulator._title_id = "4D530064"

    assert emulator._extract_saves() == 1
    assert not (emulator.staging_dir / "UDATA/4D530064/stale.dat").exists()
    assert (emulator.staging_dir / "UDATA/4D530064/saved.dat").read_bytes() == b"progress"


# ── Session contract ─────────────────────────────────────────────────────────


def test_xemu_reports_no_save_state_support() -> None:
    """Xemu advertises no save state support.

    A raw image cannot hold QEMU internal snapshots, so the parent has to
    read the absence off the status response rather than assume it.
    """
    assert xemu.Xemu.supports_states is False


def test_save_and_exit_reports_saves_rather_than_a_state(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit reports the title id and the count of extracted saves instead of a state."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"progress")
    emulator._title_id = "4D530064"
    monkeypatch.setattr(xemu, "_reap_strays", lambda: None)
    monkeypatch.setattr(emulator, "stop", lambda: None)

    result = emulator.save_and_exit(1)
    assert result["state_saved"] is None
    assert result["state_slot"] is None
    assert result["title_id"] == "4D530064"
    assert result["saves_extracted"] == 1


def test_exit_keeps_the_staged_saves_when_the_image_cannot_be_read(
    emulator: xemu.Xemu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit reports no saves rather than emptying the staging dir on a failed read.

    Reporting zero is survivable; replacing the user's saves with an empty
    archive is not.
    """
    _stage(emulator, "UDATA/4D530064/restored.dat", b"from the archive")
    emulator._title_id = "4D530064"
    monkeypatch.setattr(xemu, "_reap_strays", lambda: None)
    monkeypatch.setattr(emulator, "stop", lambda: None)
    monkeypatch.setattr(xemu, "_open_fatx_e", lambda image: None)

    result = emulator.save_and_exit(None)
    assert result["saves_extracted"] == 0
    assert (emulator.staging_dir / "UDATA/4D530064/restored.dat").exists()
