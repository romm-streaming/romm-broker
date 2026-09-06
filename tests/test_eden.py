"""Eden ROM resolution, qt-config.ini patching, launch flags, and the exit-time save refresh.

Covers picking a bootable title out of a folder, the ini keys the broker pins
before every launch, the session baseline launch records, and which saves and
profile data exit re-stamps for the delta dump.
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import eden

TITLE_ID = "0100000000010000"
"""A title id directory name, the leaf of a save unit path."""
OTHER_TITLE_ID = "0100000000020000"
"""A second title id, for the saves that must stay out of this session's dump."""
USER_ID = "a" * 32
"""A profile UUID, the middle level of an account save path."""
SPACE_ID = "0" * 16
"""The save data space id every user save sits under."""


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Eden ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(eden, "ROM_ROOT", root)
    return root


@pytest.fixture
def ini_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point qt-config.ini under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The qt-config.ini path; neither it nor its directory is created.
    """
    path = tmp_path / "config" / "eden" / "qt-config.ini"
    monkeypatch.setattr(eden, "INI_PATH", path)
    return path


@pytest.fixture
def save_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the virtual NAND's user save tree and profile store under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The user save tree.
    """
    nand = tmp_path / "data" / "nand"
    save = nand / "user" / "save"
    save.mkdir(parents=True)
    monkeypatch.setattr(eden, "SAVE_DIR", save)
    monkeypatch.setattr(eden, "PROFILE_STORE_DIR", nand / "system" / "save" / "8000000000000010")
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


def _save_file(save_dir: Path, title_id: str, name: str, mtime: Optional[float] = None) -> Path:
    """Write a file inside one title's account save directory.

    Args:
        save_dir: The user save tree.
        title_id: The title the save belongs to.
        name: The file name under the save unit.
        mtime: Modification time to stamp on it, if any.

    Returns:
        The path that was written.
    """
    return _touch(save_dir / SPACE_ID / USER_ID / title_id / name, mtime=mtime)


def test_resolve_takes_a_file_as_given(rom_root: Path) -> None:
    """A path that is already a file resolves to itself."""
    rom = _touch(rom_root / "Game.xci")
    assert eden.Eden().resolve_rom_file(rom) == rom


def test_resolve_reports_nothing_for_a_path_that_is_not_there(rom_root: Path) -> None:
    """A path that is neither a file nor a directory resolves to nothing."""
    assert eden.Eden().resolve_rom_file(rom_root / "missing") is None


def test_rom_pick_prefers_the_cartridge_dump_over_the_package(rom_root: Path) -> None:
    """An .xci beside an .nsp of the same game is the one picked."""
    game = rom_root / "Game"
    _touch(game / "Game.nsp")
    best = _touch(game / "Game.xci")

    assert eden.Eden().resolve_rom_file(game) == best


def test_rom_pick_reaches_a_title_wrapped_in_a_library_folder(rom_root: Path) -> None:
    """A title nested one folder deeper still resolves."""
    game = rom_root / "Game"
    rom = _touch(game / "Game [0100000000010000][v0]" / "Game.nsp")

    assert eden.Eden().resolve_rom_file(game) == rom


def test_rom_pick_skips_the_update_beside_the_base_game(rom_root: Path) -> None:
    """An update package beside the base game is not the thing booted."""
    game = rom_root / "Game"
    _touch(game / "Game (Update).nsp")
    base = _touch(game / "Game.nsp")

    assert eden.Eden().resolve_rom_file(game) == base


def test_rom_pick_ignores_a_file_the_loader_cannot_boot(rom_root: Path) -> None:
    """A folder holding nothing bootable resolves to nothing."""
    game = rom_root / "Game"
    _touch(game / "Game.zip")
    _touch(game / "cover.png")

    assert eden.Eden().resolve_rom_file(game) is None


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """A title symlinked from outside the ROM root is never picked."""
    outside = _touch(tmp_path / "outside" / "Game.xci")
    game = rom_root / "Game"
    game.mkdir()
    (game / "Game.xci").symlink_to(outside)

    assert eden.Eden().resolve_rom_file(game) is None


