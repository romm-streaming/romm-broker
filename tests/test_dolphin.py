"""Dolphin ROM resolution, state naming, window selection, and state loading.

Covers picking a bootable image out of a folder, the working-slot state
naming contract, the undo buffer, finding the render window, and confirming
a hotkey load off the access time of the state Dolphin reads back.
"""

import io
import os
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional, Union

import pytest

from webstation_broker import imports, memcard, saves
from webstation_broker.emulators import dolphin

from .conftest import import_zip, preflight_import, restore_import


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
    # The save subtrees hang off the user directory, which the class resolves
    # once at import, so the clear would reach outside tmp_path without this.
    monkeypatch.setattr(dolphin.Dolphin, "save_root", tmp_path)
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


@pytest.mark.parametrize(
    "filename",
    [
        "../escape.s01",
        "",
        ".",
        "..",
        "notastate.bin",
        "GZLE01.s\u0660\u0661",
        "GZLE01.s01\n",
        " .s01",
        ".GZLE01.s01",
    ],
)
def test_state_target_refuses_a_name_dolphin_would_never_write(state_dir: Path, filename: str) -> None:
    """A push whose name Dolphin would never write is refused.

    Args:
        state_dir: The patched state directory.
        filename: The pushed name.
    """
    assert dolphin.Dolphin().state_target(filename) is None


