"""PPSSPP ROM resolution, state naming, and window selection.

Covers picking a bootable image out of a folder, the working-slot state
naming contract, and finding the game window among PPSSPP's windows.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import ppsspp


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


@pytest.mark.parametrize("filename", ["../escape_1.ppst", "", ".", "..", "notastate.bin"])
def test_state_target_refuses_a_name_ppsspp_would_never_write(state_dir: Path, filename: str) -> None:
    """A push whose name PPSSPP would never write is refused."""
    assert ppsspp.Ppsspp().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the working slot removes its state and thumbnail and keeps the other slots."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    stale = _touch(state_dir / "ULUS10041_1_1.ppst")
    stale_shot = _touch(state_dir / "ULUS10041_1_1.jpg")
    other = _touch(state_dir / "ULUS10041_1_2.ppst")

    ppsspp.Ppsspp().clear_working_slot()

    assert not stale.exists()
    assert not stale_shot.exists()
    assert other.exists()


def test_clearing_the_slot_removes_a_staging_file_a_killed_session_left(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging file left by a session killed mid-save must not ship to RomM as a state."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    staged = _touch(state_dir / ("ULUS10041_1_1.ppst" + ppsspp._STAGING_SUFFIX))
    other_staged = _touch(state_dir / ("ULUS10041_1_2.ppst" + ppsspp._STAGING_SUFFIX))

    ppsspp.Ppsspp().clear_working_slot()

    assert not staged.exists()
    assert other_staged.exists()


def test_clearing_the_slot_removes_a_screenshot_whose_state_is_already_gone(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orphaned thumbnail is swept by name, not only alongside the state it belonged to."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    orphan = _touch(state_dir / "ULUS10041_1_1.jpg")
    other = _touch(state_dir / "ULUS10041_1_2.jpg")

    ppsspp.Ppsspp().clear_working_slot()

    assert not orphan.exists()
    assert other.exists()


def test_state_screenshot_path_matches_the_working_state(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screenshot path is the .jpg beside the working slot's state."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    _touch(state_dir / "ULUS10041_1_1.ppst")
    shot = _touch(state_dir / "ULUS10041_1_1.jpg")

    assert ppsspp.Ppsspp().state_screenshot_path() == shot


def test_state_screenshot_path_is_none_without_a_thumbnail(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screenshot path is None when the working state has no .jpg beside it."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    _touch(state_dir / "ULUS10041_1_1.ppst")

    assert ppsspp.Ppsspp().state_screenshot_path() is None


def test_a_players_own_state_bindings_survive_the_launch_patch(
    config_inis: tuple[Path, Path],
) -> None:
    """The broker's bracket keys join the player's mapping for those actions instead of replacing it."""
    _ini, controls = config_inis
    controls.write_text(
        "﻿[ControlMapping]\nSave State = 10-190\nLoad State = 10-191\nRewind = 10-192\n",
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
    controls.write_text("﻿[ControlMapping]\nSave State = 10-190\n", encoding="utf-8")

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
    assert raw.startswith("﻿[ControlMapping]")
    assert "Save State = 1-71" in raw
    assert "Load State = 1-72" in raw


def test_a_controls_file_without_the_state_actions_gains_them(
    config_inis: tuple[Path, Path],
) -> None:
    """An action the file never mentions is added under its section."""
    _ini, controls = config_inis
    controls.write_text("﻿[ControlMapping]\nRewind = 10-192\n", encoding="utf-8")

    ppsspp._patch_config()

    body = controls.read_text(encoding="utf-8-sig")
    assert "Save State = 1-71" in body
    assert "Rewind = 10-192" in body


def test_the_broker_owned_settings_are_still_written_over(config_inis: tuple[Path, Path]) -> None:
    """ppsspp.ini settings the broker owns are replaced outright, not merged."""
    ini, _controls = config_inis
    ini.write_text("﻿[General]\nFirstRun = True\nStateSlot = 4\n", encoding="utf-8")

    ppsspp._patch_config()

    body = ini.read_text(encoding="utf-8-sig")
    assert "FirstRun = False" in body
    assert f"StateSlot = {ppsspp.STATE_SLOT}" in body
    assert "StateSlot = 4" not in body


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


class _WritingClock(_FakeClock):
    """A fake clock that grows a file as it advances, standing in for a write still in flight."""

    def __init__(self, path: Path, done_at: float) -> None:
        """Grow `path` by 100 bytes per fake second until `done_at`, then hold it still.

        Args:
            path: The file the imaginary writer is filling.
            done_at: Fake time the write finishes at.
        """
        super().__init__()
        self.path = path
        self.done_at = done_at

    def sleep(self, seconds: float) -> None:
        """Advance the fake clock and write however much has been produced by then."""
        super().sleep(seconds)
        self.path.write_bytes(b"x" * int(min(self.now, self.done_at) * 100))


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


def test_exit_reports_the_working_slot_without_a_running_emulator(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit with no emulator running reports the working slot and no saved state."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)

    report = ppsspp.Ppsspp().save_and_exit(4)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


def test_a_screenshot_still_being_written_is_waited_out(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save reported done while the thumbnail is mid-write serves RomM a torn image."""
    state = _touch(state_dir / "ULUS10041_1_1.ppst")
    clock = _WritingClock(state_dir / "ULUS10041_1_1.jpg", done_at=1.0)
    monkeypatch.setattr(ppsspp, "time", clock)

    with caplog.at_level("WARNING"):
        ppsspp._wait_for_screenshot(state, 5.0)

    assert clock.now >= 1.0 + ppsspp.STATE_SHOT_STABLE
    assert "never settled" not in caplog.text


def test_a_screenshot_that_never_lands_does_not_fail_the_save(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The state is already confirmed by then, so a missing preview is logged and nothing more."""
    clock = _FakeClock()
    monkeypatch.setattr(ppsspp, "time", clock)
    state = _touch(state_dir / "ULUS10041_1_1.ppst")

    with caplog.at_level("WARNING"):
        ppsspp._wait_for_screenshot(state, 3.0)

    assert "never settled" in caplog.text


def test_a_save_is_not_reported_done_until_its_screenshot_is_waited_on(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The save route answering is what sends RomM to fetch the thumbnail, so the wait belongs here."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    state = _touch(state_dir / "ULUS10041_1_1.ppst")
    waited: list[Path] = []
    monkeypatch.setattr(ppsspp, "_wait_for_state_write", lambda before, deadline: True)
    monkeypatch.setattr(ppsspp, "_wait_for_screenshot", lambda s, d: waited.append(s))
    emu = ppsspp.Ppsspp()
    emu._send_key = lambda key: True

    assert emu.save_state(4) is True
    assert waited == [state]


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
