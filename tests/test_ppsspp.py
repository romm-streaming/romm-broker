"""PPSSPP ROM resolution, state naming, and window selection.

Covers picking a bootable image out of a folder, the working-slot state
naming contract, and finding the game window among PPSSPP's windows.
"""

import os
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import pytest

from webstation_broker import imports
from webstation_broker.emulators import ppsspp

from .conftest import import_zip, preflight_import, restore_import


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the PPSSPP ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(ppsspp, "ROM_ROOT", root)
    return root


@pytest.fixture
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the PPSSPP state directory at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The state directory.
    """
    d = tmp_path / "PPSSPP_STATE"
    d.mkdir()
    monkeypatch.setattr(ppsspp, "STATE_DIR", d)
    # The save subtrees hang off the memory stick root, which the class resolves
    # once at import, so the clear would reach outside tmp_path without this.
    monkeypatch.setattr(ppsspp.Ppsspp, "save_root", tmp_path)
    return d


@pytest.fixture
def config_inis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point both inis the broker patches at files under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ppsspp.ini and controls.ini paths, neither written yet.
    """
    system = tmp_path / "SYSTEM"
    system.mkdir()
    ini = system / "ppsspp.ini"
    controls = system / "controls.ini"
    monkeypatch.setattr(ppsspp, "INI_PATH", ini)
    monkeypatch.setattr(ppsspp, "CONTROLS_INI_PATH", controls)
    return ini, controls


def _touch(path: Path, mtime: Optional[float] = None) -> Path:
    """Write a placeholder file, creating parents, optionally with a fixed mtime.

    Args:
        path: The file to create.
        mtime: Modification time to stamp on it, if any.

    Returns:
        The path that was written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_rom_pick_prefers_the_compressed_image_beside_the_raw_one(rom_root: Path) -> None:
    """A .chd beside an .iso of the same game is the one picked."""
    game = rom_root / "game"
    _touch(game / "Game.iso")
    _touch(game / "Game.chd")

    assert ppsspp._pick_rom_file(game.glob("*"), game).name == "Game.chd"


def test_rom_pick_ignores_unbootable_and_hidden_files(rom_root: Path) -> None:
    """Files with the wrong extension or a leading dot are never picked."""
    game = rom_root / "game"
    _touch(game / "readme.txt")
    _touch(game / ".Game.iso")

    assert ppsspp._pick_rom_file(game.glob("*"), game) is None


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """An image symlinked from outside the ROM root is never picked."""
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    game = rom_root / "game"
    game.mkdir()
    (game / "linked.iso").symlink_to(outside)

    assert ppsspp._pick_rom_file(game.glob("*"), game) is None


def test_resolve_takes_a_file_as_given(rom_root: Path) -> None:
    """A path that is already a file resolves to itself."""
    rom = _touch(rom_root / "Game.iso")

    assert ppsspp.Ppsspp().resolve_rom_file(rom) == rom


def test_resolve_searches_one_level_into_a_folder(rom_root: Path) -> None:
    """A folder is searched one level down for a bootable image."""
    _touch(rom_root / "game" / "inner" / "Game.iso")

    resolved = ppsspp.Ppsspp().resolve_rom_file(rom_root / "game")

    assert resolved.name == "Game.iso"


def test_resolve_keeps_the_candidates_it_could_read_when_one_search_pattern_fails(
    rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One unreadable subdirectory must not report a bootable title as having no boot file."""
    game = rom_root / "game"
    rom = _touch(game / "Game.iso")
    real_glob = Path.glob

    def flaky_glob(self: Path, pattern: str) -> Iterator[Path]:
        if pattern == "*/*":
            raise OSError("permission denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", flaky_glob)

    with caplog.at_level("WARNING"):
        assert ppsspp.Ppsspp().resolve_rom_file(game) == rom

    assert "rom search" in caplog.text


def test_resolve_gives_up_on_a_path_that_is_not_there(rom_root: Path) -> None:
    """A path that does not exist resolves to None."""
    assert ppsspp.Ppsspp().resolve_rom_file(rom_root / "gone") is None


def test_resolve_refuses_a_direct_path_that_is_a_symlink_out_of_the_library(
    rom_root: Path, tmp_path: Path
) -> None:
    """A direct path that is a symlink escaping the ROM library resolves to None."""
    outside = tmp_path / "elsewhere.iso"
    outside.write_bytes(b"iso")
    linked = rom_root / "Game.iso"
    linked.symlink_to(outside)

    assert ppsspp.Ppsspp().resolve_rom_file(linked) is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("ULUS10041_1_1.ppst", "ULUS10041_1_7.ppst"),
        ("ULUS10041_1_9.ppst", "ULUS10041_1_7.ppst"),
    ],
)
def test_restamp_keeps_the_game_and_rewrites_the_slot(filename: str, expected: str) -> None:
    """Restamping keeps the game id and rewrites only the slot number."""
    assert ppsspp._restamp_slot(filename, 7) == expected


@pytest.mark.parametrize(
    "filename",
    ["ULUS10041.ppst", "ULUS10041_1_1.jpg", "", "a/b_1.ppst"],
)
def test_restamp_refuses_anything_that_is_not_a_state_name(filename: str) -> None:
    """Restamping returns None for a name that is not a PPSSPP state name."""
    assert ppsspp._restamp_slot(filename, 1) is None


def test_working_slot_reads_the_newest_state_in_it(state_dir: Path) -> None:
    """The working slot resolves to the newest state in that slot, ignoring other slots."""
    _touch(state_dir / "OLD01_1_1.ppst", mtime=1000)
    newest = _touch(state_dir / "NEW01_1_1.ppst", mtime=3000)
    _touch(state_dir / "OTHER_1_2.ppst", mtime=9000)

    assert ppsspp._state_for_slot(1) == newest


def test_working_slot_is_empty_when_it_holds_nothing(state_dir: Path) -> None:
    """The working slot resolves to None when only other slots hold states."""
    _touch(state_dir / "OTHER_1_2.ppst")

    assert ppsspp._state_for_slot(1) is None


