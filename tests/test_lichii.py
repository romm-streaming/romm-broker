"""Lichii ROM resolution, ini patching, the import decision, launch flags, and the exit save refresh.

Mirrors test_eden.py, since Lichii copies the Eden launcher, and adds the
legacy profile import decision Lichii checks at startup. Covers picking a
bootable title out of a folder, the ini keys the broker pins before every
launch, the session baseline launch records, and which saves and profile data
exit re-stamps for the delta dump.
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker import imports
from webstation_broker.emulators import lichii

from .conftest import import_zip, preflight_import, restore_import

TITLE_ID = "0100000000010000"
"""A title id directory name, the leaf of a save unit path."""
OTHER_TITLE_ID = "0100000000020000"
"""A second title id, for the saves that must stay out of this session's dump."""
USER_ID = "a" * 32
"""A profile UUID, the middle level of an account save path."""
SPACE_ID = "0" * 16
"""The save data space id every user save sits under."""
_MODULE_IMPORT_STATE_DIR = lichii.IMPORT_STATE_DIR
"""The import state directory as the module resolved it, before any fixture patches it."""


@pytest.fixture(autouse=True)
def import_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point Lichii's import state directory under tmp_path, for every test.

    Autouse because every launch writes the import decision, and the real
    directory sits under the developer's own data home.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The state directory; it is not created.
    """
    path = tmp_path / "data" / "lichii.migration"
    monkeypatch.setattr(lichii, "IMPORT_STATE_DIR", path)
    return path


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Lichii ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(lichii, "ROM_ROOT", root)
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
    path = tmp_path / "config" / "lichii" / "qt-config.ini"
    monkeypatch.setattr(lichii, "INI_PATH", path)
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
    monkeypatch.setattr(lichii, "SAVE_DIR", save)
    monkeypatch.setattr(lichii, "PROFILE_STORE_DIR", nand / "system" / "save" / "8000000000000010")
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
    assert lichii.Lichii().resolve_rom_file(rom) == rom


def test_resolve_reports_nothing_for_a_path_that_is_not_there(rom_root: Path) -> None:
    """A path that is neither a file nor a directory resolves to nothing."""
    assert lichii.Lichii().resolve_rom_file(rom_root / "missing") is None


def test_rom_pick_prefers_the_cartridge_dump_over_the_package(rom_root: Path) -> None:
    """An .xci beside an .nsp of the same game is the one picked."""
    game = rom_root / "Game"
    _touch(game / "Game.nsp")
    best = _touch(game / "Game.xci")

    assert lichii.Lichii().resolve_rom_file(game) == best


def test_rom_pick_reaches_a_title_wrapped_in_a_library_folder(rom_root: Path) -> None:
    """A title nested one folder deeper still resolves."""
    game = rom_root / "Game"
    rom = _touch(game / "Game [0100000000010000][v0]" / "Game.nsp")

    assert lichii.Lichii().resolve_rom_file(game) == rom


def test_rom_pick_skips_the_update_beside_the_base_game(rom_root: Path) -> None:
    """An update package beside the base game is not the thing booted."""
    game = rom_root / "Game"
    _touch(game / "Game (Update).nsp")
    base = _touch(game / "Game.nsp")

    assert lichii.Lichii().resolve_rom_file(game) == base


def test_rom_pick_ignores_a_file_the_loader_cannot_boot(rom_root: Path) -> None:
    """A folder holding nothing bootable resolves to nothing."""
    game = rom_root / "Game"
    _touch(game / "Game.zip")
    _touch(game / "cover.png")

    assert lichii.Lichii().resolve_rom_file(game) is None


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """A title symlinked from outside the ROM root is never picked."""
    outside = _touch(tmp_path / "outside" / "Game.xci")
    game = rom_root / "Game"
    game.mkdir()
    (game / "Game.xci").symlink_to(outside)

    assert lichii.Lichii().resolve_rom_file(game) is None


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

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.lichii"):
        assert lichii.Lichii().resolve_rom_file(game) == rom

    assert any("search of" in r.getMessage() for r in caplog.records)


def test_a_missing_ini_is_seeded_with_the_close_confirmation_off(ini_path: Path) -> None:
    """A missing qt-config.ini is created with confirmStop pinned to Ask_Never."""
    lichii._patch_ini()

    text = ini_path.read_text()
    assert "[UI]" in text
    assert "confirmStop\\default = false" in text
    assert "confirmStop = 2" in text


def test_a_missing_ini_is_seeded_with_the_backend_warning_hidden(ini_path: Path) -> None:
    """A fresh profile gets the Wayland backend modal suppressed so the game boots headless."""
    lichii._patch_ini()

    lines = ini_path.read_text().splitlines()
    assert lines.count("[UI]") == 1
    assert "gui_hide_backend_warning\\default = false" in lines
    assert "gui_hide_backend_warning = true" in lines


def test_a_shown_backend_warning_is_forced_hidden(ini_path: Path) -> None:
    """An ini that still has the backend warning on its default is pinned to hidden."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text(
        "[UI]\ngui_hide_backend_warning\\default=true\ngui_hide_backend_warning=false\n"
        "theme=dark\n"
    )

    lichii._patch_ini()

    lines = ini_path.read_text().splitlines()
    assert "gui_hide_backend_warning\\default = false" in lines
    assert "gui_hide_backend_warning = true" in lines
    assert "gui_hide_backend_warning=false" not in lines
    assert "theme=dark" in lines
    assert lines.count("[UI]") == 1


