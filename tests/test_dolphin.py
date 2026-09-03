"""Dolphin ROM resolution, state naming, window selection, and state loading.

Covers picking a bootable image out of a folder, the working-slot state
naming contract, the undo buffer, finding the render window, and confirming
a hotkey load off the access time of the state Dolphin reads back.
"""

import os
import time
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import dolphin


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Dolphin ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(dolphin, "ROM_ROOT", root)
    return root


@pytest.fixture
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Dolphin state directory at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The state directory.
    """
    d = tmp_path / "StateSaves"
    d.mkdir()
    monkeypatch.setattr(dolphin, "STATE_DIR", d)
    return d


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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Game.rvz", 1),
        ("Game (Disc 2).rvz", 2),
        ("Game.disc3.iso", 3),
        ("Game_cd-4.iso", 4),
        # A digit elsewhere in the name is not a disc number.
        ("Sonic Adventure 2.gcm", 1),
        ("Game (Disc 0).iso", 1),
    ],
)
def test_disc_number_reads_only_a_disc_marker(name: str, expected: int) -> None:
    """The disc number comes from an explicit disc marker, not any digit in the name."""
    assert dolphin._disc_number(Path(name)) == expected


def test_rom_pick_prefers_the_compressed_image_beside_the_raw_one(rom_root: Path) -> None:
    """A .rvz beside an .iso of the same game is the one picked."""
    game = rom_root / "game"
    _touch(game / "Game.iso")
    _touch(game / "Game.rvz")

    assert dolphin._pick_rom_file(game.glob("*"), game).name == "Game.rvz"


def test_rom_pick_prefers_the_first_disc(rom_root: Path) -> None:
    """A folder holding several discs resolves to disc one."""
    game = rom_root / "game"
    _touch(game / "Game (Disc 2).iso")
    _touch(game / "Game (Disc 1).iso")

    assert dolphin._pick_rom_file(game.glob("*"), game).name == "Game (Disc 1).iso"


def test_rom_pick_ignores_unbootable_and_hidden_files(rom_root: Path) -> None:
    """Files with the wrong extension or a leading dot are never picked."""
    game = rom_root / "game"
    _touch(game / "readme.txt")
    _touch(game / ".Game.rvz")

    assert dolphin._pick_rom_file(game.glob("*"), game) is None


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """An image symlinked from outside the ROM root is never picked."""
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    game = rom_root / "game"
    game.mkdir()
    (game / "linked.iso").symlink_to(outside)

    assert dolphin._pick_rom_file(game.glob("*"), game) is None


def test_resolve_takes_a_file_as_given(rom_root: Path) -> None:
    """A path that is already a file resolves to itself."""
    rom = _touch(rom_root / "Game.rvz")

    assert dolphin.Dolphin().resolve_rom_file(rom) == rom


def test_resolve_searches_one_level_into_a_folder(rom_root: Path) -> None:
    """A folder is searched one level down for a bootable image."""
    _touch(rom_root / "game" / "inner" / "Game.rvz")

    resolved = dolphin.Dolphin().resolve_rom_file(rom_root / "game")

    assert resolved.name == "Game.rvz"


def test_resolve_gives_up_on_a_path_that_is_not_there(rom_root: Path) -> None:
    """A path that does not exist resolves to None."""
    assert dolphin.Dolphin().resolve_rom_file(rom_root / "gone") is None


def test_resolve_refuses_a_direct_path_that_is_a_symlink_out_of_the_library(
    rom_root: Path, tmp_path: Path
) -> None:
    """A direct path that is a symlink escaping the ROM library resolves to None."""
    outside = tmp_path / "elsewhere.rvz"
    outside.write_bytes(b"rvz")
    linked = rom_root / "Game.rvz"
    linked.symlink_to(outside)

    assert dolphin.Dolphin().resolve_rom_file(linked) is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("GXCE01.s01", "GXCE01.s07"),
        ("GXCE01.s09", "GXCE01.s07"),
        ("Game Name (GXCE01).s02", "Game Name (GXCE01).s07"),
    ],
)
def test_restamp_keeps_the_game_and_rewrites_the_slot(filename: str, expected: str) -> None:
    """Restamping keeps the game id and rewrites only the slot number."""
    assert dolphin._restamp_slot(filename, 7) == expected


@pytest.mark.parametrize(
    "filename",
    ["GXCE01.sav", "GXCE01.s1", "GXCE01.s001", "lastState.sav", "", "a/b.s01"],
)
def test_restamp_refuses_anything_that_is_not_a_state_name(filename: str) -> None:
    """Restamping returns None for a name that is not a Dolphin state name."""
    assert dolphin._restamp_slot(filename, 1) is None


def test_working_slot_reads_the_newest_state_in_it(state_dir: Path) -> None:
    """The working slot resolves to the newest state in that slot, ignoring other slots."""
    _touch(state_dir / "OLD01.s01", mtime=1000)
    newest = _touch(state_dir / "NEW01.s01", mtime=3000)
    _touch(state_dir / "OTHER.s02", mtime=9000)

    assert dolphin._state_for_slot(1) == newest


def test_working_slot_is_empty_when_it_holds_nothing(state_dir: Path) -> None:
    """The working slot resolves to None when only other slots hold states."""
    _touch(state_dir / "OTHER.s02")

    assert dolphin._state_for_slot(1) is None


def test_state_target_names_a_push_for_the_working_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pushed state is targeted at the working slot under its own game id."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)

    target = dolphin.Dolphin().state_target("GXCE01.s05")

    assert target == state_dir / "GXCE01.s01"


def test_state_target_matches_the_state_already_in_the_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push for the game already in the slot targets that state, and another game is refused."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    existing = _touch(state_dir / "GXCE01.s01")

    assert dolphin.Dolphin().state_target("GXCE01.s09") == existing
    # A different game cannot land on top of the state the slot is holding.
    assert dolphin.Dolphin().state_target("RMCE01.s09") is None


@pytest.mark.parametrize("filename", ["../escape.s01", "", ".", "..", "notastate.bin"])
def test_state_target_refuses_a_name_dolphin_would_never_write(state_dir: Path, filename: str) -> None:
    """A push whose name Dolphin would never write is refused."""
    assert dolphin.Dolphin().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the working slot removes its state and keeps the other slots."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    stale = _touch(state_dir / "GXCE01.s01")
    other = _touch(state_dir / "GXCE01.s02")

    dolphin.Dolphin().clear_working_slot()

    assert not stale.exists()
    assert other.exists()


def test_the_undo_buffer_is_dropped_before_the_dump(state_dir: Path) -> None:
    """Dropping the undo buffer removes lastState.sav from the state directory."""
    undo = _touch(state_dir / "lastState.sav")

    dolphin.Dolphin()._drop_undo_buffer()

    assert not undo.exists()


def _disc(path: Path, game_id: bytes, offset: int = 0) -> Path:
    """Write a stub disc image carrying `game_id` at `offset`.

    Args:
        path: The image to create.
        game_id: The six-byte id the disc header opens with.
        offset: Where the header sits, 0x200 for a WBFS file.

    Returns:
        The path that was written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * offset + game_id + b"\0" * 32)
    return path