def test_state_target_names_a_push_for_the_working_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pushed state is targeted at the working slot under its own game id."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)

    target = ppsspp.Ppsspp().state_target("ULUS10041_1_5.ppst")

    assert target == state_dir / "ULUS10041_1_1.ppst"


def test_state_target_matches_the_state_already_in_the_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push for the game already in the slot targets that state, and another game is refused."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    existing = _touch(state_dir / "ULUS10041_1_1.ppst")

    assert ppsspp.Ppsspp().state_target("ULUS10041_1_9.ppst") == existing
    # A different game cannot land on top of the state the slot is holding.
    assert ppsspp.Ppsspp().state_target("ULUS20041_1_9.ppst") is None


@pytest.mark.parametrize(
    "filename",
    [
        "../escape_1.ppst",
        "",
        ".",
        "..",
        "notastate.bin",
        "ULUS10041_\u0661.ppst",
        "a\n_1.ppst",
        ".hidden_1.ppst",
        " _1.ppst",
        "a\\b_1.ppst",
        "ULUS10041_1.ppst\n",
    ],
)
def test_state_target_refuses_a_name_ppsspp_would_never_write(state_dir: Path, filename: str) -> None:
    """A push whose name PPSSPP would never write is refused.

    Args:
        state_dir: The patched state directory.
        filename: The pushed name.
    """
    assert ppsspp.Ppsspp().state_target(filename) is None


def test_clearing_the_slot_takes_every_state_not_just_the_broker_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state in any slot is the last session's, and nothing in its name says so."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    stale = _touch(state_dir / "ULUS10041_1_1.ppst")
    stale_shot = _touch(state_dir / "ULUS10041_1_1.jpg")
    other = _touch(state_dir / "ULUS10041_1_2.ppst")
    other_shot = _touch(state_dir / "ULUS10041_1_2.jpg")

    ppsspp.Ppsspp().clear_working_slot()

    assert not any(p.exists() for p in (stale, stale_shot, other, other_shot))
    assert state_dir.is_dir()


def test_clearing_the_slot_takes_the_in_game_saves_too(state_dir: Path, tmp_path: Path) -> None:
    """SAVEDATA has no whole-card route, so a leftover would ship in this session's dump."""
    savedata = tmp_path / "SAVEDATA"
    theirs = _touch(savedata / "ULUS100410000" / "DATA.BIN")

    ppsspp.Ppsspp().clear_working_slot()

    assert not theirs.parent.exists()
    assert savedata.is_dir()