def test_clearing_the_slot_takes_every_state_not_just_the_broker_slot(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state in any slot is the last session's, and nothing in its name says so."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    stale = _touch(state_dir / "GXCE01.s01")
    other = _touch(state_dir / "GXCE01.s02")
    card = _touch(tmp_path / "GC" / "MemoryCardA.USA.raw")
    nand = _touch(tmp_path / "Wii" / "title" / "00010000" / "data.bin")

    dolphin.Dolphin().clear_working_slot()

    assert not stale.exists()
    assert not other.exists()
    assert not card.exists()
    assert not nand.exists()
    assert state_dir.is_dir()


def test_clearing_the_slot_keeps_a_card_the_memory_route_just_synced(
    state_dir: Path, tmp_path: Path
) -> None:
    """The GameCube card is hydrated before activate, so a clear that took it would drop it."""
    card = _touch(tmp_path / "GC" / "MemoryCardA.USA.raw")
    stale = _touch(state_dir / "GXCE01.s02")

    dolphin.Dolphin().clear_working_slot(("GC",))

    assert card.exists()
    assert not stale.exists()


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


@pytest.mark.parametrize(
    ("data", "offset", "expected"),
    [
        (b"GZLE01xxxx", 0, "GZLE01"),
        (b"\x00" * 0x200 + b"RMCE01", 0x200, "RMCE01"),
        (b"\xff" * 6, 0, None),
        (b"GZLE0", 0, None),
        (b"GZ-E01", 0, None),
        (b"GZLE01", 0x200, None),
    ],
)
def test_game_id_in_reads_six_alphanumerics_or_nothing(
    data: bytes, offset: int, expected: Optional[str]
) -> None:
    """The bytes variant accepts exactly what the file variant does.

    Args:
        data: The bytes to read.
        offset: Where the id starts.
        expected: The id, or None.
    """
    assert dolphin._game_id_in(data, offset) == expected


def test_game_id_at_reads_through_the_bytes_variant(tmp_path: Path) -> None:
    """A file read and a bytes read agree.

    Args:
        tmp_path: The per-test temporary directory.
    """
    disc = tmp_path / "game.iso"
    disc.write_bytes(b"GZLE01" + bytes(64))

    assert dolphin._game_id_at(disc, 0) == dolphin._game_id_in(disc.read_bytes(), 0) == "GZLE01"


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


def test_a_launch_sends_dolphin_to_the_directories_the_broker_uses(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawned emulator resolves the same config and data directories the broker seeds and reads.

    Nothing on the command line names them any more, so this is the whole of
    the agreement: if the exported XDG roots ever stop matching, the pad
    bindings are seeded into a file Dolphin never opens, which is exactly the
    bug that made a rebound controller work on the desktop and nowhere else.
    """
    monkeypatch.setattr(dolphin, "CONFIG_DIR", tmp_path / "cfg" / "dolphin-emu")
    monkeypatch.setattr(dolphin, "USER_DIR", tmp_path / "data" / "dolphin-emu")
    rom = _disc(tmp_path / "Game.iso", b"GXCE01")
    spawned: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(dolphin, "_seed_gcpad", lambda: None)
    monkeypatch.setattr(dolphin, "Thread", lambda **kwargs: type("T", (), {"start": lambda s: None})())
    monkeypatch.setattr(
        dolphin.Dolphin, "_spawn", lambda self, cmd, env: spawned.append((cmd, env))
    )

    dolphin.Dolphin().launch(rom, 1)

    assert spawned
    cmd, env = spawned[0]
    # -u would move the config under the user dir, where the desktop launcher,
    # which passes none, would never read the pad a player just rebound.
    assert "-u" not in cmd
    assert Path(env["XDG_CONFIG_HOME"]) / "dolphin-emu" == dolphin.CONFIG_DIR
    assert Path(env["XDG_DATA_HOME"]) / "dolphin-emu" == dolphin.USER_DIR


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


def test_the_access_time_probe_never_ships_in_a_save_archive(
    state_dir: Path, tmp_path: Path
) -> None:
    """A probe stranded by a kill is left out of the archive the exit dump builds."""
    state = _touch(state_dir / "GXCE01.s01")
    dolphin._atime_probe_path(state_dir).write_bytes(b"probe")

    report = saves.build_save_archive(tmp_path, ("StateSaves",), 0.0)

    assert [f["path"] for f in report["files"]] == [f"StateSaves/{state.name}"]


def test_a_stranded_access_time_probe_is_cleared_before_the_dump(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit sweeps a probe whose own cleanup never ran, and leaves the states alone."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    stranded = dolphin._atime_probe_path(state_dir)
    stranded.write_bytes(b"probe")
    state = _touch(state_dir / "GXCE01.s01")

    dolphin.Dolphin().save_and_exit(None)

    assert not stranded.exists()
    assert state.exists()


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
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
    reads: bool,
    tracked: bool = True,
    writes_undo: bool = False,
) -> dolphin.Dolphin:
    """Build an emulator whose load hotkey optionally reads the state back.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        state_dir: The state directory holding the working slot.
        reads: Whether the hotkey moves the state's access time, as a real load would.
        tracked: What the access-time probe reports for the state directory.
        writes_undo: Whether the hotkey rewrites the undo buffer, as a real load would.

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
        if writes_undo:
            (state_dir / dolphin._UNDO_BUFFER_NAME).write_bytes(b"undo state")
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


def test_a_load_falls_back_to_the_undo_buffer_where_access_times_are_not_recorded(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a noatime mount the undo buffer Dolphin rewrites is what confirms the load."""
    emu = _loadable(monkeypatch, state_dir, reads=False, tracked=False, writes_undo=True)

    assert emu.load_state(1) is True


def test_an_undo_buffer_rewritten_over_an_older_one_still_confirms_the_load(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second load in one session is confirmed by the undo buffer changing, not by it appearing."""
    emu = _loadable(monkeypatch, state_dir, reads=False, tracked=False, writes_undo=True)
    _touch(state_dir / dolphin._UNDO_BUFFER_NAME, mtime=5000)

    assert emu.load_state(1) is True


def test_a_load_nothing_can_confirm_is_reported_as_a_failure(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no access time and no undo buffer rewrite, a load is a failure, not an assumed success."""
    emu = _loadable(monkeypatch, state_dir, reads=False, tracked=False)

    assert emu.load_state(1) is False


def test_an_untouched_undo_buffer_never_confirms_a_load(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The undo buffer a previous load left behind is not taken as this load's confirmation."""
    emu = _loadable(monkeypatch, state_dir, reads=False, tracked=False)
    _touch(state_dir / dolphin._UNDO_BUFFER_NAME, mtime=5000)

    assert emu.load_state(1) is False


def test_a_state_whose_marker_could_not_be_stamped_falls_back_to_the_undo_buffer(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backdate that failed leaves no marker to watch, so the undo buffer stands in for it."""
    emu = _loadable(monkeypatch, state_dir, reads=False, tracked=True, writes_undo=True)
    monkeypatch.setattr(dolphin, "_backdate_atime", lambda p: None)

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


def _card_gci(code: bytes) -> bytes:
    """A one-block GCI whose directory entry opens with `code`.

    Args:
        code: The six-byte game id, e.g. `GZLE01`.

    Returns:
        The GCI's bytes.
    """
    return code + bytes(0x40 - len(code) + 0x2000)


def _arrange(members: dict[str, bytes]) -> memcard.Placement:
    """Run Dolphin's card placement over members the way `memcard.replace` shows them.

    Args:
        members: Member names mapped to their full bytes.

    Returns:
        Where each member goes under `GC`, and which ones Dolphin will not read.
    """
    return dolphin.Dolphin().arrange_card({n: b[: memcard.HEAD_BYTES] for n, b in members.items()})


def _push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: dict[str, bytes]
) -> Union[memcard.Replaced, str]:
    """Push a zip of `members` to Dolphin's card through `memcard.replace`, as the PUT route does.

    Args:
        tmp_path: The pytest temporary directory, standing in for Dolphin's user folder.
        monkeypatch: The pytest monkeypatch fixture.
        members: Member names mapped to their bytes.

    Returns:
        What `memcard.replace` returned.
    """
    monkeypatch.setattr(dolphin, "USER_DIR", tmp_path)
    emu = dolphin.Dolphin()
    card = emu.memory_card_path(platform="ngc")
    assert card is not None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return memcard.replace(card, buf.getvalue(), None, emu.arrange_card)


@pytest.mark.parametrize(
    "members",
    [
        {"SRAM.raw": bytes(64), "USA/Card A/01-GZLE-zelda.gci": _card_gci(b"GZLE01")},
        {"USA/Card A/01-GZLE-zelda.gci": _card_gci(b"GZLE01")},
        {"EUR/Card A/01-GZLP-zelda.gci": _card_gci(b"GZLP01"), "JAP/Card A/01-GZLJ-zelda.gci": b"x"},
    ],
)
def test_a_card_packed_the_way_dolphin_keeps_it_is_left_as_it_is(members: dict[str, bytes]) -> None:
    """The contents of Dolphin's own `GC` folder, SRAM included, land exactly as packed.

    Args:
        members: The pushed card's members.
    """
    assert _arrange(members) == ({n: n for n in members}, ())


@pytest.mark.parametrize(
    ("name", "code", "dest"),
    [
        ("01-GZLE-zelda.gci", b"GZLE01", "USA/Card A/01-GZLE-zelda.gci"),
        ("Card A/01-GZLE-zelda.gci", b"GZLE01", "USA/Card A/01-GZLE-zelda.gci"),
        ("Card A/01-GZLP-zelda.gci", b"GZLP01", "EUR/Card A/01-GZLP-zelda.gci"),
        ("Card A/01-GZLJ-zelda.gci", b"GZLJ01", "JAP/Card A/01-GZLJ-zelda.gci"),
        ("Card A/01-GZLD-zelda.gci", b"GZLD01", "EUR/Card A/01-GZLD-zelda.gci"),
        ("Card A/save.gci", b"GAAB01", "USA/Card A/save.gci"),
        ("Card A/save.gci", b"GAAN01", "USA/Card A/save.gci"),
        ("Card A/save.gci", b"GAAK01", "JAP/Card A/save.gci"),
        ("Card A/save.gci", b"GAAW01", "JAP/Card A/save.gci"),
        ("Card A/save.gci", b"GAAT01", "JAP/Card A/save.gci"),
        ("Card A/save.gci", b"GAAH01", "EUR/Card A/save.gci"),
        ("USA/01-GZLE-zelda.gci", b"GZLE01", "USA/Card A/01-GZLE-zelda.gci"),
        ("USA/Card B/01-GZLE-zelda.gci", b"GZLE01", "USA/Card A/01-GZLE-zelda.gci"),
        ("USA/Card A/sub/01-GZLE-zelda.gci", b"GZLE01", "USA/Card A/01-GZLE-zelda.gci"),
        # Dolphin's folder scan matches the extension case-insensitively, so it reads this one too.
        ("01-GZLE-zelda.GCI", b"GZLE01", "USA/Card A/01-GZLE-zelda.GCI"),
    ],
)
def test_a_misplaced_gci_moves_to_the_card_its_game_code_names(name: str, code: bytes, dest: str) -> None:
    """A loose GCI, or one under a bare or wrong slot folder, goes to the region Dolphin boots its game in.

    Args:
        name: Where the player packed the GCI.
        code: The game id its directory entry opens with.
        dest: Where Dolphin reads it.
    """
    assert _arrange({name: _card_gci(code)}) == ({name: dest}, ())


@pytest.mark.parametrize("wrapper", ["GC/", "saves/dolphin-emu/User/GC/"])
def test_the_gc_folder_packed_by_name_loses_its_wrapper(wrapper: str) -> None:
    """Zipping the `GC` folder itself, not its contents, still lands the card at the card root.

    Args:
        wrapper: The folders the card was packed under.
    """
    members = {
        f"{wrapper}SRAM.raw": bytes(64),
        f"{wrapper}USA/Card A/01-GZLE-zelda.gci": _card_gci(b"GZLE01"),
    }

    placement = _arrange(members)

    assert sorted(placement.dests.values()) == ["SRAM.raw", "USA/Card A/01-GZLE-zelda.gci"]
    assert placement.unread == ()


@pytest.mark.parametrize("code", [b"GZLX01", b"GZLY01", b"GZLZ01", b"GZLA01", b"GZL", b""])
def test_a_gci_whose_region_is_unknown_stays_where_it_was(
    code: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """A code Dolphin settles by the disc, or with no region at all, is left in place and reported.

    Args:
        code: The GCI's opening bytes.
        caplog: The pytest log capture fixture.
    """
    member = code + bytes(0x40 - len(code)) if len(code) == 6 else code

    name = "Card A/save.gci"

    assert _arrange({name: member}) == ({name: name}, (name,))
    assert "names no region" in caplog.text


def test_a_stray_copy_never_displaces_the_gci_already_in_place(caplog: pytest.LogCaptureFixture) -> None:
    """The copy already in `<region>/Card A` keeps its spot; the stray stays put and is reported.

    Args:
        caplog: The pytest log capture fixture.
    """
    members = {
        "01-GZLE-zelda.gci": _card_gci(b"GZLE01"),
        "USA/Card A/01-GZLE-zelda.gci": _card_gci(b"GZLE01"),
    }

    assert _arrange(members) == ({n: n for n in members}, ("01-GZLE-zelda.gci",))
    assert "already taken" in caplog.text


def test_two_strays_of_one_save_move_only_the_first() -> None:
    """Two misplaced copies bound for one file: the first moves, the second stays put."""
    members = {"01-GZLE-zelda.gci": _card_gci(b"GZLE01"), "Card A/01-GZLE-zelda.gci": _card_gci(b"GZLE01")}

    assert _arrange(members) == (
        {
            "01-GZLE-zelda.gci": "USA/Card A/01-GZLE-zelda.gci",
            "Card A/01-GZLE-zelda.gci": "Card A/01-GZLE-zelda.gci",
        },
        ("Card A/01-GZLE-zelda.gci",),
    )


def test_a_wrapper_never_strips_a_member_onto_one_packed_bare() -> None:
    """`GC/SRAM.raw` beside a bare `SRAM.raw` keeps its wrapper rather than overwrite it."""
    members = {"GC/SRAM.raw": bytes(64), "SRAM.raw": bytes(64)}

    assert _arrange(members).dests == {n: n for n in members}


@pytest.mark.parametrize(
    "members",
    [
        {"GC/SRAM.raw": bytes(64), "SRAM.raw": bytes(64)},
        {"GC/USA/Card A/a.gci": _card_gci(b"GAAE01"), "USA/Card A/a.gci": _card_gci(b"GAAE01")},
        {"a.gci": _card_gci(b"GAAE01"), "USA/Card A/a.gci": _card_gci(b"GAAE01"), "GC/a.gci": b"GAAE"},
        {"USA": b"a file", "Card A/a.gci": _card_gci(b"GAAE01")},
        {"USA/Card A": b"a file", "a.gci": _card_gci(b"GAAE01")},
        {"USA/Card A/a.gci/x": b"a file", "a.gci": _card_gci(b"GAAE01")},
        {
            "GC/USA/Card A/a.gci": _card_gci(b"GAAE01"),
            "Card A/a.gci": _card_gci(b"GAAE01"),
            "a.gci": b"GAAE",
        },
    ],
)
def test_no_packing_makes_two_members_collide_and_refuse_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: dict[str, bytes]
) -> None:
    """However a card is packed, tidying it never sends two members onto one path.

    A refused push makes RomM abort its claim, so a card that wrote fine
    before the tidying has to write fine after it.

    Args:
        tmp_path: The pytest temporary directory.
        monkeypatch: The pytest monkeypatch fixture.
        members: The pushed card's members.
    """
    result = _push(tmp_path, monkeypatch, members)

    assert isinstance(result, memcard.Replaced), result
    assert result.written == len(members)


def test_a_member_naming_no_file_does_not_crash_the_hook() -> None:
    """A member whose path normalises to nothing is handed back as is, for `memcard.replace` to refuse."""
    assert _arrange({".": b""}).dests == {".": "."}


@pytest.mark.parametrize("name", ["MemoryCardA.USA.raw", "card.mcd", "card.gcp", "GC/MemoryCardA.EUR.raw"])
def test_a_whole_card_image_is_kept_but_reported(name: str, caplog: pytest.LogCaptureFixture) -> None:
    """A card image stays in the card as before, reported and logged as one the folder card never reads.

    Args:
        name: The image's name in the pushed card.
        caplog: The pytest log capture fixture.
    """
    assert _arrange({name: bytes(0x40)}) == ({name: PurePosixPath(name).name}, (name,))
    assert "Memory Card Manager" in caplog.text


def test_sram_is_never_mistaken_for_a_card_image(caplog: pytest.LogCaptureFixture) -> None:
    """`SRAM.raw` is Dolphin's own settings file, so it is not reported as an unreadable card.

    Args:
        caplog: The pytest log capture fixture.
    """
    assert _arrange({"SRAM.raw": bytes(64)}) == ({"SRAM.raw": "SRAM.raw"}, ())
    assert "Memory Card Manager" not in caplog.text


def test_a_pushed_card_lays_its_saves_where_dolphin_reads_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through `memcard.replace`: a zipped bare `Card A` lands in `USA/Card A`.

    Args:
        tmp_path: The pytest temporary directory.
        monkeypatch: The pytest monkeypatch fixture.
    """
    gci = _card_gci(b"GZLE01")

    result = _push(tmp_path, monkeypatch, {"Card A/01-GZLE-zelda.gci": gci})

    assert result == memcard.Replaced(1, ())
    assert (tmp_path / "GC" / "USA" / "Card A" / "01-GZLE-zelda.gci").read_bytes() == gci
    assert not (tmp_path / "GC" / "Card A").exists()


def test_exit_reports_the_working_slot_without_a_running_emulator(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit with no emulator running reports the working slot and drops the undo buffer."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    undo = _touch(state_dir / "lastState.sav")

    report = dolphin.Dolphin().save_and_exit(4)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not undo.exists()


def test_seed_gcpad_binds_all_four_pads_to_the_configured_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each seeded GCPad section points at the SDL index for the configured pad name."""
    config_dir = tmp_path / "Config"
    monkeypatch.setattr(dolphin, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(dolphin, "_PAD_NAME", "Xbox 360 Controller")

    dolphin._seed_gcpad()

    ini = (config_dir / "GCPadNew.ini").read_text()
    for i in range(4):
        assert f"[GCPad{i + 1}]\nDevice = SDL/{i}/Xbox 360 Controller" in ini


def test_seed_gcpad_does_not_overwrite_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file already on disk, such as a player's own remapping, is left alone."""
    config_dir = tmp_path / "Config"
    config_dir.mkdir()
    monkeypatch.setattr(dolphin, "CONFIG_DIR", config_dir)
    path = config_dir / "GCPadNew.ini"
    path.write_text("custom")

    dolphin._seed_gcpad()

    assert path.read_text() == "custom"


def test_seed_gcpad_repoints_pads_bound_to_a_name_sdl_never_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container seeded with the old kernel-side pad name has its bindings repaired in place."""
    config_dir = tmp_path / "Config"
    config_dir.mkdir()
    monkeypatch.setattr(dolphin, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(dolphin, "_PAD_NAME", "Xbox 360 Controller")
    path = config_dir / "GCPadNew.ini"
    path.write_text(
        "[GCPad1]\nDevice = SDL/0/Microsoft X-Box 360 pad\nButtons/A = `Button E`\n"
        "[GCPad2]\nDevice = SDL/1/Microsoft X-Box 360 pad\n"
    )

    dolphin._seed_gcpad()

    healed = path.read_text()
    assert "Device = SDL/0/Xbox 360 Controller" in healed
    assert "Device = SDL/1/Xbox 360 Controller" in healed
    # Only the device line is the broker's business; the mapping is the player's.
    assert "Buttons/A = `Button E`" in healed


def test_seed_gcpad_leaves_a_device_the_player_chose_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pad rebound to any other device, on any backend, survives the repair pass."""
    config_dir = tmp_path / "Config"
    config_dir.mkdir()
    monkeypatch.setattr(dolphin, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(dolphin, "_PAD_NAME", "Xbox 360 Controller")
    path = config_dir / "GCPadNew.ini"
    chosen = (
        "[GCPad1]\nDevice = evdev/0/Microsoft X-Box 360 pad\n"
        "[GCPad2]\nDevice = SDL/1/8BitDo Pro 2\n"
    )
    path.write_text(chosen)

    dolphin._seed_gcpad()

    assert path.read_text() == chosen


# -- declared imports --

_ROMM_ID = imports.RomRef(1, "Game", "ngc", title_id="GZLE01")
"""A GameCube activate's rom, carrying the game id RomM holds for it."""

_WII_ID = imports.RomRef(1, "Game", "wii", title_id="RMCE01")
"""A Wii activate's rom, carrying the game id RomM holds for it."""

_GCI = b"GZLE01" + bytes(0x40 - 6 + 0x2000)
"""A one-block GCI for `GZLE01`: its 64-byte directory entry, then one 8 KiB block."""


def _preflight(
    members: dict[str, bytes],
    *,
    platform: str = "ngc",
    rom_file: Optional[Path] = None,
    v1: Optional[dict[str, bytes]] = None,
    **kwargs: Any,
) -> imports.PreflightResult:
    """Preflight an archive of import members on a fresh Dolphin, resuming slot 1.

    Args:
        members: `.import/<kind>/...` names mapped to bytes.
        platform: The RomM platform slug the session loads.
        rom_file: The disc about to boot, or None.
        v1: Ordinary archive members to carry beside them, or None.
        **kwargs: Extra `preflight_import` arguments, such as `rom`.

    Returns:
        What preflight decided.
    """
    emu = dolphin.Dolphin()
    emu.platform = platform
    kwargs.setdefault("resume_slot", 1)
    return preflight_import(emu, import_zip(members, v1), rom_file=rom_file, **kwargs)


@pytest.mark.parametrize("member", [".import/state/mystate.s04", ".import/state/StateSaves/GZLE01.s04"])
def test_a_state_lands_in_the_working_slot_named_for_its_header(state_dir: Path, member: str) -> None:
    """Dolphin finds a state by the running disc's id, so the header names it, not the member's name.

    Args:
        state_dir: The patched state directory.
        member: The member's zip name.
    """
    result = _preflight({member: b"GZLE01progress"}, rom=_ROMM_ID)

    assert result.refusals == ()
    assert [p.dest for p in result.placements] == [
        PurePosixPath(f"StateSaves/GZLE01.s{dolphin.STATE_SLOT:02d}")
    ]


def test_a_state_that_does_not_open_with_a_game_id_is_refused(state_dir: Path) -> None:
    """A file that does not open with a game id is not a Dolphin state.

    Args:
        state_dir: The patched state directory.
    """
    result = _preflight({".import/state/GZLE01.s01": bytes(64)}, rom=_ROMM_ID)

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("unrecognised_layout", "the state does not open with a game id")
    ]


def test_a_state_for_another_game_is_refused(state_dir: Path) -> None:
    """A state only loads into the game that wrote it, whatever its name says.

    Args:
        state_dir: The patched state directory.
    """
    result = _preflight({".import/state/GZLE01.s01": b"GALE01progress"}, rom=_ROMM_ID)

    assert [r.reason for r in result.refusals] == ["identity_mismatch"]


def test_a_state_is_held_to_the_roms_full_id(state_dir: Path, tmp_path: Path) -> None:
    """A state whose maker code differs from the rom's would be refused at boot, so it is refused here.

    The session identity keeps only the game code, `GZLE`, which both share.

    Args:
        state_dir: The patched state directory.
        tmp_path: The per-test temporary directory.
    """
    rom = _disc(tmp_path / "roms" / "game.iso", b"GZLE8P")

    result = _preflight({".import/state/GZLE01.s01": b"GZLE01progress"}, rom_file=rom)

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("identity_mismatch", "member GZLE01, rom GZLE8P")
    ]


@pytest.mark.parametrize("member", [".import/state/lastState.sav", ".import/state/StateSaves/lastState.sav"])
def test_the_undo_buffer_is_refused_as_emulator_configuration(state_dir: Path, member: str) -> None:
    """Dolphin rewrites `lastState.sav` on every load, so it is never a state to resume.

    Args:
        state_dir: The patched state directory.
        member: The member's zip name.
    """
    result = _preflight({member: b"GZLE01progress"})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("protected_destination", "StateSaves/lastState.sav is emulator configuration")
    ]


@pytest.mark.parametrize(
    "member",
    [".import/state/Game.state1", ".import/state/Game.state.auto", ".import/state/StateSaves/Game.state3"],
)
def test_a_libretro_state_is_refused_as_another_emulators(state_dir: Path, member: str) -> None:
    """A RetroArch numbered or auto state is another emulator's format, which Dolphin cannot load.

    Args:
        state_dir: The patched state directory.
        member: The member's zip name.
    """
    result = _preflight({member: b"GZLE01progress"}, rom=_ROMM_ID)

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("source_incompatible", "a RetroArch (libretro) state")
    ]


def test_an_empty_state_is_incomplete(state_dir: Path) -> None:
    """A zero-byte state would boot the game from scratch without a word.

    Args:
        state_dir: The patched state directory.
    """
    result = _preflight({".import/state/GZLE01.s01": b""})

    assert [(r.reason, r.detail) for r in result.refusals] == [("incomplete_unit", "the file is empty")]


@pytest.mark.parametrize("kind", ["save", "memcard"])
@pytest.mark.parametrize("prefix", ["", "GC/", "saves/dolphin-emu/User/GC/"])
def test_a_gci_lands_in_its_regions_card_however_deep_it_was_packed(
    state_dir: Path, kind: str, prefix: str
) -> None:
    """A GCI is taken as a save or a card, below any wrapper a copy of the user folder leaves.

    Args:
        state_dir: The patched state directory.
        kind: The declared kind.
        prefix: The folders the GCI was packed under.
    """
    result = _preflight({f".import/{kind}/{prefix}USA/Card A/01-GZLE-zelda.gci": _GCI}, rom=_ROMM_ID)

    assert result.refusals == ()
    assert [p.dest for p in result.placements] == [PurePosixPath("GC/USA/Card A/01-GZLE-zelda.gci")]


def test_another_games_gci_is_allowed(state_dir: Path) -> None:
    """A game can read another game's save, so a GCI's game code is only logged, never refused.

    Args:
        state_dir: The patched state directory.
    """
    melee = b"GALE01" + bytes(0x40 - 6 + 0x2000)

    result = _preflight({".import/save/USA/Card A/01-GALE-melee.gci": melee}, rom=_ROMM_ID)

    assert result.refusals == ()


@pytest.mark.parametrize(
    "member", [".import/save/01-GZLE-zelda.gci", ".import/save/USA/Card B/01-GZLE-zelda.gci"]
)
def test_a_gci_outside_a_regions_card_a_is_refused(state_dir: Path, member: str) -> None:
    """Slot A is pinned to a folder card, which reads GCIs from `<region>/Card A` only.

    Args:
        state_dir: The patched state directory.
        member: The member's zip name.
    """
    result = _preflight({member: _GCI})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("unrecognised_layout", "a GCI goes in <USA|EUR|JAP>/Card A/")
    ]


@pytest.mark.parametrize("data", [_GCI[:-1], _GCI[:0x40]])
def test_a_gci_that_is_not_whole_blocks_is_refused(state_dir: Path, data: bytes) -> None:
    """A GCI is its 64-byte directory entry followed by at least one whole 8 KiB block.

    Args:
        state_dir: The patched state directory.
        data: The member's bytes.
    """
    result = _preflight({".import/save/USA/Card A/01-GZLE-zelda.gci": data})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("unrecognised_layout", "not a 64-byte header followed by whole 8 KiB blocks")
    ]


@pytest.mark.parametrize("name", ["card.raw", "card.mcd", "card.gcp"])
def test_a_card_image_needs_its_saves_exported(state_dir: Path, name: str) -> None:
    """A folder card cannot take a whole card image; each save has to be exported from it as a GCI.

    Args:
        state_dir: The patched state directory.
        name: The image's file name.
    """
    result = _preflight({f".import/memcard/{name}": bytes(0x40)})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("needs_conversion", "a memory card image; export each save from it as a .gci")
    ]