def test_the_ini_patch_keeps_what_the_player_tuned(ini_path: Path) -> None:
    """Patching pins the broker's keys and leaves every other setting alone."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text(
        "[UI]\nconfirmStop\\default=true\nconfirmStop=0\ntheme=dark\n"
        "\n[Renderer]\nbackend=1\n"
    )

    lichii._patch_ini()

    lines = ini_path.read_text().splitlines()
    assert "confirmStop\\default = false" in lines
    assert "confirmStop = 2" in lines
    assert "theme=dark" in lines
    assert "backend=1" in lines


def test_a_missing_key_is_added_to_the_section_it_belongs_in(ini_path: Path) -> None:
    """A [UI] section without the key gets it, rather than a second section."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text("[UI]\ntheme=dark\n")

    lichii._patch_ini()

    text = ini_path.read_text()
    assert text.count("[UI]") == 1
    assert "confirmStop = 2" in text
    assert "theme=dark" in text


def test_a_file_without_the_section_gets_one_appended(ini_path: Path) -> None:
    """An ini with no [UI] section gets the section and both keys appended."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text("[Renderer]\nbackend=1\n")

    lichii._patch_ini()

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

    with caplog.at_level(logging.ERROR, logger="webstation_broker.emulators.lichii"):
        with pytest.raises(OSError):
            lichii._patch_ini()

    assert any("refusing to launch" in r.getMessage() for r in caplog.records)


def test_launch_boots_the_rom_fullscreen(
    ini_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch command boots the resolved ROM fullscreen through the ini-patched config."""
    monkeypatch.setenv("LICHII_BIN", "lichii-test")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        lichii.Lichii,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = _touch(tmp_path / "game.xci")

    lichii.Lichii().launch(rom, resume_slot=None)

    assert spawned == [["lichii-test", "-f", "-g", str(rom)]]
    assert ini_path.exists()


