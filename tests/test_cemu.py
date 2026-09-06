"""Cemu ROM resolution, settings.xml patching, pad profile seeding, and the exit-time save refresh.

Covers picking a bootable title out of a folder, the settings keys the broker
pins, the GamePad profile it seeds, and which saves exit re-stamps.
"""

import logging
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import cemu


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Cemu ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(cemu, "ROM_ROOT", root)
    return root


@pytest.fixture
def config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Cemu settings and pad profile paths under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The Cemu config directory; it is not created.
    """
    d = tmp_path / "config" / "Cemu"
    monkeypatch.setattr(cemu, "SETTINGS_PATH", d / "settings.xml")
    monkeypatch.setattr(cemu, "PROFILE_PATH", d / "controllerProfiles" / "controller0.xml")
    return d


@pytest.fixture
def save_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Cemu MLC tree and its save directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The usr/save directory inside the MLC tree.
    """
    mlc = tmp_path / "mlc01"
    save = mlc / "usr" / "save"
    save.mkdir(parents=True)
    monkeypatch.setattr(cemu, "MLC_DIR", mlc)
    monkeypatch.setattr(cemu, "SAVE_DIR", save)
    return save


def _touch(path: Path, mtime: Optional[float] = None) -> Path:
    """Write a placeholder file, creating parents, optionally with a fixed mtime.

    Args:
        path: The file to create.
        mtime: Modification time to stamp on it, if any.

    Returns:
        The path that was written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_rom_pick_prefers_the_archive_over_the_raw_image(rom_root: Path) -> None:
    """A .wua beside a .wud of the same game is the one picked."""
    game = rom_root / "Game"
    _touch(game / "Game.wud")
    best = _touch(game / "Game.wua")

    assert cemu.Cemu().resolve_rom_file(game) == best


def test_rom_pick_finds_the_rpx_inside_an_extracted_dump(rom_root: Path) -> None:
    """An extracted dump resolves to the .rpx under its code directory."""
    game = rom_root / "Game"
    rpx = _touch(game / "code" / "Game.rpx")
    _touch(game / "content" / "data.bin")
    _touch(game / "meta" / "meta.xml")

    assert cemu.Cemu().resolve_rom_file(game) == rpx


def test_rom_pick_reaches_a_dump_wrapped_in_a_library_folder(rom_root: Path) -> None:
    """A dump nested one folder deeper still resolves to its .rpx."""
    game = rom_root / "Game"
    rpx = _touch(game / "Game [TITLEID]" / "code" / "Game.rpx")

    assert cemu.Cemu().resolve_rom_file(game) == rpx


def test_rom_pick_skips_the_update_beside_the_base_game(rom_root: Path) -> None:
    """An update dump beside the base game image is not the thing booted."""
    game = rom_root / "Game"
    _touch(game / "Game (Update)" / "code" / "Game.rpx")
    base = _touch(game / "Game.wux")

    assert cemu.Cemu().resolve_rom_file(game) == base


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """A title symlinked from outside the ROM root is never picked."""
    outside = _touch(tmp_path / "outside" / "Game.wua")
    game = rom_root / "Game"
    game.mkdir()
    (game / "Game.wua").symlink_to(outside)

    assert cemu.Cemu().resolve_rom_file(game) is None


def test_resolve_takes_a_file_as_given(rom_root: Path) -> None:
    """A path that is already a file resolves to itself."""
    rom = _touch(rom_root / "Game.wux")
    assert cemu.Cemu().resolve_rom_file(rom) == rom


def test_settings_are_seeded_when_the_file_is_missing(config_dir: Path) -> None:
    """A missing settings.xml is created with updates and Discord presence off."""
    cemu._patch_settings()

    root = ET.parse(cemu.SETTINGS_PATH).getroot()
    assert root.tag == "content"
    assert root.find("check_update").text == "false"
    assert root.find("use_discord_presence").text == "false"


def test_settings_patch_keeps_what_the_user_tuned(config_dir: Path) -> None:
    """Patching pins the broker's keys and keeps every other key the user set."""
    config_dir.mkdir(parents=True)
    cemu.SETTINGS_PATH.write_text(
        "<content><check_update>true</check_update>"
        "<vsync>1</vsync><graphic_api>1</graphic_api></content>"
    )

    cemu._patch_settings()

    root = ET.parse(cemu.SETTINGS_PATH).getroot()
    assert root.find("check_update").text == "false"
    assert root.find("vsync").text == "1"
    assert root.find("graphic_api").text == "1"