@pytest.mark.parametrize("prefix", ["", "Wii/", "saves/dolphin-emu/User/Wii/"])
def test_a_wii_save_lands_in_the_nand_with_lower_case_ids(state_dir: Path, prefix: str) -> None:
    """A title's save goes under `Wii/title`, with its id folders lower-cased, as Dolphin writes them.

    Args:
        state_dir: The patched state directory.
        prefix: The folders the title was packed under.
    """
    member = f".import/save/{prefix}title/00010000/524D4345/data/banner.bin"

    result = _preflight({member: b"banner"}, platform="wii", rom=_WII_ID)

    assert result.refusals == ()
    assert [p.dest for p in result.placements] == [
        PurePosixPath("Wii/title/00010000/524d4345/data/banner.bin")
    ]


def test_a_wii_save_for_another_title_is_refused(state_dir: Path) -> None:
    """A Wii title's save is held to the session's game.

    Args:
        state_dir: The patched state directory.
    """
    result = _preflight(
        {".import/save/title/00010000/534D4E45/data/banner.bin": b"banner"}, platform="wii", rom=_WII_ID
    )

    assert [r.reason for r in result.refusals] == ["identity_mismatch"]


@pytest.mark.parametrize(
    "rel",
    [
        "sys/uid.sys",
        "ticket/00010000/524d4345.tik",
        "title/00000001/00000002/data/setting.txt",
        "title/00010000/524d4345/content/title.tmd",
    ],
)
def test_wii_system_and_install_data_is_refused_as_emulator_configuration(state_dir: Path, rel: str) -> None:
    """The NAND's system files, tickets, system titles and installed content are no player's save.

    Args:
        state_dir: The patched state directory.
        rel: The member's path below `.import/save/`.
    """
    result = _preflight({f".import/save/{rel}": b"x"}, platform="wii", rom=_WII_ID)

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("protected_destination", f"Wii/{rel} is emulator configuration")
    ]