def test_a_launch_sends_lichii_to_the_directories_the_broker_uses(
    ini_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawned emulator resolves the same config and data roots the broker writes and reads.

    Nothing on Lichii's command line names either one, so the exported XDG roots
    are the whole of the agreement. Let them drift and the broker patches a
    qt-config.ini Lichii never opens and dumps a NAND it never wrote to, which
    loses saves without reporting anything.
    """
    monkeypatch.setattr(lichii, "CONFIG_DIR", tmp_path / "cfg" / "lichii")
    monkeypatch.setattr(lichii, "DATA_DIR", tmp_path / "data" / "lichii")
    spawned: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(
        lichii.Lichii,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.update(env=env),
    )
    rom = _touch(tmp_path / "game.xci")

    lichii.Lichii().launch(rom, resume_slot=None)

    env = spawned["env"]
    assert Path(env["XDG_CONFIG_HOME"]) / "lichii" == lichii.CONFIG_DIR
    assert Path(env["XDG_DATA_HOME"]) / "lichii" == lichii.DATA_DIR


def test_launch_records_the_session_baseline(
    ini_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch stamps the baseline the exit dump scopes saves by."""
    monkeypatch.setattr(lichii.Lichii, "_spawn", lambda self, cmd, env, stdin_pipe=False: None)
    rom = _touch(tmp_path / "game.xci")
    emu = lichii.Lichii()
    assert emu._session_start == float("inf")

    before = time.time()
    emu.launch(rom, resume_slot=None)

    assert before <= emu._session_start <= time.time()


def test_a_resume_slot_is_logged_and_ignored(
    ini_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Lichii has no save states, so a resume slot changes nothing about the launch."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        lichii.Lichii,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = _touch(tmp_path / "game.xci")

    with caplog.at_level(logging.INFO, logger="webstation_broker.emulators.lichii"):
        lichii.Lichii().launch(rom, resume_slot=3)

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
        lichii.Lichii,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = _touch(tmp_path / "game.xci")

    with pytest.raises(OSError):
        lichii.Lichii().launch(rom, resume_slot=None)

    assert spawned == []


def test_the_import_state_sits_beside_the_data_directory() -> None:
    """Lichii looks for its import decision in `<data dir>.migration`, a sibling of the data dir."""
    assert _MODULE_IMPORT_STATE_DIR == lichii.DATA_DIR.parent / "lichii.migration"
    assert lichii.DATA_DIR.name == "lichii"
    assert lichii.CONFIG_DIR.name == "lichii"
    assert lichii.INI_PATH == lichii.CONFIG_DIR / "qt-config.ini"


def test_a_first_launch_records_a_fresh_start(import_state_dir: Path) -> None:
    """With no decision on record, an empty `fresh` marker is written, directory and all."""
    lichii._mark_import_fresh()

    marker = import_state_dir / "fresh"
    assert marker.is_file()
    assert marker.read_bytes() == b""


def test_a_fresh_start_is_recorded_even_with_eden_data_beside_it(import_state_dir: Path) -> None:
    """Eden's data directory is what trips Lichii's prompt in the webstation; the marker settles it.

    Eden's tree is left exactly as it was: nothing is imported from it.
    """
    eden_save = _touch(import_state_dir.parent / "eden" / "nand" / "user" / "save" / "x.bin")

    lichii._mark_import_fresh()

    assert (import_state_dir / "fresh").is_file()
    assert eden_save.read_bytes() == b"data"
    assert not (import_state_dir.parent / "lichii").exists()


@pytest.mark.parametrize("decided", ["complete", "fresh"])
def test_a_decision_already_on_record_is_left_alone(import_state_dir: Path, decided: str) -> None:
    """A finished import or an earlier fresh start writes nothing new."""
    marker = _touch(import_state_dir / decided, mtime=1000.0)

    lichii._mark_import_fresh()

    assert sorted(p.name for p in import_state_dir.iterdir()) == [decided]
    assert marker.stat().st_mtime == 1000.0


def test_recording_a_fresh_start_twice_is_harmless(import_state_dir: Path) -> None:
    """Every launch calls it; the second call finds the first one's marker."""
    lichii._mark_import_fresh()
    lichii._mark_import_fresh()

    assert sorted(p.name for p in import_state_dir.iterdir()) == ["fresh"]


@pytest.mark.parametrize("interrupted", ["pending", "journal"])
def test_an_interrupted_import_refuses_the_launch(
    import_state_dir: Path, interrupted: str, caplog: pytest.LogCaptureFixture
) -> None:
    """An import that stopped part way is refused, and its record kept for Lichii to recover."""
    record = _touch(import_state_dir / interrupted)

    with caplog.at_level(logging.ERROR, logger="webstation_broker.emulators.lichii"):
        with pytest.raises(lichii.ImportPendingError):
            lichii._mark_import_fresh()

    assert record.read_bytes() == b"data"
    assert not (import_state_dir / "fresh").exists()
    assert any("refusing to launch" in r.getMessage() for r in caplog.records)


def test_an_interrupted_import_wins_over_a_fresh_marker(import_state_dir: Path) -> None:
    """Lichii itself reads `pending` before `fresh`, so the broker refuses too."""
    _touch(import_state_dir / "fresh")
    _touch(import_state_dir / "journal")

    with pytest.raises(lichii.ImportPendingError):
        lichii._mark_import_fresh()


def test_an_unwritable_import_state_refuses_the_launch(
    import_state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A marker the broker cannot write must stop the launch: the prompt would hold the session."""
    import_state_dir.parent.mkdir(parents=True)
    import_state_dir.write_text("not a directory")

    with caplog.at_level(logging.ERROR, logger="webstation_broker.emulators.lichii"):
        with pytest.raises(OSError):
            lichii._mark_import_fresh()

    assert any("refusing to launch" in r.getMessage() for r in caplog.records)


def test_launch_records_the_import_decision_before_spawning(
    ini_path: Path, import_state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is on disk by the time Lichii starts and looks for it."""
    seen: list[bool] = []
    monkeypatch.setattr(
        lichii.Lichii,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: seen.append((import_state_dir / "fresh").is_file()),
    )
    rom = _touch(tmp_path / "game.xci")

    lichii.Lichii().launch(rom, resume_slot=None)

    assert seen == [True]


@pytest.mark.parametrize("interrupted", ["pending", "journal"])
def test_an_interrupted_import_spawns_nothing(
    ini_path: Path,
    import_state_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted: str,
) -> None:
    """A launch over an interrupted import raises before anything is spawned."""
    _touch(import_state_dir / interrupted)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        lichii.Lichii,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = _touch(tmp_path / "game.xci")

    with pytest.raises(lichii.ImportPendingError):
        lichii.Lichii().launch(rom, resume_slot=None)

    assert spawned == []


def test_an_unwritable_import_state_spawns_nothing(
    ini_path: Path, import_state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed marker write aborts the launch before anything is spawned."""
    import_state_dir.parent.mkdir(parents=True)
    import_state_dir.write_text("not a directory")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        lichii.Lichii,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = _touch(tmp_path / "game.xci")

    with pytest.raises(OSError):
        lichii.Lichii().launch(rom, resume_slot=None)

    assert spawned == []


def test_an_empty_ini_gets_the_close_confirmation_off(ini_path: Path) -> None:
    """An empty qt-config.ini, as a crashed first run can leave, gets the section and both keys."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text("")

    lichii._patch_ini()

    lines = ini_path.read_text().splitlines()
    assert lines.count("[UI]") == 1
    assert "confirmStop\\default = false" in lines
    assert "confirmStop = 2" in lines


def test_the_ini_patch_is_idempotent(ini_path: Path) -> None:
    """A second launch's patch leaves the file exactly as the first one did."""
    ini_path.parent.mkdir(parents=True)
    ini_path.write_text("[UI]\nconfirmStop=0\ntheme=dark\n")

    lichii._patch_ini()
    first = ini_path.read_text()
    lichii._patch_ini()

    assert ini_path.read_text() == first


def test_lichii_never_sends_sigint() -> None:
    """Lichii maps SIGINT to `_exit(1)`, so the stop path must never use it."""
    source = Path(lichii.__file__).read_text()

    assert "SIGINT)" not in source
    assert "signal.SIGINT" not in source


def test_lichii_is_registered_on_its_own() -> None:
    """The registry names Lichii, which is not an Eden in disguise."""
    from webstation_broker import emulators
    from webstation_broker.emulators import eden

    assert emulators.REGISTRY["lichii"] is lichii.Lichii
    assert not issubclass(lichii.Lichii, eden.Eden)
    assert lichii.Lichii.name == "lichii"
    assert lichii.Lichii.supports_states is False


def test_exit_ships_the_whole_save_of_the_title_this_session_wrote(save_dir: Path) -> None:
    """Exit re-stamps every file of a title written this session and nothing else.

    A Switch game rewrites only part of its save, so the sidecar files it left
    alone would be filtered out of the delta dump and the restored save would
    land incomplete.
    """
    emu = lichii.Lichii()
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
    emu = lichii.Lichii()
    emu._session_start = time.time() - 100
    profile = _touch(
        lichii.PROFILE_STORE_DIR / "su" / "avators" / "profiles.dat",
        mtime=emu._session_start - 5000,
    )
    _save_file(save_dir, TITLE_ID, "game.dat")

    emu.save_and_exit(None)

    assert profile.stat().st_mtime >= emu._session_start


def test_a_profile_store_kept_as_a_single_file_is_shipped_too(save_dir: Path) -> None:
    """A profile store laid down as one file, not a directory, is re-stamped as well."""
    emu = lichii.Lichii()
    emu._session_start = time.time() - 100
    profile = _touch(lichii.PROFILE_STORE_DIR, mtime=emu._session_start - 5000)
    _save_file(save_dir, TITLE_ID, "game.dat")

    emu.save_and_exit(None)

    assert profile.stat().st_mtime >= emu._session_start


def test_exit_leaves_the_profile_alone_when_no_save_was_written(save_dir: Path) -> None:
    """A session that wrote no save ships nothing, profile store included."""
    emu = lichii.Lichii()
    emu._session_start = time.time() - 100
    profile = _touch(
        lichii.PROFILE_STORE_DIR / "su" / "avators" / "profiles.dat",
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

    lichii.Lichii().save_and_exit(None)

    assert other.stat().st_mtime == before


def test_exit_skips_a_save_dir_whose_leaf_is_not_a_title_id(save_dir: Path) -> None:
    """A save unit path not ending in a title id is not a save, so it is not shipped."""
    emu = lichii.Lichii()
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
    emu = lichii.Lichii()
    emu._session_start = time.time() - 100

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.lichii"):
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
    emu = lichii.Lichii()
    emu._session_start = time.time() - 100

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.lichii"):
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

    monkeypatch.setattr(lichii.os, "utime", boom)
    emu = lichii.Lichii()
    emu._session_start = time.time() - 100

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.lichii"):
        report = emu.save_and_exit(None)

    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
    assert any("could not restamp" in r.getMessage() for r in caplog.records)


def test_exit_stops_the_emulator(save_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit stops the process before it reads the save tree."""
    stopped: list[bool] = []
    monkeypatch.setattr(lichii.Lichii, "stop", lambda self: stopped.append(True))

    lichii.Lichii().save_and_exit(None)

    assert stopped == [True]


def test_exit_reports_no_state(save_dir: Path) -> None:
    """Exit reports that Lichii has no save state to offer."""
    report = lichii.Lichii().save_and_exit(None)
    assert report == {"state_saved": None, "state_slot": None, "state_file": None}


def test_clearing_the_working_slot_drops_the_last_players_save(save_dir: Path) -> None:
    """A previous player's save unit must not survive into this session.

    The restore only writes the members this player's archive names, so an
    untouched leftover would still be in the save unit at exit, where the
    restamp ships that unit whole into this player's own archive.
    """
    leftover = _save_file(save_dir, TITLE_ID, "old.bin")

    lichii.Lichii().clear_working_slot()

    assert not leftover.exists()


def test_clearing_the_working_slot_drops_another_titles_save_too(save_dir: Path) -> None:
    """A save unit named for another title is another player's data just the same."""
    other = _save_file(save_dir, OTHER_TITLE_ID, "old.bin")

    lichii.Lichii().clear_working_slot()

    assert not other.exists()


def test_clearing_the_working_slot_drops_a_device_save(save_dir: Path) -> None:
    """A device save, keyed by the all-zero user id, is player data as well."""
    device = _touch(save_dir / SPACE_ID / ("0" * 32) / TITLE_ID / "device.bin")

    lichii.Lichii().clear_working_slot()

    assert not device.exists()


def test_clearing_the_working_slot_drops_the_previous_profile(save_dir: Path) -> None:
    """The profile store goes with the saves the paths through it key on."""
    profile = _touch(lichii.PROFILE_STORE_DIR / "su" / "avators" / "profiles.dat")

    lichii.Lichii().clear_working_slot()

    assert not profile.exists()


def test_a_profile_store_kept_as_a_single_file_is_dropped_too(save_dir: Path) -> None:
    """A profile store laid down as one file has no children, so it is unlinked whole."""
    profile = _touch(lichii.PROFILE_STORE_DIR)

    lichii.Lichii().clear_working_slot()

    assert not profile.exists()


def test_clearing_the_working_slot_keeps_the_save_tree_itself(save_dir: Path) -> None:
    """The subtree directories stay; Lichii hangs the rest of the NAND layout off them."""
    _save_file(save_dir, TITLE_ID, "old.bin")

    lichii.Lichii().clear_working_slot()

    assert save_dir.is_dir()


def test_clearing_the_working_slot_keeps_installed_titles(save_dir: Path) -> None:
    """Installed content is the game itself, not save data, and sits outside the subtrees."""
    installed = _touch(save_dir.parent / "Contents" / "registered" / "0000.nca")

    lichii.Lichii().clear_working_slot()

    assert installed.exists()


def test_clearing_the_working_slot_does_not_follow_a_link_out_of_the_tree(
    save_dir: Path, tmp_path: Path
) -> None:
    """A symlinked save unit is unlinked, never walked into and deleted through."""
    outside = _touch(tmp_path / "outside" / "keep.bin")
    link = save_dir / "linked"
    link.symlink_to(outside.parent)

    lichii.Lichii().clear_working_slot()

    assert not link.exists()
    assert outside.exists()


def test_clearing_the_working_slot_logs_what_it_cannot_remove(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save unit that cannot be removed is reported, never silently left in place."""
    _save_file(save_dir, TITLE_ID, "old.bin")

    def boom(path: Path) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(lichii.shutil, "rmtree", boom)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.lichii"):
        lichii.Lichii().clear_working_slot()

    assert any("could not clear stale save data" in r.getMessage() for r in caplog.records)


def test_clearing_the_working_slot_survives_a_tree_that_cannot_be_listed(
    save_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save tree that fails to list is logged, not raised through the activate."""

    def boom(self: Path) -> None:
        raise OSError("nand went away")

    monkeypatch.setattr(Path, "iterdir", boom)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.lichii"):
        lichii.Lichii().clear_working_slot()

    assert any("could not clear stale save data" in r.getMessage() for r in caplog.records)


def test_lichii_declares_that_it_clears_stale_saves() -> None:
    """The activate contract's flag has to match what clear_working_slot actually does."""
    assert lichii.Lichii.clears_stale_saves is True


def test_exit_cannot_ship_a_previous_players_leftover_save(
    save_dir: Path, ini_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exit restamp can only reach what this session's own activate left behind.

    save_and_exit restamps a save unit whole so a partially rewritten save
    ships intact, which is exactly why nothing from an earlier session may
    still be in that unit by then.
    """
    monkeypatch.setattr(lichii.Lichii, "_spawn", lambda self, cmd, env, stdin_pipe=False: None)
    monkeypatch.setattr(lichii.Lichii, "stop", lambda self: None)
    leftover = _save_file(save_dir, TITLE_ID, "theirs.bin", mtime=time.time() - 5000)
    rom = _touch(tmp_path / "game.xci")

    emu = lichii.Lichii()
    emu.clear_working_slot()
    emu.launch(rom, resume_slot=None)
    _save_file(save_dir, TITLE_ID, "mine.bin")
    emu.save_and_exit(None)

    assert not leftover.exists()


# -- declared imports --

_IMPORT_TITLE = "0100ABCD01230000"
"""The title the session runs, in the case Lichii names its save folder."""
_ROMM = imports.RomRef(1, "Game", "switch-2", title_id=_IMPORT_TITLE)
"""The rom a Switch 2 activate carries, with RomM's id for it."""
_ACCOUNT = f"nand/user/save/{SPACE_ID}/{USER_ID}/{_IMPORT_TITLE.lower()}"
"""An account save unit's folder as a hand-collected export might spell it."""
_DEVICE = f"nand/user/save/{SPACE_ID}/{'0' * 32}/{_IMPORT_TITLE}"
"""A device save unit's folder."""
_PROFILE = "nand/system/save/8000000000000010"
"""The profile store's folder."""
_PROFILE_FILE = f"{_PROFILE}/su/avators/profiles.dat"
"""A file in the profile store."""


@pytest.fixture
def nand_root(save_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point `Lichii.save_root` at the data directory the `save_dir` fixture built.

    A restore lands under `save_root`, which the class resolves once at
    import, so a round trip would reach outside tmp_path without this.

    Args:
        save_dir: The patched save tree.
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The data directory holding `nand`.
    """
    root = tmp_path / "data"
    monkeypatch.setattr(lichii.Lichii, "save_root", root)
    return root


def _preflight(
    members: dict[str, bytes],
    *,
    rom: Optional[imports.RomRef] = _ROMM,
    v1: Optional[dict[str, bytes]] = None,
) -> imports.PreflightResult:
    """Preflight an archive of import members against an Lichii whose NAND is patched into tmp_path.

    Args:
        members: `.import/<kind>/...` names mapped to bytes.
        rom: The activate body's rom, or None.
        v1: Ordinary archive members to carry beside them, or None.

    Returns:
        What preflight decided.
    """
    return preflight_import(lichii.Lichii(), import_zip(members, v1), rom_file=None, rom=rom)


@pytest.mark.usefixtures("nand_root")
@pytest.mark.parametrize(
    ("rel", "dest"),
    [
        (
            f"{_ACCOUNT}/save.bin",
            f"nand/user/save/{SPACE_ID}/{'A' * 32}/{_IMPORT_TITLE}/save.bin",
        ),
        (
            f"nand/user/save/{SPACE_ID}/{'B' * 32}/{_IMPORT_TITLE}/sub/dir/slot.dat",
            f"nand/user/save/{SPACE_ID}/{'B' * 32}/{_IMPORT_TITLE}/sub/dir/slot.dat",
        ),
    ],
    ids=["lower case ids", "nested"],
)
def test_an_account_save_lands_under_its_own_space_user_and_title(rel: str, dest: str) -> None:
    """A verbatim NAND export is placed where it came from, with its ids in upper case.

    Args:
        rel: The member's path below `.import/save/`.
        dest: Where it lands, below the data directory.
    """
    result = _preflight({f".import/save/{rel}": b"save", f".import/save/{_PROFILE_FILE}": b"profiles"})

    assert result.refusals == ()
    assert dest in [str(p.dest) for p in result.placements]


@pytest.mark.usefixtures("nand_root")
def test_a_device_save_stands_alone() -> None:
    """A device save needs no profile store, since it belongs to no profile."""
    result = _preflight({f".import/save/{_DEVICE.lower()}/system.bin": b"save"})

    assert result.refusals == ()
    assert [str(p.dest) for p in result.placements] == [f"{_DEVICE}/system.bin"]


@pytest.mark.usefixtures("nand_root")
def test_a_profile_store_alone_is_taken() -> None:
    """The profile store is a valid import on its own, with no identity to check."""
    result = _preflight({f".import/save/{_PROFILE_FILE}": b"profiles"}, rom=None)

    assert result.refusals == ()
    assert [str(p.dest) for p in result.placements] == [_PROFILE_FILE]


@pytest.mark.usefixtures("nand_root")
def test_an_account_save_is_taken_when_no_game_id_is_known() -> None:
    """With no id from the rom or RomM there is nothing to hold the save's title to."""
    result = _preflight(
        {f".import/save/{_ACCOUNT}/save.bin": b"save", f".import/save/{_PROFILE_FILE}": b"profiles"},
        rom=None,
    )

    assert result.refusals == ()
    assert len(result.placements) == 2


@pytest.mark.usefixtures("nand_root")
@pytest.mark.parametrize(
    ("rel", "reason"),
    [
        (f"bis/user/save/{SPACE_ID}/{USER_ID}/{_IMPORT_TITLE}/save.bin", "source_incompatible"),
        ("bis/system/save/8000000000000010/x", "source_incompatible"),
        ("Nintendo/Contents/registered/00", "source_incompatible"),
        (f"sdmc/Nintendo/save/{_IMPORT_TITLE}", "source_incompatible"),
        (f"{_IMPORT_TITLE}/save.bin", "destination_unresolvable"),
        (f"0x{_IMPORT_TITLE}/save.bin", "destination_unresolvable"),
        (f"{_IMPORT_TITLE}", "destination_unresolvable"),
        ("JKSV/Game/Slot 1/save.bin", "destination_unresolvable"),
        (f"switch/Checkpoint/saves/{_IMPORT_TITLE} Game/save.bin", "destination_unresolvable"),
        (f"nand/user/save/{SPACE_ID}/{USER_ID}/0100000000020000/save.bin", "identity_mismatch"),
        (f"nand/user/save/{SPACE_ID}/{_IMPORT_TITLE}/save.bin", "unrecognised_layout"),
        (f"nand/user/save/{SPACE_ID}/{USER_ID}/{_IMPORT_TITLE}", "unrecognised_layout"),
        ("nand/user/save/cache/save.bin", "unrecognised_layout"),
        ("nand/system/Contents/registered/00", "unrecognised_layout"),
        ("nand/system/save/8000000000000011/su/x", "unrecognised_layout"),
        (_PROFILE, "unrecognised_layout"),
        ("notes.txt", "unrecognised_layout"),
    ],
    ids=[
        "bis account save",
        "bis profile store",
        "Nintendo folder",
        "sdmc",
        "loose title folder",
        "loose 0x title folder",
        "loose title file",
        "JKSV",
        "Checkpoint",
        "another title",
        "no user level",
        "no file under the title",
        "cache space",
        "installed contents",
        "another system save",
        "a profile store with no file",
        "a loose file",
    ],
)
def test_a_member_lichii_would_not_read_is_refused(rel: str, reason: str) -> None:
    """Each shape the spec names is refused with its own code, and nothing is placed.

    Args:
        rel: The member's path below `.import/save/`.
        reason: The refusal code.
    """
    result = _preflight({f".import/save/{rel}": b"x"})

    assert [r.reason for r in result.refusals] == [reason]
    assert result.placements == ()


@pytest.mark.usefixtures("nand_root")
def test_a_raw_dump_over_the_limit_is_a_hardware_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `.bin` bigger than the limit is a raw dump; a small one is just an unrecognised file.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(lichii, "_RAW_SAVE_LIMIT", 4)

    big = _preflight({".import/save/dump.bin": b"12345678"})
    small = _preflight({".import/save/dump.bin": b"1234"})

    assert [r.reason for r in big.refusals] == ["source_incompatible"]
    assert [r.reason for r in small.refusals] == ["unrecognised_layout"]


@pytest.mark.usefixtures("nand_root")
def test_a_save_file_named_like_a_raw_dump_is_still_a_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """The size check reads members that fit no shape; a well-shaped `.bin` is placed however big.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(lichii, "_RAW_SAVE_LIMIT", 4)

    result = _preflight({f".import/save/{_DEVICE}/system.bin": b"12345678"})

    assert result.refusals == ()
    assert [str(p.dest) for p in result.placements] == [f"{_DEVICE}/system.bin"]


@pytest.mark.usefixtures("nand_root")
def test_an_account_save_needs_its_profile_store() -> None:
    """Without the profile store an account save's user folder would resolve to nothing."""
    result = _preflight({f".import/save/{_ACCOUNT}/save.bin": b"save"})

    assert [(r.reason, r.member) for r in result.refusals] == [
        ("incomplete_unit", f".import/save/{_ACCOUNT}/save.bin")
    ]


@pytest.mark.usefixtures("nand_root")
def test_a_profile_store_already_in_the_archive_does_not_stand_in_for_one_in_the_import() -> None:
    """The profile store has to come in the same import as the account save it belongs to."""
    result = _preflight({f".import/save/{_ACCOUNT}/save.bin": b"save"}, v1={_PROFILE_FILE: b"archived"})

    assert [r.reason for r in result.refusals] == ["incomplete_unit"]


@pytest.mark.usefixtures("nand_root")
def test_a_profile_store_in_the_import_and_the_archive_is_refused() -> None:
    """Two profile stores cannot both be Lichii's; the imported one is refused, not merged."""
    result = _preflight(
        {f".import/save/{_PROFILE}/su/avators/other.dat": b"imported"},
        rom=None,
        v1={_PROFILE_FILE: b"archived"},
    )

    assert [r.reason for r in result.refusals] == ["destination_conflict"]


@pytest.mark.usefixtures("nand_root")
def test_a_profile_store_on_the_same_file_as_the_archives_is_refused_once() -> None:
    """The shared clash check already names this member, so the profile check leaves it alone."""
    result = _preflight(
        {f".import/save/{_PROFILE_FILE}": b"imported"}, rom=None, v1={_PROFILE_FILE: b"archived"}
    )

    assert [r.reason for r in result.refusals] == ["destination_conflict"]


@pytest.mark.usefixtures("nand_root")
def test_an_account_save_that_clashes_is_refused_once_for_the_clash() -> None:
    """Two spellings of one file are the shared check's to refuse, not also a missing profile store."""
    upper = f"nand/user/save/{SPACE_ID}/{USER_ID.upper()}/{_IMPORT_TITLE}"

    result = _preflight(
        {f".import/save/{_ACCOUNT}/save.bin": b"one", f".import/save/{upper}/save.bin": b"two"}
    )

    assert [r.reason for r in result.refusals] == ["destination_conflict", "destination_conflict"]


@pytest.mark.usefixtures("nand_root")
def test_an_account_save_with_its_profile_store_is_taken_whole() -> None:
    """Both halves arrive together and neither is refused."""
    result = _preflight(
        {f".import/save/{_ACCOUNT}/save.bin": b"save", f".import/save/{_PROFILE_FILE}": b"profiles"}
    )

    assert result.refusals == ()
    assert len(result.placements) == 2


@pytest.mark.usefixtures("nand_root")
def test_a_state_or_memory_card_is_not_taken() -> None:
    """Lichii has no states and no cards, so the kind gate stops them before the hook."""
    result = _preflight({".import/state/game.sav": b"x", ".import/memcard/card.mcd": b"x"})

    assert sorted(r.reason for r in result.refusals) == ["kind_not_accepted", "kind_not_accepted"]


def test_the_import_paths_are_the_save_subtrees_lichii_dumps_and_restores() -> None:
    """The literals `place_import` builds destinations from are the ones the class and module declare."""
    subtrees = (lichii._SAVE_SUBTREE, lichii._PROFILE_SUBTREE)

    assert lichii.Lichii.save_subtrees == subtrees
    assert lichii.DATA_DIR.joinpath(*lichii._SAVE_WRAPPER) == lichii.SAVE_DIR
    assert lichii.DATA_DIR.joinpath(*lichii._PROFILE_WRAPPER) == lichii.PROFILE_STORE_DIR
    assert tuple("/".join(w) for w in (lichii._SAVE_WRAPPER, lichii._PROFILE_WRAPPER)) == subtrees


def test_an_imported_save_is_where_lichiis_own_lookups_find_it(save_dir: Path, nand_root: Path) -> None:
    """The placed save unit is the one the exit restamp walks, and the clear empties both trees.

    The read-back is by literal upper-case path: on a case-sensitive
    filesystem, a destination Lichii never opens would be written and no
    refusal could catch it.

    Args:
        save_dir: The patched save tree.
        nand_root: The patched data directory.
    """
    emu = lichii.Lichii()
    body = import_zip(
        {f".import/save/{_ACCOUNT}/save.bin": b"progress", f".import/save/{_PROFILE_FILE}": b"profiles"}
    )
    result = preflight_import(emu, body, rom_file=None, rom=_ROMM)
    restore_import(emu, body, result)

    unit = save_dir / SPACE_ID / ("A" * 32) / _IMPORT_TITLE
    assert (unit / "save.bin").read_bytes() == b"progress"
    assert (nand_root / _PROFILE_FILE).read_bytes() == b"profiles"
    emu._session_start = time.time() - 100
    assert emu._session_save_dirs() == [unit]

    emu.clear_working_slot()

    assert not any(save_dir.rglob("*"))
    assert not any((nand_root / _PROFILE).rglob("*"))


def test_lichii_declares_a_save_kind_only() -> None:
    """The spec names the save kind alone, with no state channel and no protected globs."""
    spec = lichii.Lichii().import_spec()

    assert [k.kind for k in spec.kinds] == ["save"]
    assert spec.state_channel == "none"
    assert spec.protected == ()
    assert spec.case_insensitive_dest is False