def test_settings_pins_the_audio_device_to_default(config_dir: Path) -> None:
    """TVDevice and PadDevice are forced to the cubeb default sentinel."""
    cemu._patch_settings()

    root = ET.parse(cemu.SETTINGS_PATH).getroot()
    audio = root.find("Audio")
    assert audio.find("TVDevice").text == "default"
    assert audio.find("PadDevice").text == "default"


def test_settings_patch_overwrites_a_blank_audio_device(config_dir: Path) -> None:
    """An existing blank TVDevice/PadDevice, Cemu's own default, is patched too."""
    config_dir.mkdir(parents=True)
    cemu.SETTINGS_PATH.write_text(
        "<content><Audio><TVDevice /><PadDevice /></Audio></content>"
    )

    cemu._patch_settings()

    root = ET.parse(cemu.SETTINGS_PATH).getroot()
    audio = root.find("Audio")
    assert audio.find("TVDevice").text == "default"
    assert audio.find("PadDevice").text == "default"


def test_a_broken_settings_file_is_reseeded_not_fatal(config_dir: Path) -> None:
    """A settings.xml that does not parse is reseeded instead of raising."""
    config_dir.mkdir(parents=True)
    cemu.SETTINGS_PATH.write_text("<content><unclosed>")

    cemu._patch_settings()

    root = ET.parse(cemu.SETTINGS_PATH).getroot()
    assert root.find("check_update").text == "false"


def test_an_unwritable_settings_file_aborts_the_launch(config_dir: Path) -> None:
    """A settings.xml the broker cannot write must stop the launch, not pass it.

    Launching anyway parks Cemu on its Getting Started modal while the
    activate reports a healthy session.
    """
    # A plain file where the config directory belongs fails every write.
    config_dir.parent.mkdir(parents=True)
    config_dir.write_text("not a directory")

    with pytest.raises(RuntimeError, match="broker settings"):
        cemu._patch_settings()


def test_crc16_matches_the_arc_check_value() -> None:
    """The CRC-16 implementation produces the standard ARC check value."""
    assert cemu._crc16(b"123456789") == 0xBB3D


def test_sdl_guid_is_the_interposers_name_based_fallback() -> None:
    """With crc=0, the GUID is a zero bus and crc followed by the pad name."""
    assert cemu._sdl_guid(0) == "000000004d6963726f736f6674205800"


def test_sdl_guid_matches_the_interposers_measured_guid_with_crc() -> None:
    """With the real name crc, the GUID matches the one measured against a live interposer."""
    assert cemu._sdl_guid(cemu._crc16(cemu._PAD_NAME.encode())) == "000081b84d6963726f736f6674205800"


def test_pad_profile_carries_both_guid_variants(config_dir: Path) -> None:
    """The seeded GamePad profile lists two distinct controller GUIDs with full mappings."""
    cemu._seed_pad_profile()

    root = ET.parse(cemu.PROFILE_PATH).getroot()
    assert root.find("type").text == "Wii U GamePad"
    uuids = [c.find("uuid").text for c in root.findall("controller")]
    assert len(uuids) == 2 and len(set(uuids)) == 2
    for controller in root.findall("controller"):
        entries = controller.find("mappings").findall("entry")
        assert len(entries) == len(cemu._VPAD_SDL_MAPPINGS)


def test_pad_profile_is_not_overwritten_once_present(config_dir: Path) -> None:
    """An existing pad profile is left exactly as the player tuned it."""
    cemu.PROFILE_PATH.parent.mkdir(parents=True)
    cemu.PROFILE_PATH.write_text("player tuned")

    cemu._seed_pad_profile()

    assert cemu.PROFILE_PATH.read_text() == "player tuned"


def test_pad_uuids_can_be_pinned_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """CEMU_PAD_UUIDS overrides the computed pad UUIDs with a comma-separated list."""
    monkeypatch.setenv("CEMU_PAD_UUIDS", "0_aaaa, 1_bbbb")
    assert cemu._pad_uuids() == ["0_aaaa", "1_bbbb"]