@pytest.mark.parametrize(
    ("rel", "reason", "detail"),
    [
        (
            "data.bin",
            "needs_conversion",
            "a Wii SD-card export; import it with Dolphin's Import Wii Save, then send the title folder",
        ),
        (
            "private/wii/title/RMCE/data.bin",
            "needs_conversion",
            "a Wii SD-card export; import it with Dolphin's Import Wii Save, then send the title folder",
        ),
        ("nand.bin", "source_incompatible", "a whole NAND dump; send the title folder from it instead"),
        (
            "USA/Card A/01-GZLE-zelda.gci",
            "unrecognised_layout",
            "a GameCube save; a Wii session takes NAND title saves",
        ),
        ("mysaves/banner.bin", "unrecognised_layout", None),
        ("shared1/00000000.app", "unrecognised_layout", None),
        ("shared2/sys/SYSCONF", "unrecognised_layout", None),
        ("meta/00010000/524d4345/title.met", "unrecognised_layout", None),
        ("Wii/shared2/sys/SYSCONF", "unrecognised_layout", None),
        ("title/00010000/524d4345/banner.bin", "unrecognised_layout", None),
    ],
)
def test_a_wii_member_that_is_not_a_title_save_is_refused(
    state_dir: Path, rel: str, reason: str, detail: Optional[str]
) -> None:
    """Each wrong shape is refused with the reason that tells the player what to do.

    Args:
        state_dir: The patched state directory.
        rel: The member's path below `.import/save/`.
        reason: The refusal code.
        detail: The refusal's detail.
    """
    result = _preflight({f".import/save/{rel}": b"x"}, platform="wii", rom=_WII_ID)

    assert [(r.reason, r.detail) for r in result.refusals] == [(reason, detail)]