def test_clearing_the_slot_removes_a_staging_file_a_killed_session_left(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging file left by a session killed mid-save must not ship to RomM as a state."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    staged = _touch(state_dir / ("ULUS10041_1_1.ppst" + ppsspp._STAGING_SUFFIX))
    other_staged = _touch(state_dir / ("ULUS10041_1_2.ppst" + ppsspp._STAGING_SUFFIX))

    ppsspp.Ppsspp().clear_working_slot()

    assert not staged.exists()
    assert not other_staged.exists()


def test_a_players_own_state_bindings_survive_the_launch_patch(
    config_inis: tuple[Path, Path],
) -> None:
    """The broker's bracket keys join the player's mapping for those actions instead of replacing it."""
    _ini, controls = config_inis
    controls.write_text(
        "\ufeff[ControlMapping]\nSave State = 10-190\nLoad State = 10-191\nRewind = 10-192\n",
        encoding="utf-8",
    )

    ppsspp._patch_config()

    body = controls.read_text(encoding="utf-8-sig")
    assert "Save State = 10-190,1-71" in body
    assert "Load State = 10-191,1-72" in body
    assert "Rewind = 10-192" in body


def test_patching_the_controls_twice_does_not_stack_the_broker_binding(
    config_inis: tuple[Path, Path],
) -> None:
    """Every launch patches the same file, so the broker's binding must land at most once."""
    _ini, controls = config_inis
    controls.write_text("\ufeff[ControlMapping]\nSave State = 10-190\n", encoding="utf-8")

    ppsspp._patch_config()
    ppsspp._patch_config()

    body = controls.read_text(encoding="utf-8-sig")
    assert body.count("1-71") == 1
    assert "Save State = 10-190,1-71" in body


def test_a_missing_controls_file_is_seeded_with_the_broker_bindings(
    config_inis: tuple[Path, Path],
) -> None:
    """A container with no controls.ini yet is seeded with the bracket bindings, BOM included."""
    _ini, controls = config_inis

    ppsspp._patch_config()

    raw = controls.read_text(encoding="utf-8")
    assert raw.startswith("\ufeff[ControlMapping]")
    assert "Save State = 1-71" in raw
    assert "Load State = 1-72" in raw


def test_a_controls_file_without_the_state_actions_gains_them(
    config_inis: tuple[Path, Path],
) -> None:
    """An action the file never mentions is added under its section."""
    _ini, controls = config_inis
    controls.write_text("\ufeff[ControlMapping]\nRewind = 10-192\n", encoding="utf-8")

    ppsspp._patch_config()

    body = controls.read_text(encoding="utf-8-sig")
    assert "Save State = 1-71" in body
    assert "Rewind = 10-192" in body


def test_the_broker_owned_settings_are_still_written_over(config_inis: tuple[Path, Path]) -> None:
    """ppsspp.ini settings the broker owns are replaced outright, not merged."""
    ini, _controls = config_inis
    ini.write_text("\ufeff[General]\nFirstRun = True\nStateSlot = 4\n", encoding="utf-8")

    ppsspp._patch_config()

    body = ini.read_text(encoding="utf-8-sig")
    assert "FirstRun = False" in body
    assert f"StateSlot = {ppsspp.STATE_SLOT}" in body
    assert "StateSlot = 4" not in body


def test_a_config_the_patcher_cannot_read_fails_the_patch(
    config_inis: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """An unpatched controls.ini leaves the bracket keys unbound, so the failure has to surface."""
    _ini, controls = config_inis
    controls.mkdir()

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="could not apply broker settings"):
            ppsspp._patch_config()

    assert "patch failed" in caplog.text


def test_a_config_that_is_not_decodable_text_fails_the_patch(
    config_inis: tuple[Path, Path],
) -> None:
    """A corrupt ini is not silently launched past: nothing the broker forces would be applied."""
    ini, _controls = config_inis
    ini.write_bytes(b"\xff\xfe[General]\n")

    with pytest.raises(RuntimeError, match="could not apply broker settings"):
        ppsspp._patch_config()


def test_a_launch_whose_config_cannot_be_patched_never_spawns(
    config_inis: tuple[Path, Path], tmp_path: Path
) -> None:
    """A session on an unpatched config parks on the setup wizard with dead hotkeys, so it is refused."""
    ini, _controls = config_inis
    ini.mkdir()
    emu = ppsspp.Ppsspp()
    emu.stop = lambda: None
    emu._spawn = lambda cmd, env: pytest.fail("ppsspp spawned with its config unpatched")

    with pytest.raises(RuntimeError):
        emu.launch(tmp_path / "Game.iso", None)


def test_a_launch_sends_ppsspp_to_the_config_root_the_broker_uses(
    config_inis: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawned emulator resolves the same config root the broker patches and reads back.

    Nothing on PPSSPP's command line names it, so the exported XDG root is the
    whole of the agreement. The memory stick lives under that root too, so a
    drift both strands the patched inis and points the save dump at a tree
    PPSSPP does not write to.
    """
    monkeypatch.setattr(ppsspp, "CONFIG_DIR", tmp_path / "cfg" / "ppsspp")
    spawned: dict[str, dict[str, str]] = {}
    emu = ppsspp.Ppsspp()
    emu.stop = lambda: None
    emu._spawn = lambda cmd, env: spawned.update(env=env)

    emu.launch(tmp_path / "Game.iso", None)

    assert Path(spawned["env"]["XDG_CONFIG_HOME"]) / "ppsspp" == ppsspp.CONFIG_DIR


class _FakeProc:
    """Stand-in for a spawned emulator process, carrying only the pid the window search matches on."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


def test_the_game_window_is_this_launchs_window_titled_with_a_running_game() -> None:
    """The game window is the window owned by this launch whose title names a running game."""
    emu = ppsspp.Ppsspp()
    emu._proc = _FakeProc(pid=4242)
    windows = {
        "111": ("4242", "PPSSPP 1.20.4"),
        "222": ("4242", "Controller Settings"),
        "333": ("4242", "PPSSPP 1.20.4 - ULUS10041 : Some Game"),
    }

    def fake_xdotool(*args: str) -> str:
        if args[0] == "search":
            return "111\n222\n333\n"
        if args[0] == "getwindowpid":
            return windows[args[1]][0]
        if args[0] == "getwindowname":
            return windows[args[1]][1]
        return ""

    emu._xdotool = fake_xdotool

    assert emu._game_window() == "333"


def test_a_leftover_window_from_the_previous_process_never_takes_the_hotkey(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A game-titled window owned by an older emulator process is skipped, not handed the hotkey."""
    emu = ppsspp.Ppsspp()
    emu._proc = _FakeProc(pid=4242)

    def fake_xdotool(*args: str) -> str:
        if args[0] == "search":
            return "111\n"
        if args[0] == "getwindowpid":
            return "9999\n"
        if args[0] == "getwindowname":
            raise AssertionError("a window owned by another pid was inspected")
        return ""

    emu._xdotool = fake_xdotool

    with caplog.at_level("WARNING"):
        assert emu._game_window() is None

    assert "no ppsspp game window found for pid 4242" in caplog.text


def test_no_game_window_without_a_launched_process() -> None:
    """With no process spawned there is no window to send a hotkey to."""
    emu = ppsspp.Ppsspp()
    emu._xdotool = lambda *args: pytest.fail("xdotool run with no process launched")

    assert emu._game_window() is None


def test_no_game_window_when_only_the_menu_is_up() -> None:
    """No game window is found while only the PPSSPP menu window is open."""
    emu = ppsspp.Ppsspp()
    emu._proc = _FakeProc(pid=4242)

    def fake_xdotool(*args: str) -> str:
        if args[0] == "search":
            return "111\n"
        if args[0] == "getwindowpid":
            return "4242\n"
        return "PPSSPP 1.20.4"

    emu._xdotool = fake_xdotool

    assert emu._game_window() is None


class _FakeClock:
    """Stand-in for the time module so the state-write poller runs on a clock the test drives."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake clock instead of blocking."""
        self.now += seconds


def test_a_write_that_stalls_mid_flight_is_not_reported_as_complete(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that pauses mid-write is not a finished state: reporting one ships RomM a truncated save."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    state = state_dir / "ULUS10041_1_1.ppst"
    # 0.8 s of no progress at 100 bytes, then the rest of the write lands.
    sizes = iter([100] * 8 + [4096] * 200)

    def stalling_snapshot() -> dict[Path, tuple[int, float]]:
        return {state: (next(sizes), 1000.0)}

    monkeypatch.setattr(ppsspp, "_snapshot", stalling_snapshot)

    assert ppsspp._wait_for_state_write({}, 60.0) is True
    assert clock.now >= 0.8 + ppsspp.STATE_STABLE


def test_a_state_still_being_staged_is_not_reported_as_complete(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A staging file left beside the state means the write is still going: PPSSPP renames it on success."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    _touch(state_dir / "ULUS10041_1_1.ppst")
    _touch(state_dir / ("ULUS10041_1_1.ppst" + ppsspp._STAGING_SUFFIX))

    with caplog.at_level("WARNING"):
        assert ppsspp._wait_for_state_write({}, 30.0) is False

    assert "never finished writing" in caplog.text


def test_a_state_that_settles_with_nothing_left_staged_is_reported_complete(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-empty state that holds still with no staging file beside it is a finished write."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    _touch(state_dir / "ULUS10041_1_1.ppst")

    assert ppsspp._wait_for_state_write({}, 30.0) is True
    assert clock.now >= ppsspp.STATE_STABLE


def test_an_empty_state_file_is_never_reported_as_complete(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero-byte state is a write that never got anywhere, however still it holds."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    (state_dir / "ULUS10041_1_1.ppst").write_bytes(b"")

    assert ppsspp._wait_for_state_write({}, 30.0) is False


def test_a_state_untouched_since_the_hotkey_is_never_reported_as_complete(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A slot whose state never changed after the save hotkey times out rather than reporting a save."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    _touch(state_dir / "ULUS10041_1_1.ppst")

    with caplog.at_level("WARNING"):
        assert ppsspp._wait_for_state_write(ppsspp._snapshot(), 30.0) is False

    assert "no save state was written" in caplog.text


def test_load_state_refuses_an_empty_slot(state_dir: Path) -> None:
    """Loading an empty slot returns False without sending a hotkey."""
    emu = ppsspp.Ppsspp()
    emu._send_key = lambda key: pytest.fail("hotkey sent at an empty slot")

    assert emu.load_state(1) is False


def test_backdating_puts_the_access_time_behind_the_mtime(state_dir: Path) -> None:
    """A state's access time is stamped behind its own mtime, which is left alone."""
    state = _touch(state_dir / "ULUS10041_1_1.ppst", mtime=5000)

    marker = ppsspp._backdate_atime(state)

    assert marker == 5000 - ppsspp._ATIME_BACKDATE
    st = state.stat()
    assert st.st_atime == marker
    assert st.st_mtime == 5000


def test_backdating_a_state_that_is_not_there_reports_no_marker(state_dir: Path) -> None:
    """A state that vanished before the load leaves no marker to watch."""
    assert ppsspp._backdate_atime(state_dir / "gone_1_1.ppst") is None


def test_the_access_time_probe_measures_the_filesystem_and_cleans_up(tmp_path: Path) -> None:
    """The probe agrees with what a read actually does to an access time, and leaves nothing."""
    probe = tmp_path / "probe"
    probe.write_bytes(b"x")
    marker = probe.stat().st_mtime - ppsspp._ATIME_BACKDATE
    os.utime(probe, (marker, probe.stat().st_mtime))
    probe.read_bytes()
    if probe.stat().st_atime <= marker:
        pytest.skip("the test filesystem does not record access times")
    probe.unlink()

    assert ppsspp._atime_tracked(tmp_path) is True
    assert list(tmp_path.iterdir()) == []


def test_the_access_time_probe_fails_closed_on_a_directory_that_is_not_there(
    tmp_path: Path,
) -> None:
    """A state directory the probe cannot write to reports no access-time tracking."""
    assert ppsspp._atime_tracked(tmp_path / "gone") is False


def test_a_read_of_the_state_confirms_the_load(state_dir: Path) -> None:
    """The load is confirmed once something moves the state's access time past the marker."""
    state = _touch(state_dir / "ULUS10041_1_1.ppst", mtime=5000)
    marker = ppsspp._backdate_atime(state)
    os.utime(state, (5000, 5000))

    assert ppsspp._wait_for_state_read(state, marker, time.monotonic() + 0.5) is True


def test_a_state_nothing_ever_read_is_not_a_load(state_dir: Path) -> None:
    """An access time that never moves means the hotkey never reached the core."""
    state = _touch(state_dir / "ULUS10041_1_1.ppst", mtime=5000)
    marker = ppsspp._backdate_atime(state)

    assert ppsspp._wait_for_state_read(state, marker, time.monotonic() + 0.3) is False


def _loadable(
    monkeypatch: pytest.MonkeyPatch, state_dir: Path, reads: bool, tracked: bool = True
) -> ppsspp.Ppsspp:
    """Build an emulator whose load hotkey optionally reads the state back.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        state_dir: The state directory holding the working slot.
        reads: Whether the hotkey moves the state's access time, as a real load would.
        tracked: What the access-time probe reports for the state directory.

    Returns:
        The emulator, with a state already in the working slot.
    """
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    monkeypatch.setattr(ppsspp, "LOAD_WAIT", 0.5)
    monkeypatch.setattr(ppsspp, "LOAD_SETTLE", 0.0)
    monkeypatch.setattr(ppsspp, "_atime_tracked", lambda d: tracked)
    state = _touch(state_dir / "ULUS10041_1_1.ppst", mtime=5000)
    emu = ppsspp.Ppsspp()

    def fake_send(key: str) -> bool:
        """Send the load hotkey, reading the state back when the emulator would."""
        assert key == ppsspp.LOAD_KEY
        if reads:
            os.utime(state, (time.time(), 5000))
        return True

    emu._send_key = fake_send
    return emu


def test_load_state_waits_for_ppsspp_to_read_the_state_back(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load whose state was read back reports success."""
    emu = _loadable(monkeypatch, state_dir, reads=True)

    assert emu.load_state(1) is True


def test_a_dropped_load_hotkey_is_not_reported_as_a_load(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A hotkey an unfocused or still-booting PPSSPP swallowed never reads the state, so it fails."""
    emu = _loadable(monkeypatch, state_dir, reads=False)

    with caplog.at_level("WARNING"):
        assert emu.load_state(1) is False

    assert "never read" in caplog.text


def test_a_load_hotkey_that_could_not_be_sent_fails(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load with no game window to send the hotkey at fails without waiting it out."""
    emu = _loadable(monkeypatch, state_dir, reads=False)
    emu._send_key = lambda key: False

    assert emu.load_state(1) is False


def test_a_load_is_taken_on_trust_where_access_times_are_not_recorded(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a noatime mount the read cannot be seen, so a sent hotkey is not called a failure."""
    emu = _loadable(monkeypatch, state_dir, reads=False, tracked=False)

    assert emu.load_state(1) is True


def test_exit_reports_the_working_slot_without_a_running_emulator(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit with no emulator running reports the working slot and no saved state."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)

    report = ppsspp.Ppsspp().save_and_exit(4)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


def test_a_resume_load_waits_for_the_game_window_before_sending_the_hotkey(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume state is on disk before the boot starts, so the hotkey has to wait on the window."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    _touch(state_dir / "ULUS10041_1_1.ppst")
    emu = ppsspp.Ppsspp()
    emu._launch_seq = 1
    emu.wait_for_state = lambda deadline: True
    # The game window only turns up 20 s into the boot, long past the settle
    # the old code sent its one and only hotkey after.
    emu._game_window = lambda log_missing=True: "333" if clock.now >= 20.0 else None
    sent: list[tuple[str, float]] = []
    emu._send_key = lambda key: bool(sent.append((key, clock.now))) or True

    emu._deferred_load_state(1)

    assert sent == [(ppsspp.LOAD_KEY, 20.0 + ppsspp.RESUME_LOAD_SETTLE)]


def test_a_resume_load_is_abandoned_when_no_game_window_ever_comes_up(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A boot that never reaches a game gets no blind hotkey fired into it."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    _touch(state_dir / "ULUS10041_1_1.ppst")
    emu = ppsspp.Ppsspp()
    emu._launch_seq = 1
    emu.wait_for_state = lambda deadline: True
    emu._game_window = lambda log_missing=True: None
    emu._send_key = lambda key: pytest.fail("load hotkey sent with no game window up")

    with caplog.at_level("WARNING"):
        emu._deferred_load_state(1)

    assert "no game window came up in time" in caplog.text


def test_a_resume_load_is_dropped_when_the_launch_is_superseded_while_booting(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A session that ended during the window wait must not have a hotkey land in the next one."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    _touch(state_dir / "ULUS10041_1_1.ppst")
    emu = ppsspp.Ppsspp()
    emu._launch_seq = 1
    emu.wait_for_state = lambda deadline: True

    def window_then_relaunch(log_missing: bool = True) -> Optional[str]:
        emu._launch_seq = 2
        return "333"

    emu._game_window = window_then_relaunch
    emu._send_key = lambda key: pytest.fail("load hotkey sent for a superseded launch")

    with caplog.at_level("INFO"):
        emu._deferred_load_state(1)

    assert "launch superseded" in caplog.text


# -- declared imports --

_ROMM_ID = imports.RomRef(1, "Game", "psp", title_id="ULUS-10041")
"""An activate's rom, carrying the product code RomM holds for it."""


@pytest.fixture
def psp_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the memory stick root, and so both save subtrees, under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The memory stick root.
    """
    monkeypatch.setattr(ppsspp, "STATE_DIR", tmp_path / "PPSSPP_STATE")
    monkeypatch.setattr(ppsspp.Ppsspp, "save_root", tmp_path)
    return tmp_path


def _preflight(members: dict[str, bytes], **kwargs: Any) -> imports.PreflightResult:
    """Preflight an archive of import members on a fresh PPSSPP, resuming slot 1.

    Args:
        members: `.import/<kind>/...` names mapped to bytes.
        **kwargs: Extra `preflight_import` arguments, such as `rom`.

    Returns:
        What preflight decided.
    """
    kwargs.setdefault("resume_slot", 1)
    return preflight_import(ppsspp.Ppsspp(), import_zip(members), rom_file=None, **kwargs)


@pytest.mark.parametrize(
    "member",
    [
        ".import/save/ULUS10041DATA00/PARAM.SFO",
        ".import/save/SAVEDATA/ULUS10041DATA00/PARAM.SFO",
        ".import/save/PSP/SAVEDATA/ULUS10041DATA00/PARAM.SFO",
        ".import/save/memstick/PSP/SAVEDATA/ULUS10041DATA00/PARAM.SFO",
    ],
)
def test_a_save_folder_lands_under_savedata_however_deep_it_was_packed(psp_root: Path, member: str) -> None:
    """A save folder is found under any of the wrappers a memory stick copy leaves.

    Args:
        psp_root: The patched memory stick root.
        member: The member's zip name.
    """
    result = _preflight({member: b"sfo"})

    assert result.refusals == ()
    assert [p.dest for p in result.placements] == [PurePosixPath("SAVEDATA/ULUS10041DATA00/PARAM.SFO")]


def test_a_save_folder_without_param_sfo_is_incomplete(psp_root: Path) -> None:
    """PPSSPP lists a save by its `PARAM.SFO`; without one the folder is not a save.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight({".import/save/ULUS10041DATA00/DATA.BIN": b"data"})

    assert [(r.reason, r.detail) for r in result.refusals] == [("incomplete_unit", "missing PARAM.SFO")]


def test_two_titles_save_folders_are_each_a_unit(psp_root: Path) -> None:
    """Each folder under `SAVEDATA` is checked on its own.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight(
        {
            ".import/save/ULUS10041DATA00/PARAM.SFO": b"sfo",
            ".import/save/ULUS10041DATA00/DATA.BIN": b"data",
            ".import/save/ULES00151SYS/DATA.BIN": b"data",
        }
    )

    assert [(r.reason, r.member) for r in result.refusals] == [
        ("incomplete_unit", ".import/save/ULES00151SYS/DATA.BIN")
    ]


def test_another_titles_save_folder_is_allowed(psp_root: Path) -> None:
    """A sequel can read its predecessor's save, so a save folder's code is not held to RomM's.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight({".import/save/ULES00151DATA00/PARAM.SFO": b"sfo"}, rom=_ROMM_ID)

    assert result.refusals == ()


@pytest.mark.parametrize(
    ("member", "reason", "detail"),
    [
        (".import/save/PARAM.SFO", "unrecognised_layout", "a single file; a PSP save is a folder"),
        (".import/save/Game.srm", "source_incompatible", "a RetroArch save file"),
        (
            ".import/save/PSP/SYSTEM/CONFIG.BIN",
            "protected_destination",
            "PSP/SYSTEM is emulator configuration",
        ),
        (
            ".import/save/PSP/PPSSPP_STATE/ULUS10041_1.00_1.ppst",
            "unrecognised_layout",
            "a PPSSPP state: declare it as kind state",
        ),
        (
            ".import/save/PPSSPP_STATE/ULUS10041_1.00_1.ppst",
            "unrecognised_layout",
            "a PPSSPP state: declare it as kind state",
        ),
        (".import/save/PSP/GAME/EBOOT.PBP", "unrecognised_layout", "PSP/GAME holds no saves"),
        (".import/save/mysaves/PARAM.SFO", "unrecognised_layout", None),
    ],
)
def test_a_save_member_that_is_not_a_save_folder_is_refused(
    psp_root: Path, member: str, reason: str, detail: Optional[str]
) -> None:
    """Each wrong shape is refused with the reason that tells the player what to do.

    Args:
        psp_root: The patched memory stick root.
        member: The member's zip name.
        reason: The refusal code.
        detail: The refusal's detail.
    """
    result = _preflight({member: b"x"})

    assert [(r.reason, r.detail) for r in result.refusals] == [(reason, detail)]


def test_a_state_and_its_screenshot_land_in_the_working_slot(psp_root: Path) -> None:
    """Both are restamped into the broker's slot; the screenshot does not count as a second state.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight(
        {
            ".import/state/ULUS10041_1.00_4.ppst": b"progress",
            ".import/state/ULUS10041_1.00_4.jpg": b"jpeg",
        },
        rom=_ROMM_ID,
    )

    assert result.refusals == ()
    assert sorted(p.dest for p in result.placements) == [
        PurePosixPath(f"PPSSPP_STATE/ULUS10041_1.00_{ppsspp.STATE_SLOT}.jpg"),
        PurePosixPath(f"PPSSPP_STATE/ULUS10041_1.00_{ppsspp.STATE_SLOT}.ppst"),
    ]


def test_a_screenshot_without_its_state_is_incomplete(psp_root: Path) -> None:
    """A screenshot on its own resumes nothing.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight(
        {
            ".import/state/ULUS10041_1.00_4.ppst": b"progress",
            ".import/state/NPJH50001_1.00_2.jpg": b"jpeg",
        }
    )

    assert [(r.reason, r.member) for r in result.refusals] == [
        ("incomplete_unit", ".import/state/NPJH50001_1.00_2.jpg")
    ]


def test_a_state_for_another_title_is_refused(psp_root: Path) -> None:
    """A state only loads into the game that wrote it.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight({".import/state/ULES00151_1.00_1.ppst": b"progress"}, rom=_ROMM_ID)

    assert [r.reason for r in result.refusals] == ["identity_mismatch"]


def test_a_retroarch_state_declared_as_a_state_is_refused(psp_root: Path) -> None:
    """A libretro core's numbered state is no PPSSPP state, whatever its extension looks like.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight({".import/state/Game.state1": b"progress"})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("source_incompatible", "a RetroArch (libretro) state")
    ]


def test_a_homebrew_state_is_taken_on_trust(psp_root: Path) -> None:
    """A homebrew id is no product code, so there is nothing to compare.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight({".import/state/HOMEBREW_1.00_1.ppst": b"progress"}, rom=_ROMM_ID)

    assert result.refusals == ()


def test_an_empty_state_is_incomplete(psp_root: Path) -> None:
    """A zero-byte state would boot the game from scratch without a word.

    Args:
        psp_root: The patched memory stick root.
    """
    result = _preflight({".import/state/ULUS10041_1.00_1.ppst": b""})

    assert [r.reason for r in result.refusals] == ["incomplete_unit"]


def test_an_archived_screenshot_does_not_count_against_an_imported_state(psp_root: Path) -> None:
    """A v1 screenshot is a `state_screenshot`, not a state, so the import still fits.

    Args:
        psp_root: The patched memory stick root.
    """
    body = import_zip(
        {".import/state/ULUS10041_1.00_1.ppst": b"progress"},
        v1={"PPSSPP_STATE/ULUS10041_1.00_1.jpg": b"jpeg"},
    )

    result = preflight_import(ppsspp.Ppsspp(), body, rom_file=None, resume_slot=1)

    assert result.refusals == ()


@pytest.mark.parametrize(
    "undo", ["PPSSPP_STATE/ULUS10041_1.00_1.undo.ppst", "PPSSPP_STATE/load_undo.ppst"]
)
def test_an_archived_undo_state_does_not_count_against_an_imported_state(psp_root: Path, undo: str) -> None:
    """PPSSPP's save and load undo buffers sit beside the slot's state but are not one, so the import fits.

    Args:
        psp_root: The patched memory stick root.
        undo: The archived undo buffer's path.
    """
    body = import_zip({".import/state/ULUS10041_1.00_1.ppst": b"progress"}, v1={undo: b"undo"})

    result = preflight_import(ppsspp.Ppsspp(), body, rom_file=None, resume_slot=1)

    assert result.refusals == ()


def test_an_archived_state_leaves_no_room_for_an_imported_one(psp_root: Path) -> None:
    """The broker resumes one state; an archive that already carries one takes no second.

    Args:
        psp_root: The patched memory stick root.
    """
    body = import_zip(
        {".import/state/ULUS10041_1.00_1.ppst": b"progress"},
        v1={"PPSSPP_STATE/ULUS10041_1.00_1.ppst": b"older"},
    )

    result = preflight_import(ppsspp.Ppsspp(), body, rom_file=None, resume_slot=1)

    assert [r.reason for r in result.refusals] == ["destination_conflict"]


def test_a_pushed_state_for_another_title_is_refused(state_dir: Path) -> None:
    """The push route takes a state named for the session's product code, or for none it can read.

    Args:
        state_dir: The patched state directory.
    """
    emu = ppsspp.Ppsspp()
    emu.import_identity = imports.SessionIdentity("ULUS10041", "romm")

    assert emu.state_target("ULES00151_1.00_3.ppst") is None
    assert emu.state_target("ULUS10041_1.00_3.ppst") == state_dir / f"ULUS10041_1.00_{ppsspp.STATE_SLOT}.ppst"
    assert emu.state_target("HOMEBREW_1.00_3.ppst") == state_dir / f"HOMEBREW_1.00_{ppsspp.STATE_SLOT}.ppst"


def test_a_push_refused_for_another_title_logs_both_ids_and_the_override(
    state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log line tells an identity refusal from a bad name: both ids, RomM as the source, and the fix.

    Args:
        state_dir: The patched state directory.
        caplog: The pytest log capture fixture.
    """
    emu = ppsspp.Ppsspp()
    emu.import_identity = imports.SessionIdentity("ULUS10041", "romm")

    with caplog.at_level("WARNING"):
        assert emu.state_target("ULES00151_1.00_3.ppst") is None

    assert (
        "ppsspp: refusing pushed state ULES00151_1.00_3.ppst, which names another game: member ULES00151,"
        " session ULUS10041 (from romm) - fix via PUT /api/roms/{id}/identity if RomM is wrong"
    ) in caplog.text


def test_a_push_after_an_import_must_match_the_imported_state(psp_root: Path) -> None:
    """The imported state holds the slot, so a push lands on it only under the same game id and version.

    The push's slot does not matter: every name is restamped into the working
    slot before it is compared, as for a state the broker saved itself.

    Args:
        psp_root: The patched memory stick root.
    """
    emu = ppsspp.Ppsspp()
    body = import_zip({".import/state/ULUS10041_1.00_4.ppst": b"progress"})
    result = _preflight({".import/state/ULUS10041_1.00_4.ppst": b"progress"}, rom=_ROMM_ID)

    report = restore_import(emu, body, result)
    emu.import_identity = result.identity

    imported = ppsspp.STATE_DIR / f"ULUS10041_1.00_{ppsspp.STATE_SLOT}.ppst"
    assert (report["imported"], report["failed"]) == (1, 0)
    assert imported.read_bytes() == b"progress"
    assert emu.state_target(imported.name) == imported
    assert emu.state_target("ULUS10041_1.00_7.ppst") == imported
    assert emu.state_target("ULUS10041_1.01_4.ppst") is None
    assert emu.state_target("HOMEBREW_1.00_4.ppst") is None
    assert emu.state_target("ULES00151_1.00_4.ppst") is None


# -- Archived ROMs --


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_inis: tuple[Path, Path]) -> Path:
    """Turn the extraction cache on and point it under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.
        config_inis: Redirects the inis a launch patches.

    Returns:
        The cache directory, not created yet.
    """
    d = tmp_path / "extracted"
    monkeypatch.setattr(ppsspp, "CACHE_DIR", d)
    monkeypatch.setattr(ppsspp.settings, "PPSSPP_CACHE_ENABLED", True)
    return d


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    """Write a zip, creating parents.

    Args:
        path: The archive to write.
        members: Member path to its contents.

    Returns:
        The archive.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    return path


def _launched(emu: ppsspp.Ppsspp, rom: Path) -> list[str]:
    """Launch `rom` with the spawn stubbed out, and return the argv it would have run.

    Args:
        emu: The launcher.
        rom: What `resolve_rom_file` handed back.

    Returns:
        The argv.
    """
    spawned: dict[str, list[str]] = {}
    emu.stop = lambda: None
    emu._spawn = lambda cmd, env: spawned.update(cmd=cmd)
    emu.launch(rom, None)
    return spawned["cmd"]


def test_a_zipped_image_resolves_to_the_archive(rom_root: Path, cache: Path) -> None:
    """An archive holding a PSP image is accepted for `launch` to extract."""
    archive = _zip(rom_root / "Game.zip", {"Game.iso": b"iso"})

    assert ppsspp.Ppsspp().resolve_rom_file(archive) == archive


def test_an_archive_holding_no_psp_image_is_refused(rom_root: Path, cache: Path) -> None:
    """Refused at resolve, from the member list, rather than after an extraction."""
    archive = _zip(rom_root / "Game.zip", {"readme.txt": b"hi"})

    assert ppsspp.Ppsspp().resolve_rom_file(archive) is None


def test_a_corrupt_archive_is_refused(rom_root: Path, cache: Path) -> None:
    """A file that only claims to be a zip is a clean refusal."""
    archive = _touch(rom_root / "Game.zip")

    assert ppsspp.Ppsspp().resolve_rom_file(archive) is None


def test_an_archive_is_refused_with_the_cache_off(
    rom_root: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nowhere to extract it, an archive cannot boot and is not advertised."""
    monkeypatch.setattr(ppsspp.settings, "PPSSPP_CACHE_ENABLED", False)
    archive = _zip(rom_root / "Game.zip", {"Game.iso": b"iso"})
    emu = ppsspp.Ppsspp()

    assert emu.resolve_rom_file(archive) is None
    assert ".zip" not in emu.rom_extensions


def test_a_folder_holding_only_an_archive_resolves_to_it(rom_root: Path, cache: Path) -> None:
    """A ROM folder with no image of its own falls back to its one archive."""
    archive = _zip(rom_root / "game" / "Game.zip", {"Game.cso": b"cso"})

    assert ppsspp.Ppsspp().resolve_rom_file(rom_root / "game") == archive.resolve()


def test_an_image_beside_an_archive_wins(rom_root: Path, cache: Path) -> None:
    """The archive is only a fallback, never picked over a bootable image."""
    _zip(rom_root / "game" / "Game.zip", {"Game.iso": b"iso"})
    _touch(rom_root / "game" / "Game.iso")

    assert ppsspp.Ppsspp().resolve_rom_file(rom_root / "game").name == "Game.iso"


def test_a_folder_of_several_archives_is_refused(rom_root: Path, cache: Path) -> None:
    """Two archives and no image is ambiguous, so neither is guessed at."""
    _zip(rom_root / "game" / "Disc A.zip", {"A.iso": b"iso"})
    _zip(rom_root / "game" / "Disc B.zip", {"B.iso": b"iso"})

    assert ppsspp.Ppsspp().resolve_rom_file(rom_root / "game") is None


def test_an_archive_in_a_subfolder_never_makes_the_games_own_ambiguous(
    rom_root: Path, cache: Path
) -> None:
    """An extras bundle one folder down is not a second candidate for the game."""
    archive = _zip(rom_root / "game" / "Game.zip", {"Game.cso": b"cso"})
    _zip(rom_root / "game" / "extras" / "Manual.zip", {"Manual.iso": b"iso"})

    assert ppsspp.Ppsspp().resolve_rom_file(rom_root / "game") == archive.resolve()


def test_an_extracted_archive_is_not_listed_again(
    rom_root: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its extraction already showed a PSP image, so a later activate skips the listing."""
    archive = _zip(rom_root / "Game.zip", {"Game.iso": b"iso"})
    emu = ppsspp.Ppsspp()
    _launched(emu, emu.resolve_rom_file(archive))

    def never_lists(archive: Path, timeout: float) -> list[str]:
        """Fail the test if the archive is listed.

        Args:
            archive: The archive.
            timeout: The lister timeout.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(f"listed {archive.name} again")

    monkeypatch.setattr(ppsspp.extraction_cache, "list_members", never_lists)

    assert ppsspp.Ppsspp().resolve_rom_file(archive) == archive


def test_a_startup_sweep_it_cannot_read_never_stops_the_broker(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable cache dir skips the sweep instead of failing startup."""

    def unreadable() -> None:
        """Fail the way an unreadable scratch dir would.

        Raises:
            PermissionError: Always.
        """
        raise PermissionError("permission denied")

    monkeypatch.setattr(ppsspp._CACHE, "_clear_scratch", unreadable)

    ppsspp.sweep_stale_extractions()


def test_an_archived_rom_boots_its_extracted_image(rom_root: Path, cache: Path) -> None:
    """The image inside the archive, wrapper folder and all, is what PPSSPP is handed."""
    archive = _zip(rom_root / "Game.zip", {"Game (USA)/Game (USA).iso": b"iso"})
    emu = ppsspp.Ppsspp()

    cmd = _launched(emu, emu.resolve_rom_file(archive))

    booted = Path(cmd[-1])
    assert cmd[-2] == "--"
    assert booted.name == "Game (USA).iso"
    assert booted.is_relative_to(cache.resolve())
    assert booted.read_bytes() == b"iso"
    assert emu.extraction_phase is None


def test_a_second_launch_reuses_the_extraction(
    rom_root: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive is extracted once, then booted from the cache."""
    archive = _zip(rom_root / "Game.zip", {"Game.iso": b"iso"})
    extractions: list[Path] = []
    real_extract = ppsspp.extraction_cache._extract_archive

    def counting(rom: Path, dest: Path, timeout: float) -> None:
        """Record an extraction and run it.

        Args:
            rom: The archive.
            dest: Where it goes.
            timeout: The extractor timeout.
        """
        extractions.append(rom)
        real_extract(rom, dest, timeout)

    monkeypatch.setattr(ppsspp.extraction_cache, "_extract_archive", counting)
    emu = ppsspp.Ppsspp()

    first = _launched(emu, emu.resolve_rom_file(archive))
    second = _launched(emu, emu.resolve_rom_file(archive))

    assert first == second
    assert len(extractions) == 1


def test_an_archive_escaping_the_cache_never_launches(rom_root: Path, cache: Path) -> None:
    """A Zip Slip member fails the launch before anything is written or spawned."""
    archive = _zip(rom_root / "Game.zip", {"../../escaped.iso": b"x", "Game.iso": b"iso"})
    emu = ppsspp.Ppsspp()
    emu.stop = lambda: None
    emu._spawn = lambda cmd, env: pytest.fail("ppsspp spawned from a Zip Slip archive")

    with pytest.raises(RuntimeError, match="escapes"):
        emu.launch(emu.resolve_rom_file(archive), None)

    assert not (cache.parent / "escaped.iso").exists()


def test_an_extraction_cut_short_leaves_nothing_to_boot_from(
    rom_root: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure mid-extraction leaves no half-written image for the next launch to trust."""
    archive = _zip(rom_root / "Game.zip", {"Game.iso": b"iso"})

    def dies(rom: Path, dest: Path, timeout: float) -> None:
        """Write part of the image, then fail the way a full disk would.

        Args:
            rom: The archive.
            dest: Where it goes.
            timeout: The extractor timeout.

        Raises:
            RuntimeError: Always.
        """
        (dest / "Game.iso").write_bytes(b"i")
        raise RuntimeError("no space left on device")

    monkeypatch.setattr(ppsspp.extraction_cache, "_extract_archive", dies)
    emu = ppsspp.Ppsspp()
    emu.stop = lambda: None
    emu._spawn = lambda cmd, env: pytest.fail("ppsspp spawned from a failed extraction")

    with pytest.raises(RuntimeError, match="no space"):
        emu.launch(emu.resolve_rom_file(archive), None)

    assert [p.name for p in cache.iterdir() if p.name != ".scratch"] == []
    assert not any((cache / ".scratch").iterdir())


def test_an_image_nested_deeper_than_the_search_is_refused_at_activate(rom_root: Path, cache: Path) -> None:
    """An image the extraction search would never find is refused before anything is extracted."""
    archive = _zip(rom_root / "Game.zip", {"a/b/Game.iso": b"iso"})

    assert ppsspp.Ppsspp().resolve_rom_file(archive) is None


def test_an_image_one_folder_down_is_accepted(rom_root: Path, cache: Path) -> None:
    """The usual wrapper folder is within the search's reach."""
    archive = _zip(rom_root / "Game.zip", {"Game/Game.iso": b"iso"})

    assert ppsspp.Ppsspp().resolve_rom_file(archive) == archive


def test_a_hidden_image_does_not_make_an_archive_bootable(rom_root: Path, cache: Path) -> None:
    """A Mac fork named like an image is not one."""
    archive = _zip(rom_root / "Game.zip", {"__MACOSX/._Game.iso": b"fork"})

    assert ppsspp.Ppsspp().resolve_rom_file(archive) is None


def test_archives_resolve_under_a_symlinked_rom_root(
    tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `/romm` that is itself a symlink, as bind and NFS layouts make it, still boots archives."""
    real = tmp_path / "real-romm"
    link = tmp_path / "romm-link"
    real.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(ppsspp, "ROM_ROOT", link)
    folder = link / "psp" / "Game"
    archive = _zip(folder / "Game.zip", {"Game.iso": b"iso"})

    assert ppsspp.Ppsspp().resolve_rom_file(folder) == archive.resolve()
    assert ppsspp.Ppsspp().resolve_rom_file(archive) == archive


def test_the_cache_is_disabled_by_default() -> None:
    """The cache is disabled by default when PPSSPP_CACHE_ENABLED is unset."""
    env = {k: v for k, v in os.environ.items() if k != "PPSSPP_CACHE_ENABLED"}
    code = "from webstation_broker import settings; print(settings.PPSSPP_CACHE_ENABLED)"
    probe = subprocess.run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip() == "False"