def test_a_boot_resume_takes_the_state_that_matches_the_disc(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state whose game id matches the image being booted is the one resumed from."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    state = state_dir / "GXCE01.s01"
    state.write_bytes(b"GXCE01" + b"\0" * 16)

    assert dolphin._resume_state(_disc(tmp_path / "Game.iso", b"GXCE01")) == state
    assert dolphin._resume_state(_disc(tmp_path / "Game.wbfs", b"GXCE01", 0x200)) == state


def test_a_boot_resume_refuses_another_games_state(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state carrying another game's id is never handed to Dolphin as a resume."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    (state_dir / "GXCE01.s01").write_bytes(b"GXCE01" + b"\0" * 16)

    assert dolphin._resume_state(_disc(tmp_path / "Other.iso", b"RMCE01")) is None


def test_a_compressed_image_keeps_its_state_on_trust(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A format that hides its game id resumes as before, rather than losing every resume."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    state = state_dir / "GXCE01.s01"
    state.write_bytes(b"GXCE01" + b"\0" * 16)

    assert dolphin._resume_state(_disc(tmp_path / "Game.rvz", b"RVZ\x01\x00\x00")) == state


def test_a_launch_over_another_games_state_boots_without_it(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch whose slot holds another game's state spawns Dolphin with no -s."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    (state_dir / "GXCE01.s01").write_bytes(b"GXCE01" + b"\0" * 16)
    rom = _disc(tmp_path / "Other.iso", b"RMCE01")
    spawned: list[list[str]] = []
    monkeypatch.setattr(dolphin, "_seed_gcpad", lambda: None)
    monkeypatch.setattr(dolphin, "Thread", lambda **kwargs: type("T", (), {"start": lambda s: None})())
    monkeypatch.setattr(dolphin.Dolphin, "_spawn", lambda self, cmd, env: spawned.append(cmd))

    dolphin.Dolphin().launch(rom, 1)

    assert spawned and "-s" not in spawned[0]


def test_a_state_still_open_by_the_emulator_is_not_a_finished_write(state_dir: Path) -> None:
    """A state whose size sits still while dolphin still holds it open never counts as saved."""
    before = dolphin._snapshot()
    with (state_dir / "GXCE01.s01").open("wb") as fh:
        fh.write(b"half a state")
        fh.flush()

        settled = dolphin._wait_for_state_write(before, time.monotonic() + 0.9, os.getpid())

    assert settled is False


def test_a_state_the_emulator_has_closed_counts_as_a_finished_write(state_dir: Path) -> None:
    """A non-empty state with no descriptor left on it settles as saved."""
    before = dolphin._snapshot()
    _touch(state_dir / "GXCE01.s01")

    assert dolphin._wait_for_state_write(before, time.monotonic() + 5.0, os.getpid()) is True


def test_an_empty_state_file_is_never_a_finished_write(state_dir: Path) -> None:
    """A zero-byte state is a write that produced nothing, not a save."""
    before = dolphin._snapshot()
    (state_dir / "GXCE01.s01").write_bytes(b"")

    assert dolphin._wait_for_state_write(before, time.monotonic() + 0.9) is False


def _windowed(emu: dolphin.Dolphin, windows: dict[str, tuple[str, str]], pid: int) -> None:
    """Give `emu` a process handle and an xdotool that reports `windows`.

    Args:
        emu: The emulator to wire up.
        windows: Window id to `(title, owning pid)`.
        pid: The pid the emulator's own process reports.
    """
    emu._proc = type("FakeProc", (), {"pid": pid})()

    def fake_xdotool(*args: str) -> str:
        """Answer a search, a window name or a window pid out of `windows`."""
        if args[0] == "search":
            return "\n".join(windows) + "\n"
        if args[0] == "getwindowname":
            return windows[args[1]][0]
        if args[0] == "getwindowpid":
            return windows[args[1]][1]
        return ""

    emu._xdotool = fake_xdotool


def test_the_render_window_is_the_one_titled_with_the_running_game() -> None:
    """The render window is the Dolphin window whose title names the running game."""
    emu = dolphin.Dolphin()
    _windowed(
        emu,
        {
            "111": ("Dolphin 2606-280", "4242"),
            "222": ("Controller Settings", "4242"),
            "333": ("Dolphin 2606-280 | JIT64 SC | OpenGL | HLE | Custom Robo (GXCE01)", "4242"),
        },
        pid=4242,
    )

    assert emu._render_window() == "333"


def test_no_render_window_when_only_the_main_window_is_up() -> None:
    """No render window is found while only Dolphin's main window is open."""
    emu = dolphin.Dolphin()
    _windowed(emu, {"111": ("Dolphin 2606-280", "4242")}, pid=4242)

    assert emu._render_window() is None


def test_a_window_left_by_the_previous_process_is_not_the_render_window() -> None:
    """A render-titled window belonging to an older emulator process is passed over."""
    emu = dolphin.Dolphin()
    _windowed(
        emu,
        {
            "111": ("Dolphin 2606-280 | JIT64 SC | OpenGL | HLE | Custom Robo (GXCE01)", "1111"),
            "222": ("Dolphin 2606-280 | JIT64 SC | OpenGL | HLE | Mario Kart (GM4E01)", "4242"),
        },
        pid=4242,
    )

    assert emu._render_window() == "222"


def test_no_render_window_without_a_process_of_our_own() -> None:
    """With no process handle there is no window to send a hotkey at."""
    emu = dolphin.Dolphin()
    emu._xdotool = lambda *args: pytest.fail("xdotool run with no emulator process")

    assert emu._render_window() is None


def test_load_state_refuses_an_empty_slot(state_dir: Path) -> None:
    """Loading an empty slot returns False without sending a hotkey."""
    emu = dolphin.Dolphin()
    emu._send_key = lambda key: pytest.fail("hotkey sent at an empty slot")

    assert emu.load_state(1) is False


def test_backdating_puts_the_access_time_behind_the_mtime(state_dir: Path) -> None:
    """A state's access time is stamped behind its own mtime, which is left alone."""
    state = _touch(state_dir / "GXCE01.s01", mtime=5000)

    marker = dolphin._backdate_atime(state)

    assert marker == 5000 - dolphin._ATIME_BACKDATE
    st = state.stat()
    assert st.st_atime == marker
    assert st.st_mtime == 5000


def test_backdating_a_state_that_is_not_there_reports_no_marker(state_dir: Path) -> None:
    """A state that vanished before the load leaves no marker to watch."""
    assert dolphin._backdate_atime(state_dir / "gone.s01") is None


def test_the_access_time_probe_measures_the_filesystem_and_cleans_up(tmp_path: Path) -> None:
    """The probe agrees with what a read actually does to an access time, and leaves nothing."""
    probe = tmp_path / "probe"
    probe.write_bytes(b"x")
    marker = probe.stat().st_mtime - dolphin._ATIME_BACKDATE
    os.utime(probe, (marker, probe.stat().st_mtime))
    probe.read_bytes()
    if probe.stat().st_atime <= marker:
        pytest.skip("the test filesystem does not record access times")
    probe.unlink()

    assert dolphin._atime_tracked(tmp_path) is True
    assert list(tmp_path.iterdir()) == []


def test_the_access_time_probe_fails_closed_on_a_directory_that_is_not_there(
    tmp_path: Path,
) -> None:
    """A state directory the probe cannot write to reports no access-time tracking."""
    assert dolphin._atime_tracked(tmp_path / "gone") is False


def test_a_read_of_the_state_confirms_the_load(state_dir: Path) -> None:
    """The load is confirmed once something moves the state's access time past the marker."""
    state = _touch(state_dir / "GXCE01.s01", mtime=5000)
    marker = dolphin._backdate_atime(state)
    os.utime(state, (5000, 5000))

    assert dolphin._wait_for_state_read(state, marker, time.monotonic() + 0.5) is True


def test_a_state_nothing_ever_read_is_not_a_load(state_dir: Path) -> None:
    """An access time that never moves means the hotkey never reached the core."""
    state = _touch(state_dir / "GXCE01.s01", mtime=5000)
    marker = dolphin._backdate_atime(state)

    assert dolphin._wait_for_state_read(state, marker, time.monotonic() + 0.3) is False


def _loadable(
    monkeypatch: pytest.MonkeyPatch, state_dir: Path, reads: bool, tracked: bool = True
) -> dolphin.Dolphin:
    """Build an emulator whose load hotkey optionally reads the state back.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        state_dir: The state directory holding the working slot.
        reads: Whether the hotkey moves the state's access time, as a real load would.
        tracked: What the access-time probe reports for the state directory.

    Returns:
        The emulator, with a state already in the working slot.
    """
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    monkeypatch.setattr(dolphin, "LOAD_WAIT", 0.5)
    monkeypatch.setattr(dolphin, "LOAD_SETTLE", 0.0)
    monkeypatch.setattr(dolphin, "_atime_tracked", lambda d: tracked)
    state = _touch(state_dir / "GXCE01.s01", mtime=5000)
    emu = dolphin.Dolphin()

    def fake_send(key: str) -> bool:
        """Send the load hotkey, reading the state back when the emulator would."""
        assert key == dolphin.LOAD_KEY
        if reads:
            os.utime(state, (time.time(), 5000))
        return True

    emu._send_key = fake_send
    return emu


def test_load_state_waits_for_dolphin_to_read_the_state_back(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load whose state was read back reports success."""
    emu = _loadable(monkeypatch, state_dir, reads=True)

    assert emu.load_state(1) is True


def test_a_dropped_load_hotkey_is_not_reported_as_a_load(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hotkey Dolphin dropped before the core was running never reads the state, so it fails."""
    emu = _loadable(monkeypatch, state_dir, reads=False)

    assert emu.load_state(1) is False


def test_a_load_hotkey_that_could_not_be_sent_fails(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load with no render window to send the hotkey at fails without waiting it out."""
    emu = _loadable(monkeypatch, state_dir, reads=False)
    emu._send_key = lambda key: False

    assert emu.load_state(1) is False


def test_a_load_is_taken_on_trust_where_access_times_are_not_recorded(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a noatime mount the read cannot be seen, so a sent hotkey is not called a failure."""
    emu = _loadable(monkeypatch, state_dir, reads=False, tracked=False)

    assert emu.load_state(1) is True


def test_memory_card_is_gamecube_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GC has a physical card, Wii saves live in NAND and have none."""
    monkeypatch.setattr(dolphin, "USER_DIR", tmp_path)
    emu = dolphin.Dolphin()
    assert emu.memory_card_path(platform="ngc") == tmp_path / "GC"
    assert emu.memory_card_path(platform="wii") is None
    assert emu.memory_card_path() is None


def test_only_a_gamecube_session_takes_the_card_out_of_the_save_archive() -> None:
    """The card subtree follows the card path: named for GC, absent for Wii and for no platform."""
    emu = dolphin.Dolphin()
    assert emu.memory_card_subtree is None

    emu.platform = "wii"
    # Nothing carries GC on a Wii session, so it has to stay in the archive.
    assert emu.memory_card_subtree is None

    emu.platform = "ngc"
    assert emu.memory_card_subtree == "GC"
    assert emu.memory_card_subtree in emu.save_subtrees


def test_exit_reports_the_working_slot_without_a_running_emulator(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit with no emulator running reports the working slot and drops the undo buffer."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    undo = _touch(state_dir / "lastState.sav")

    report = dolphin.Dolphin().save_and_exit(4)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not undo.exists()