def test_an_imported_gci_beside_an_archived_card_is_refused(state_dir: Path) -> None:
    """An archive carries one GameCube card: its own card members, or imported GCIs, never both.

    Args:
        state_dir: The patched state directory.
    """
    melee = b"GALE01" + bytes(0x40 - 6 + 0x2000)

    result = _preflight(
        {".import/save/USA/Card A/01-GZLE-zelda.gci": _GCI}, v1={"GC/USA/Card A/01-GALE-melee.gci": melee}
    )

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("destination_conflict", "the archive already carries a GC card")
    ]


def test_a_gci_is_refused_while_the_card_syncs_on_its_own_routes(state_dir: Path) -> None:
    """With the card synced separately, the restore leaves `GC` alone, so nothing may be imported into it.

    Args:
        state_dir: The patched state directory.
    """
    result = _preflight(
        {".import/save/USA/Card A/01-GZLE-zelda.gci": _GCI}, memory_card_synced=True, excluded=("GC",)
    )

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("memcard_synced_separately", "the card travels on its own routes this session")
    ]


@pytest.mark.parametrize(
    ("head", "accepted"),
    [(b"GZLE01" + bytes(58), True), (bytes(64), True), (b"GALE01" + bytes(58), False)],
)
def test_a_pushed_state_is_held_to_the_session_by_its_header(head: bytes, accepted: bool) -> None:
    """The push route refuses a state whose header names another game, and trusts one that names none.

    Args:
        head: The pushed state's first bytes.
        accepted: Whether the push may be written.
    """
    emu = dolphin.Dolphin()
    emu.import_identity = imports.SessionIdentity(imports.NORMALISERS["gc_wii_disc"]("GZLE01"), "romm")

    assert emu.check_state_bytes(head) is accepted