def test_rom_pick_keeps_what_a_readable_pattern_found(
    rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One unreadable search pattern must not report the title as unbootable."""
    game = rom_root / "Game"
    rom = _touch(game / "Game.xci")
    real_glob = Path.glob

    def flaky(self: Path, pattern: str) -> object:
        if pattern == "*/*":
            raise OSError("subdirectory unreadable")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", flaky)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.eden"):
        assert eden.Eden().resolve_rom_file(game) == rom

    assert any("search of" in r.getMessage() for r in caplog.records)


def test_a_missing_ini_is_seeded_with_the_close_confirmation_off(ini_path: Path) -> None:
    """A missing qt-config.ini is created with confirmStop pinned to Ask_Never."""
    eden._patch_ini()

    text = ini_path.read_text()
    assert "[UI]" in text
    assert "confirmStop\\default = false" in text
    assert "confirmStop = 2" in text


def test_the_ini_patch_keeps_what_the_player_tuned(ini_path: Path) -> None:
    """Patching pins the broker's keys and leaves every other setting alone."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text(
        "[UI]\nconfirmStop\\default=true\nconfirmStop=0\ntheme=dark\n"
        "\n[Renderer]\nbackend=1\n"
    )

    eden._patch_ini()

    lines = ini_path.read_text().splitlines()
    assert "confirmStop\\default = false" in lines
    assert "confirmStop = 2" in lines
    assert "theme=dark" in lines
    assert "backend=1" in lines


def test_a_missing_key_is_added_to_the_section_it_belongs_in(ini_path: Path) -> None:
    """A [UI] section without the key gets it, rather than a second section."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text("[UI]\ntheme=dark\n")

    eden._patch_ini()

    text = ini_path.read_text()
    assert text.count("[UI]") == 1
    assert "confirmStop = 2" in text
    assert "theme=dark" in text


def test_a_file_without_the_section_gets_one_appended(ini_path: Path) -> None:
    """An ini with no [UI] section gets the section and both keys appended."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text("[Renderer]\nbackend=1\n")

    eden._patch_ini()

    text = ini_path.read_text()
    assert "backend=1" in text
    assert "[UI]" in text
    assert "confirmStop = 2" in text