def test_launch_always_states_the_mlc_the_dump_reads_back(
    save_dir: Path, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mlc path must be passed even without CEMU_MLC_DIR set.

    CEMU_DATA_DIR and XDG_DATA_HOME move MLC_DIR too, and Cemu resolves
    neither of them the way the broker does.
    """
    monkeypatch.delenv("CEMU_MLC_DIR", raising=False)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        cemu.Cemu,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = tmp_path / "game.wua"
    rom.write_bytes(b"")

    cemu.Cemu().launch(rom, resume_slot=None)

    assert spawned[0][spawned[0].index("-m") + 1] == str(cemu.MLC_DIR)


def test_launch_creates_the_mlc_cemu_is_pointed_at(
    save_dir: Path, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cemu refuses an mlc path that is not there, so the broker makes one."""
    mlc = tmp_path / "fresh-mlc"
    monkeypatch.setattr(cemu, "MLC_DIR", mlc)
    monkeypatch.setattr(cemu.Cemu, "_spawn", lambda self, cmd, env, stdin_pipe=False: None)
    rom = tmp_path / "game.wua"
    rom.write_bytes(b"")

    cemu.Cemu().launch(rom, resume_slot=None)

    assert mlc.is_dir()


def test_exit_refreshes_only_the_title_saves_this_session_wrote(save_dir: Path) -> None:
    """Exit re-stamps every file of a title written this session and nothing else."""
    emu = cemu.Cemu()
    emu._session_start = time.time() - 100

    stale = _touch(
        save_dir / "00050000" / "aaaaaaaa" / "user" / "80000001" / "old.dat",
        mtime=emu._session_start - 500,
    )
    partner = _touch(
        save_dir / "00050000" / "bbbbbbbb" / "user" / "80000001" / "old.dat",
        mtime=emu._session_start - 500,
    )
    _touch(save_dir / "00050000" / "bbbbbbbb" / "user" / "80000001" / "new.dat")
    system = _touch(
        save_dir / "system" / "act" / "80000001" / "account.dat",
        mtime=emu._session_start + 10,
    )
    system_mtime = system.stat().st_mtime

    emu.save_and_exit(10)

    # The touched title ships whole; the untouched title and the system
    # tree keep their stamps.
    assert partner.stat().st_mtime >= emu._session_start
    assert stale.stat().st_mtime < emu._session_start
    assert system.stat().st_mtime == system_mtime


def test_exit_without_a_launch_restamps_nothing(save_dir: Path) -> None:
    """A save_and_exit that never saw a launch must not claim every title's saves.

    A zero baseline is newer than every file on disk, which would drag
    unrelated titles into this session's dump.
    """
    other = _touch(
        save_dir / "00050000" / "aaaaaaaa" / "user" / "80000001" / "old.dat",
        mtime=time.time() - 5000,
    )
    before = other.stat().st_mtime

    cemu.Cemu().save_and_exit(10)

    assert other.stat().st_mtime == before


def test_exit_skips_a_save_dir_whose_low_half_is_not_a_title_id(save_dir: Path) -> None:
    """A `usr/save/<high>/<name>` whose low half is not 8 hex is not a title save.

    Both halves feed the restamp walk, so an unvalidated low half hands
    whatever name is on disk straight to it.
    """
    emu = cemu.Cemu()
    emu._session_start = time.time() - 100

    stray = _touch(
        save_dir / "00050000" / "not-a-title-id" / "old.dat",
        mtime=emu._session_start - 500,
    )
    _touch(save_dir / "00050000" / "not-a-title-id" / "new.dat")

    emu.save_and_exit(10)

    assert stray.stat().st_mtime < emu._session_start


def test_exit_survives_a_save_tree_that_cannot_be_listed(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save tree that fails to list is logged, not raised through the exit path.

    The MLC can vanish under the walk, and the caller still has a process to
    stop and a report to hand back.
    """

    def boom(self: Path) -> None:
        raise OSError("mlc went away")

    monkeypatch.setattr(Path, "iterdir", boom)
    emu = cemu.Cemu()
    emu._session_start = time.time() - 100

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.cemu"):
        report = emu.save_and_exit(10)

    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
    assert any("could not list the save tree" in r.getMessage() for r in caplog.records)


def test_exit_survives_a_title_dir_that_cannot_be_walked(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A title dir that fails to walk is logged and stepped over, not raised."""
    _touch(save_dir / "00050000" / "aaaaaaaa" / "user" / "80000001" / "old.dat")

    def boom(self: Path, pattern: str) -> None:
        raise OSError("save vanished")

    monkeypatch.setattr(Path, "rglob", boom)
    emu = cemu.Cemu()
    emu._session_start = time.time() - 100

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.cemu"):
        report = emu.save_and_exit(10)

    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
    assert any("could not walk" in r.getMessage() for r in caplog.records)


def test_exit_reports_no_state(save_dir: Path) -> None:
    """Exit reports that Cemu has no save state to offer."""
    report = cemu.Cemu().save_and_exit(10)
    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