@pytest.mark.parametrize(
    ("source", "hint"),
    [("romm", " - fix via PUT /api/roms/{id}/identity if RomM is wrong"), ("rom", "")],
)
def test_a_push_refused_by_its_header_logs_both_ids(
    source: Literal["rom", "romm"], hint: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The log line names the header's game and the session's, with the override only when RomM gave the id.

    Args:
        source: Where the session's id came from.
        hint: The override hint the line should end with.
        caplog: The pytest log capture fixture.
    """
    emu = dolphin.Dolphin()
    session_id = imports.NORMALISERS["gc_wii_disc"]("GZLE01")
    member_id = imports.NORMALISERS["gc_wii_disc"]("GALE01")
    emu.import_identity = imports.SessionIdentity(session_id, source)

    with caplog.at_level("INFO"):
        assert emu.check_state_bytes(b"GALE01" + bytes(58)) is False

    assert (
        f"dolphin: pushed state's header names another game: member {member_id},"
        f" session {session_id} (from {source}){hint}"
    ) in caplog.text


@pytest.mark.parametrize(
    ("platform", "kinds", "card"),
    [
        ("ngc", ["save", "memcard", "state"], "GC"),
        ("wii", ["save", "state"], None),
        (None, [], None),
        ("wiiu", [], None),
    ],
)
def test_the_platform_picks_what_dolphin_takes(
    platform: Optional[str], kinds: list[str], card: Optional[str]
) -> None:
    """GameCube takes GCIs as saves or cards, Wii takes NAND saves, and any other platform takes nothing.

    Args:
        platform: The loaded platform.
        kinds: The kinds the spec declares, in order.
        card: The card subtree discovery reports.
    """
    emu = dolphin.Dolphin()
    emu.platform = platform
    spec = emu.import_spec()

    assert ([k.kind for k in spec.kinds], spec.card_subtree) == (kinds, card)


def test_a_push_after_an_import_is_held_to_the_imported_state(state_dir: Path) -> None:
    """The slot holds the imported state, so a push must be for the same game, by name and by header.

    Args:
        state_dir: The patched state directory.
    """
    emu = dolphin.Dolphin()
    emu.platform = "ngc"
    body = import_zip({".import/state/GZLE01.s04": b"GZLE01progress"})
    result = preflight_import(emu, body, rom_file=None, resume_slot=1, rom=_ROMM_ID)
    restore_import(emu, body, result)

    assert emu.state_target("GZLE01.s03") == state_dir / f"GZLE01.s{dolphin.STATE_SLOT:02d}"
    assert emu.state_target("GALE01.s03") is None
    assert emu.check_state_bytes(b"GALE01" + bytes(58)) is False