def test_an_unpatchable_ini_refuses_the_launch(
    ini_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An ini the broker cannot write must stop the launch, not pass it.

    Launching on Ask_Always leaves a modal answering SIGTERM, which holds the
    shutdown until SIGKILL cuts a running game off mid-save.
    """
    # A plain file where the config directory belongs fails every write.
    ini_path.parent.parent.mkdir(parents=True)
    ini_path.parent.write_text("not a directory")

    with caplog.at_level(logging.ERROR, logger="webstation_broker.emulators.eden"):
        with pytest.raises(OSError):
            eden._patch_ini()

    assert any("refusing to launch" in r.getMessage() for r in caplog.records)


def test_launch_boots_the_rom_fullscreen(
    ini_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch command boots the resolved ROM fullscreen through the ini-patched config."""
    monkeypatch.setenv("EDEN_BIN", "eden-test")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        eden.Eden,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = _touch(tmp_path / "game.xci")

    eden.Eden().launch(rom, resume_slot=None)

    assert spawned == [["eden-test", "-f", "-g", str(rom)]]
    assert ini_path.exists()


def test_launch_records_the_session_baseline(
    ini_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch stamps the baseline the exit dump scopes saves by."""
    monkeypatch.setattr(eden.Eden, "_spawn", lambda self, cmd, env, stdin_pipe=False: None)
    rom = _touch(tmp_path / "game.xci")
    emu = eden.Eden()
    assert emu._session_start == float("inf")

    before = time.time()
    emu.launch(rom, resume_slot=None)

    assert before <= emu._session_start <= time.time()


def test_a_resume_slot_is_logged_and_ignored(
    ini_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Eden has no save states, so a resume slot changes nothing about the launch."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        eden.Eden,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = _touch(tmp_path / "game.xci")

    with caplog.at_level(logging.INFO, logger="webstation_broker.emulators.eden"):
        eden.Eden().launch(rom, resume_slot=3)

    assert spawned[0][-1] == str(rom)
    assert any("resume_slot 3 ignored" in r.getMessage() for r in caplog.records)


def test_an_unpatchable_ini_spawns_nothing(
    ini_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ini patch aborts the launch before anything is spawned."""
    ini_path.parent.parent.mkdir(parents=True)
    ini_path.parent.write_text("not a directory")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        eden.Eden,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = _touch(tmp_path / "game.xci")

    with pytest.raises(OSError):
        eden.Eden().launch(rom, resume_slot=None)

    assert spawned == []


def test_exit_ships_the_whole_save_of_the_title_this_session_wrote(save_dir: Path) -> None:
    """Exit re-stamps every file of a title written this session and nothing else.

    A Switch game rewrites only part of its save, so the sidecar files it left
    alone would be filtered out of the delta dump and the restored save would
    land incomplete.
    """
    emu = eden.Eden()
    emu._session_start = time.time() - 100

    sidecar = _save_file(save_dir, TITLE_ID, "meta.bin", mtime=emu._session_start - 500)
    _save_file(save_dir, TITLE_ID, "game.dat")
    other = _save_file(save_dir, OTHER_TITLE_ID, "game.dat", mtime=emu._session_start - 500)

    emu.save_and_exit(None)

    assert sidecar.stat().st_mtime >= emu._session_start
    assert other.stat().st_mtime < emu._session_start


def test_exit_ships_the_profile_the_save_paths_resolve_through(save_dir: Path) -> None:
    """The profile store travels with a save written this session.

    Switch save paths embed the profile UUID and a game session never
    rewrites the profile store, so the delta dump would leave it behind and
    the restored save would resolve to a profile that is not there.
    """
    emu = eden.Eden()
    emu._session_start = time.time() - 100
    profile = _touch(
        eden.PROFILE_STORE_DIR / "su" / "avators" / "profiles.dat",
        mtime=emu._session_start - 5000,
    )
    _save_file(save_dir, TITLE_ID, "game.dat")

    emu.save_and_exit(None)

    assert profile.stat().st_mtime >= emu._session_start


def test_a_profile_store_kept_as_a_single_file_is_shipped_too(save_dir: Path) -> None:
    """A profile store laid down as one file, not a directory, is re-stamped as well."""
    emu = eden.Eden()
    emu._session_start = time.time() - 100
    profile = _touch(eden.PROFILE_STORE_DIR, mtime=emu._session_start - 5000)
    _save_file(save_dir, TITLE_ID, "game.dat")

    emu.save_and_exit(None)

    assert profile.stat().st_mtime >= emu._session_start


def test_exit_leaves_the_profile_alone_when_no_save_was_written(save_dir: Path) -> None:
    """A session that wrote no save ships nothing, profile store included."""
    emu = eden.Eden()
    emu._session_start = time.time() - 100
    profile = _touch(
        eden.PROFILE_STORE_DIR / "su" / "avators" / "profiles.dat",
        mtime=emu._session_start - 5000,
    )
    before = profile.stat().st_mtime
    _save_file(save_dir, TITLE_ID, "game.dat", mtime=emu._session_start - 500)

    emu.save_and_exit(None)

    assert profile.stat().st_mtime == before


def test_exit_without_a_launch_restamps_nothing(save_dir: Path) -> None:
    """A save_and_exit that never saw a launch must not claim every title's saves.

    A zero baseline is newer than every file on disk, which would drag
    unrelated titles into this session's dump.
    """
    other = _save_file(save_dir, TITLE_ID, "game.dat", mtime=time.time() - 5000)
    before = other.stat().st_mtime

    eden.Eden().save_and_exit(None)

    assert other.stat().st_mtime == before


def test_exit_skips_a_save_dir_whose_leaf_is_not_a_title_id(save_dir: Path) -> None:
    """A save unit path not ending in a title id is not a save, so it is not shipped."""
    emu = eden.Eden()
    emu._session_start = time.time() - 100
    stray = _touch(
        save_dir / SPACE_ID / USER_ID / "not-a-title-id" / "old.bin",
        mtime=emu._session_start - 500,
    )
    _touch(save_dir / SPACE_ID / USER_ID / "not-a-title-id" / "new.bin")

    emu.save_and_exit(None)

    assert stray.stat().st_mtime < emu._session_start


def test_exit_survives_a_save_tree_that_cannot_be_listed(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save tree that fails to list is logged, not raised through the exit path."""

    def boom(self: Path) -> None:
        raise OSError("nand went away")

    monkeypatch.setattr(Path, "iterdir", boom)
    emu = eden.Eden()
    emu._session_start = time.time() - 100

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.eden"):
        report = emu.save_and_exit(None)

    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
    assert any("could not list" in r.getMessage() for r in caplog.records)


def test_exit_survives_a_save_dir_that_cannot_be_walked(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save dir that fails to walk is logged and stepped over, not raised."""
    _save_file(save_dir, TITLE_ID, "game.dat")

    def boom(self: Path, pattern: str) -> None:
        raise OSError("save vanished")

    monkeypatch.setattr(Path, "rglob", boom)
    emu = eden.Eden()
    emu._session_start = time.time() - 100

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.eden"):
        report = emu.save_and_exit(None)

    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
    assert any("could not walk" in r.getMessage() for r in caplog.records)


def test_a_file_that_cannot_be_restamped_is_logged(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file the broker cannot stamp is logged rather than failing the exit."""
    _save_file(save_dir, TITLE_ID, "game.dat")

    def boom(path: object, times: object) -> None:
        raise OSError("read-only save")

    monkeypatch.setattr(eden.os, "utime", boom)
    emu = eden.Eden()
    emu._session_start = time.time() - 100

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.eden"):
        report = emu.save_and_exit(None)

    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
    assert any("could not restamp" in r.getMessage() for r in caplog.records)


def test_exit_stops_the_emulator(save_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit stops the process before it reads the save tree."""
    stopped: list[bool] = []
    monkeypatch.setattr(eden.Eden, "stop", lambda self: stopped.append(True))

    eden.Eden().save_and_exit(None)

    assert stopped == [True]


def test_exit_reports_no_state(save_dir: Path) -> None:
    """Exit reports that Eden has no save state to offer."""
    report = eden.Eden().save_and_exit(None)
    assert report == {"state_saved": None, "state_slot": None, "state_file": None}


def test_clearing_the_working_slot_drops_the_last_players_save(save_dir: Path) -> None:
    """A previous player's save unit must not survive into this session.

    The restore only writes the members this player's archive names, so an
    untouched leftover would still be in the save unit at exit, where the
    restamp ships that unit whole into this player's own archive.
    """
    leftover = _save_file(save_dir, TITLE_ID, "old.bin")

    eden.Eden().clear_working_slot()

    assert not leftover.exists()


def test_clearing_the_working_slot_drops_another_titles_save_too(save_dir: Path) -> None:
    """A save unit named for another title is another player's data just the same."""
    other = _save_file(save_dir, OTHER_TITLE_ID, "old.bin")

    eden.Eden().clear_working_slot()

    assert not other.exists()


def test_clearing_the_working_slot_drops_a_device_save(save_dir: Path) -> None:
    """A device save, keyed by the all-zero user id, is player data as well."""
    device = _touch(save_dir / SPACE_ID / ("0" * 32) / TITLE_ID / "device.bin")

    eden.Eden().clear_working_slot()

    assert not device.exists()


def test_clearing_the_working_slot_drops_the_previous_profile(save_dir: Path) -> None:
    """The profile store goes with the saves the paths through it key on."""
    profile = _touch(eden.PROFILE_STORE_DIR / "su" / "avators" / "profiles.dat")

    eden.Eden().clear_working_slot()

    assert not profile.exists()


def test_a_profile_store_kept_as_a_single_file_is_dropped_too(save_dir: Path) -> None:
    """A profile store laid down as one file has no children, so it is unlinked whole."""
    profile = _touch(eden.PROFILE_STORE_DIR)

    eden.Eden().clear_working_slot()

    assert not profile.exists()


def test_clearing_the_working_slot_keeps_the_save_tree_itself(save_dir: Path) -> None:
    """The subtree directories stay; Eden hangs the rest of the NAND layout off them."""
    _save_file(save_dir, TITLE_ID, "old.bin")

    eden.Eden().clear_working_slot()

    assert save_dir.is_dir()


def test_clearing_the_working_slot_keeps_installed_titles(save_dir: Path) -> None:
    """Installed content is the game itself, not save data, and sits outside the subtrees."""
    installed = _touch(save_dir.parent / "Contents" / "registered" / "0000.nca")

    eden.Eden().clear_working_slot()

    assert installed.exists()


def test_clearing_the_working_slot_does_not_follow_a_link_out_of_the_tree(
    save_dir: Path, tmp_path: Path
) -> None:
    """A symlinked save unit is unlinked, never walked into and deleted through."""
    outside = _touch(tmp_path / "outside" / "keep.bin")
    link = save_dir / "linked"
    link.symlink_to(outside.parent)

    eden.Eden().clear_working_slot()

    assert not link.exists()
    assert outside.exists()


def test_clearing_the_working_slot_logs_what_it_cannot_remove(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save unit that cannot be removed is reported, never silently left in place."""
    _save_file(save_dir, TITLE_ID, "old.bin")

    def boom(path: Path) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(eden.shutil, "rmtree", boom)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.eden"):
        eden.Eden().clear_working_slot()

    assert any("could not clear stale save data" in r.getMessage() for r in caplog.records)


def test_clearing_the_working_slot_survives_a_tree_that_cannot_be_listed(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save tree that fails to list is logged, not raised through the activate."""

    def boom(self: Path) -> None:
        raise OSError("nand went away")

    monkeypatch.setattr(Path, "iterdir", boom)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.eden"):
        eden.Eden().clear_working_slot()

    assert any("could not clear stale save data" in r.getMessage() for r in caplog.records)


def test_eden_declares_that_it_clears_stale_saves() -> None:
    """The activate contract's flag has to match what clear_working_slot actually does."""
    assert eden.Eden.clears_stale_saves is True


def test_exit_cannot_ship_a_previous_players_leftover_save(
    save_dir: Path, ini_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exit restamp can only reach what this session's own activate left behind.

    save_and_exit restamps a save unit whole so a partially rewritten save
    ships intact, which is exactly why nothing from an earlier session may
    still be in that unit by then.
    """
    monkeypatch.setattr(eden.Eden, "_spawn", lambda self, cmd, env, stdin_pipe=False: None)
    monkeypatch.setattr(eden.Eden, "stop", lambda self: None)
    leftover = _save_file(save_dir, TITLE_ID, "theirs.bin", mtime=time.time() - 5000)
    rom = _touch(tmp_path / "game.xci")

    emu = eden.Eden()
    emu.clear_working_slot()
    emu.launch(rom, resume_slot=None)
    _save_file(save_dir, TITLE_ID, "mine.bin")
    emu.save_and_exit(None)

    assert not leftover.exists()
