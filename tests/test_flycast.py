"""Flycast ROM resolution, transient -config composition, launch, and exit via a graceful close request."""

import itertools
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn, Optional

import pytest

from webstation_broker.emulators import base, flycast


@pytest.fixture(autouse=True)
def fast_state_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the shutdown-save poll window so exit tests do not sit on real seconds."""
    monkeypatch.setattr(flycast, "STATE_WAIT", 0.5)
    monkeypatch.setattr(flycast, "STATE_STABLE", 0.0)


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Provide an isolated ROM library root patched onto flycast.ROM_ROOT."""
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(flycast, "ROM_ROOT", root)
    return root


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Provide an isolated Flycast data dir patched onto flycast.DATA_DIR and the class's save layout."""
    d = tmp_path / "data" / "flycast"
    d.mkdir(parents=True)
    monkeypatch.setattr(flycast, "DATA_DIR", d)
    monkeypatch.setattr(flycast.Flycast, "save_root", d.parent)
    monkeypatch.setattr(flycast.Flycast, "save_subtrees", (d.name,))
    return d


# ── resolve_rom_file / _pick_rom_file ───────────────────────────────────


def test_resolve_takes_a_direct_file_as_given(rom_root: Path) -> None:
    """A direct file path is returned as-is without a directory search."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(rom) == rom


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(rom_root: Path) -> None:
    """A path that is neither a file nor a directory resolves to nothing."""
    missing = rom_root / "nope"

    assert flycast.Flycast().resolve_rom_file(missing) is None


def test_resolve_prefers_chd_over_a_raw_gdi_beside_it(rom_root: Path) -> None:
    """A compressed .chd outranks a raw .gdi beside it."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "MyGame.gdi").write_bytes(b"")
    chd = folder / "MyGame.chd"
    chd.write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) == chd


def test_resolve_prefers_disc_1_over_disc_2_at_the_same_extension_rank(rom_root: Path) -> None:
    """Disc 1 outranks disc 2 when both candidates share an extension."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    disc1 = folder / "MyGame (Disc 1).cdi"
    disc1.write_bytes(b"")
    (folder / "MyGame (Disc 2).cdi").write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) == disc1


def test_resolve_ignores_dotfiles(rom_root: Path) -> None:
    """Dotfiles are never considered candidate ROMs."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / ".hidden.chd").write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) is None


def test_resolve_ignores_extensions_it_does_not_recognize(rom_root: Path) -> None:
    """Files with an unrecognized extension are never considered candidate ROMs."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) is None


def test_resolve_accepts_a_homebrew_elf(rom_root: Path) -> None:
    """A homebrew .elf is accepted as a candidate ROM."""
    folder = rom_root / "MyHomebrew"
    folder.mkdir()
    elf = folder / "MyHomebrew.elf"
    elf.write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) == elf


def test_resolve_refuses_a_disc_image_that_symlinks_outside_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """A disc image symlinking outside ROM_ROOT is refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.chd"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "MyGame.chd").symlink_to(secret)

    assert flycast.Flycast().resolve_rom_file(folder) is None


def test_resolve_accepts_a_disc_image_that_symlinks_inside_the_rom_root(rom_root: Path) -> None:
    """A disc image symlinking to another location inside ROM_ROOT is accepted."""
    shared = rom_root / "SharedAssets"
    shared.mkdir()
    real = shared / "actual.chd"
    real.write_bytes(b"game data")
    folder = rom_root / "MyGame"
    folder.mkdir()
    link = folder / "MyGame.chd"
    link.symlink_to(real)

    assert flycast.Flycast().resolve_rom_file(folder) == real


def test_resolve_searches_one_level_of_subfolders(rom_root: Path) -> None:
    """A candidate one subfolder deep is found by the search."""
    folder = rom_root / "MyGame"
    sub = folder / "disc"
    sub.mkdir(parents=True)
    rom = sub / "MyGame.chd"
    rom.write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) == rom


def test_resolve_keeps_the_candidates_it_could_read_when_one_search_pattern_fails(
    rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One unreadable subdirectory must not report a bootable title as having no boot file."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    rom = folder / "MyGame.chd"
    rom.write_bytes(b"")
    real_glob = Path.glob

    def flaky_glob(self: Path, pattern: str) -> Iterator[Path]:
        if pattern == "*/*":
            raise OSError("permission denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", flaky_glob)

    with caplog.at_level("WARNING"):
        assert flycast.Flycast().resolve_rom_file(folder) == rom

    assert "rom search" in caplog.text


# ── launch ───────────────────────────────────────────────────────────────


def test_launch_stops_then_spawns(data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch stops any running instance before spawning the new one."""
    order = []
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: order.append("stop"))

    def fake_spawn(
        self: flycast.Flycast, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        order.append("spawn")

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    flycast.Flycast().launch(rom, resume_slot=None)

    assert order == ["stop", "spawn"]


def test_launch_with_no_resume_slot_omits_autoloadstate(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launching with no resume slot omits Dreamcast.AutoLoadState entirely."""
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: flycast.Flycast, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    flycast.Flycast().launch(rom, resume_slot=None)

    assert spawned["cmd"] == [
        flycast.FLYCAST_BIN,
        "-config",
        "config:Dreamcast.AutoSaveState=yes,config:Dreamcast.SavestateSlot=0,"
        "window:fullscreen=yes",
        str(rom),
    ]


def test_launch_with_a_resume_slot_and_a_state_file_enables_autoloadstate(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume slot with an existing state file enables Dreamcast.AutoLoadState."""
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: flycast.Flycast, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    (data_dir / "game.state").write_bytes(b"state")

    flycast.Flycast().launch(rom, resume_slot=1)

    cmd = spawned["cmd"]
    assert "config:Dreamcast.AutoLoadState=yes" in cmd[2]


def test_launch_with_a_resume_slot_but_no_state_boots_fresh(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A resume slot with no existing state file boots fresh and logs a warning."""
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: flycast.Flycast, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    with caplog.at_level("WARNING"):
        flycast.Flycast().launch(rom, resume_slot=1)

    assert "config:Dreamcast.AutoLoadState=yes" not in spawned["cmd"][2]
    assert "resume requested but no resume state" in caplog.text


def test_launch_with_a_zero_byte_resume_state_boots_fresh(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty state file is a torn write, not a resume point: booting fresh beats a dead boot."""
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: flycast.Flycast, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    (data_dir / "game.state").write_bytes(b"")

    with caplog.at_level("WARNING"):
        flycast.Flycast().launch(rom, resume_slot=1)

    assert "config:Dreamcast.AutoLoadState=yes" not in spawned["cmd"][2]
    assert "resume requested but no resume state" in caplog.text


def test_a_resume_state_that_cannot_be_stat_ed_is_not_loaded_and_is_logged(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A state the broker cannot look at is never handed to AutoLoadState."""
    state = data_dir / "game.state"
    state.write_bytes(b"state")

    def boom(self: Path) -> NoReturn:
        raise OSError("stale nfs handle")

    monkeypatch.setattr(Path, "stat", boom)

    with caplog.at_level("WARNING"):
        assert flycast._state_is_loadable(state) is False

    assert "could not check the resume state" in caplog.text


# ── _window (title match confirmed by owning pid) ──────────────────────────


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode = None
        self.wait_calls: list[Optional[float]] = []
        self.wait_exc: Optional[Exception] = None
        self.exit_code: Optional[int] = None

    def poll(self) -> Optional[int]:
        return self.exit_code

    def wait(self, timeout: Optional[float] = None) -> int:
        self.wait_calls.append(timeout)
        if self.wait_exc is not None:
            raise self.wait_exc
        self.exit_code = 0
        self.returncode = 0
        return self.exit_code


def test_window_is_none_when_nothing_is_running() -> None:
    """No window is looked up when no process is running."""
    emu = flycast.Flycast()
    emu._proc = None

    assert emu._window() is None


def test_window_is_none_when_the_title_search_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed title search yields no window."""
    emu = flycast.Flycast()
    emu._proc = _FakeProc()
    monkeypatch.setattr(flycast.Flycast, "_xdotool", lambda self, *a: None)

    assert emu._window() is None


def test_window_returns_the_id_whose_pid_matches_the_launched_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window id confirmed to belong to the launched process's pid is returned."""
    emu = flycast.Flycast()
    emu._proc = _FakeProc(pid=4242)
    calls = []

    def fake_xdotool(self: flycast.Flycast, *args: str) -> Optional[str]:
        calls.append(args)
        if args[0] == "search":
            return "111\n222\n"
        if args == ("getwindowpid", "111"):
            return "9999\n"
        if args == ("getwindowpid", "222"):
            return "4242\n"
        raise AssertionError(f"unexpected xdotool call: {args}")

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    assert emu._window() == "222"


def test_window_ignores_a_title_match_owned_by_a_different_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A title match owned by a different pid is skipped, not returned."""
    emu = flycast.Flycast()
    emu._proc = _FakeProc(pid=4242)

    def fake_xdotool(self: flycast.Flycast, *args: str) -> Optional[str]:
        if args[0] == "search":
            return "111\n"
        if args == ("getwindowpid", "111"):
            return "9999\n"
        raise AssertionError(f"unexpected xdotool call: {args}")

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    with caplog.at_level("WARNING"):
        assert emu._window() is None

    assert "no flycast window found for pid 4242" in caplog.text


# ── stop (Alt+F4 close request, then SIGTERM escalation) ───────────────────


def test_stop_activates_the_window_and_sends_alt_f4_then_waits_for_exit(
    monkeypatch: pytest.MonkeyPatch, pid_record: Path
) -> None:
    """A successful Alt+F4 close request waits for exit without escalating."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    proc = _FakeProc()
    emu._proc = proc
    monkeypatch.setattr(emu, "_window", lambda: "12345")
    calls = []

    def fake_xdotool(self: flycast.Flycast, *args: str) -> Optional[str]:
        calls.append(args)
        return ""

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    emu.stop()

    assert calls == [
        ("windowactivate", "--sync", "12345"),
        ("getactivewindow",),
        ("key", "--clearmodifiers", "alt+F4"),
    ]
    assert proc.wait_calls == [emu.term_timeout]
    assert escalated == []
    assert emu._proc is None
    assert not pid_record.exists()


def test_stop_sends_alt_f4_once_the_emulator_window_is_confirmed_focused(
    monkeypatch: pytest.MonkeyPatch, pid_record: Path
) -> None:
    """Alt+F4 goes out over XTEST only once the focused window is the emulator's own."""
    monkeypatch.setattr(base.Emulator, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._proc = _FakeProc()
    monkeypatch.setattr(emu, "_window", lambda: "12345")
    calls = []

    def fake_xdotool(self: flycast.Flycast, *args: str) -> Optional[str]:
        calls.append(args)
        return "12345\n" if args == ("getactivewindow",) else ""

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    emu.stop()

    assert calls[-1] == ("key", "--clearmodifiers", "alt+F4")


def test_stop_skips_alt_f4_when_another_window_holds_the_focus(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A close request is never sent blind: another window in focus escalates instead."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    emu._proc = _FakeProc()
    monkeypatch.setattr(emu, "_window", lambda: "12345")
    calls = []

    def fake_xdotool(self: flycast.Flycast, *args: str) -> Optional[str]:
        calls.append(args)
        return "99999\n" if args == ("getactivewindow",) else ""

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    with caplog.at_level("WARNING"):
        emu.stop()

    assert ("key", "--clearmodifiers", "alt+F4") not in calls
    assert escalated == [True]
    assert "did not take focus" in caplog.text


def test_stop_falls_back_to_sigterm_when_no_window_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop escalates to SIGTERM when no window can be found."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    emu._proc = _FakeProc()
    monkeypatch.setattr(emu, "_window", lambda: None)

    emu.stop()

    assert escalated == [True]


def test_stop_falls_back_to_sigterm_when_the_activate_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop escalates to SIGTERM when window activation fails."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    emu._proc = _FakeProc()
    monkeypatch.setattr(emu, "_window", lambda: "12345")
    calls = []

    def fake_xdotool(self: flycast.Flycast, *args: str) -> Optional[str]:
        calls.append(args)
        return None if args[0] == "windowactivate" else ""

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    emu.stop()

    assert calls == [("windowactivate", "--sync", "12345")]
    assert escalated == [True]


def test_stop_falls_back_to_sigterm_when_the_process_never_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop escalates to SIGTERM when the process never exits after the close request."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    proc = _FakeProc()
    proc.wait_exc = subprocess.TimeoutExpired(cmd="flycast", timeout=emu.term_timeout)
    emu._proc = proc
    monkeypatch.setattr(emu, "_window", lambda: "12345")
    monkeypatch.setattr(flycast.Flycast, "_xdotool", lambda self, *a: "")

    emu.stop()

    assert escalated == [True]


def test_stop_is_a_no_op_when_nothing_is_running() -> None:
    """Stop does nothing when no process is running."""
    emu = flycast.Flycast()
    emu._proc = None

    emu.stop()

    assert emu._proc is None


def test_stop_skips_the_close_request_and_escalates_when_the_process_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop skips the window close request and escalates when the process already exited."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    proc = _FakeProc()
    proc.exit_code = 0
    emu._proc = proc
    window_calls = []
    monkeypatch.setattr(emu, "_window", lambda: window_calls.append(True))

    emu.stop()

    assert window_calls == []
    assert escalated == [True]


# ── save_and_exit ────────────────────────────────────────────────────────


def _touch(path: Path, content: bytes = b"state") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_exit_without_a_slot_reports_nothing_but_still_stops(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exiting without a slot reports no save but still stops the emulator."""
    stopped = []
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: stopped.append(True))
    emu = flycast.Flycast()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert stopped == [True]


def test_exit_with_a_slot_reports_the_state_the_shutdown_wrote(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state file written during a graceful shutdown is reported as saved."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state, b"a fresh state")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert report["state_slot"] == 1
    assert report["state_file"]["path"] == str(state)


def test_exit_waits_for_a_half_written_state_to_settle_before_measuring_it(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pid dying is not proof the state is whole, and the caller zips DATA_DIR the moment this returns.

    A state still being flushed must be waited out, not measured once and
    shipped to RomM half written.
    """
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"
    whole = b"a whole session, once the flush finishes"

    def fake_stop(self: flycast.Flycast) -> None:
        state.write_bytes(b"")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    chunks = iter([whole[:8], whole])

    def fake_sleep(_secs: float) -> None:
        chunk = next(chunks, None)
        if chunk is not None:
            state.write_bytes(chunk)

    monkeypatch.setattr(flycast.time, "sleep", fake_sleep)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert report["state_file"]["size"] == len(whole)
    assert state.read_bytes() == whole


def test_exit_never_reports_a_state_whose_write_does_not_settle(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A state still growing when the deadline passes is not reported, and not destroyed either."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        state.write_bytes(b"x")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    written = bytearray(b"x")

    def fake_sleep(_secs: float) -> None:
        written.extend(b"x")
        state.write_bytes(bytes(written))

    # A clock that only moves when it is read keeps the deadline deterministic
    # while the fake writer runs without ever pausing.
    ticks = itertools.count(0.0, 0.05)
    monkeypatch.setattr(flycast.time, "sleep", fake_sleep)
    monkeypatch.setattr(flycast.time, "monotonic", lambda: next(ticks))
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert "never finished writing" in caplog.text
    assert state.exists()


def test_exit_never_reports_a_state_left_at_zero_bytes(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A created-but-empty state is a write that never happened, however still its size holds."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        state.write_bytes(b"")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert "never finished writing" in caplog.text


def test_exit_with_a_slot_reports_no_save_when_the_state_is_unchanged(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unchanged state file after a graceful exit is reported as not saved."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = _touch(data_dir / "game.state", b"stale, never rewritten")

    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert "unchanged" in caplog.text
    assert state.exists()  # never discarded: a graceful exit, just no rewrite


def test_exit_with_a_slot_sets_aside_a_changed_state_killed_by_bare_sigterm(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Flycast has no SIGTERM handler, so the base class's SIGTERM escalation is already a hard kill.

    Same as SIGKILL, it ends the process before dc_exit() can run, so a
    state file that changed during the kill must not be reported as this
    exit's save.
    """
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state)

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -15  # -signal.SIGTERM
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not state.exists()
    assert "force-killed" in caplog.text
    assert "set aside untrusted resume state" in caplog.text


def test_exit_with_a_slot_keeps_a_changed_state_killed_by_sigkill_as_a_sidecar(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A state file that changed during a SIGKILL is set aside, never reported and never destroyed."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        # A close-request escalation to SIGKILL could still land after
        # dc_exit() wrote a complete state, or mid-write; either way the
        # broker cannot tell torn from complete, so a file that changed
        # during the kill is never trusted.
        _touch(state, b"maybe torn, maybe a whole session")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -9  # -signal.SIGKILL
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not state.exists()
    aside = data_dir / ("game.state" + flycast.UNTRUSTED_SUFFIX)
    assert aside.read_bytes() == b"maybe torn, maybe a whole session"
    assert "force-killed" in caplog.text
    assert "set aside untrusted resume state" in caplog.text


def test_a_set_aside_state_is_replaced_rather_than_piling_up(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second force-killed exit for the same rom replaces the earlier sidecar."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"
    aside = data_dir / ("game.state" + flycast.UNTRUSTED_SUFFIX)
    _touch(aside, b"from the last kill")

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state, b"from this kill")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -9
    monkeypatch.setattr(emu, "alive", lambda: True)

    emu.save_and_exit(1)

    assert aside.read_bytes() == b"from this kill"
    assert sorted(p.name for p in data_dir.iterdir()) == ["game.state" + flycast.UNTRUSTED_SUFFIX]


def test_a_set_aside_state_is_not_picked_up_as_a_resume_or_swept_by_a_restore(
    data_dir: Path, rom_root: Path
) -> None:
    """A sidecar is invisible to resume-state resolution and survives clear_working_slot."""
    rom = rom_root / "game.chd"
    aside = _touch(data_dir / ("game.state" + flycast.UNTRUSTED_SUFFIX), b"kept")

    flycast.Flycast().clear_working_slot()

    assert aside.exists()
    assert not flycast._state_path_for(rom).exists()


def test_a_state_that_cannot_be_set_aside_is_left_alone_and_logged(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rename that fails leaves the state in place rather than losing it, and is logged."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state, b"still the player's only copy")

    def boom(self: Path, target: Path) -> NoReturn:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    monkeypatch.setattr(Path, "replace", boom)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -9
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report["state_saved"] is False
    assert state.read_bytes() == b"still the player's only copy"
    assert "could not set aside untrusted resume state" in caplog.text


def test_exit_with_a_slot_leaves_an_unchanged_state_alone_when_force_killed(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unchanged state file from a force-killed exit is left alone, not discarded."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = _touch(data_dir / "game.state", b"from an earlier session")

    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -9  # -signal.SIGKILL
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert state.exists()  # unchanged by the kill, so nothing to discard
    assert "force-killed" in caplog.text


def test_exit_with_a_slot_sets_aside_a_changed_state_when_stop_never_confirms_the_exit(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed state file is set aside when stop cannot confirm a clean exit."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state)

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = None
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not state.exists()


def test_exit_with_a_slot_but_no_rom_loaded_warns_and_reports_nothing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Exiting with a slot but no rom loaded warns and reports nothing saved."""
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = None
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert "no rom is currently loaded" in caplog.text


def test_exit_with_a_slot_but_not_alive_reports_nothing(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exiting with a slot while not alive reports nothing saved."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    _touch(data_dir / "game.state")
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = rom
    monkeypatch.setattr(emu, "alive", lambda: False)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


def test_a_torn_state_is_set_aside_on_a_force_kill_even_with_no_slot_requested(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AutoSaveState is on for every launch, so a kill can tear a state no caller asked to save.

    The caller dumps DATA_DIR by mtime the moment this returns, so a state
    left plain would ride the archive as this player's progress.
    """
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state, b"maybe torn")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -9
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert not state.exists()
    aside = data_dir / ("game.state" + flycast.UNTRUSTED_SUFFIX)
    assert aside.read_bytes() == b"maybe torn"
    assert "force-killed" in caplog.text


def test_a_clean_exit_with_no_slot_leaves_its_state_in_place_unreported(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graceful exit's state is not quarantined when no slot was asked for, only left unreported."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state, b"a whole session")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert state.read_bytes() == b"a whole session"
    assert not (data_dir / ("game.state" + flycast.UNTRUSTED_SUFFIX)).exists()


# ── owner markers ────────────────────────────────────────────────────────


def _mark(state: Path, rom: Path) -> Path:
    """Write the owner marker claiming `state` for `rom`."""
    return _touch(
        state.with_name(state.name + flycast.OWNER_SUFFIX),
        f"{rom.resolve()}\n".encode(),
    )


def test_rom_identity_falls_back_to_the_given_path_when_it_cannot_be_resolved(
    rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unresolvable rom still gets an identity rather than failing the marker outright."""
    rom = rom_root / "game.chd"

    def boom(self: Path, strict: bool = False) -> NoReturn:
        raise OSError("stale nfs handle")

    monkeypatch.setattr(Path, "resolve", boom)

    with caplog.at_level("WARNING"):
        assert flycast._rom_identity(rom) == str(rom)

    assert "could not resolve" in caplog.text


def test_a_confirmed_save_marks_the_state_as_belonging_to_the_rom(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state filename carries only a basename, so a confirmed write records the rom it came from."""
    rom = rom_root / "USA" / "game.chd"
    rom.parent.mkdir()
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state, b"a fresh state")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    assert emu.save_and_exit(1)["state_saved"] is True

    marker = data_dir / ("game.state" + flycast.OWNER_SUFFIX)
    assert marker.read_text(encoding="utf-8").strip() == str(rom.resolve())


def test_an_unconfirmed_save_writes_no_marker(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state whose write never settled is not this exit's, so nothing claims it for this rom."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    _touch(data_dir / "game.state", b"stale, never rewritten")

    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    assert emu.save_and_exit(1)["state_saved"] is False
    assert not (data_dir / ("game.state" + flycast.OWNER_SUFFIX)).exists()


def test_a_marker_that_cannot_be_written_is_logged_and_the_save_still_reports(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unmarked state still ships and still resumes, so a failed marker costs no progress."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state, b"a fresh state")

    def boom(self: Path, data: str, encoding: Optional[str] = None) -> NoReturn:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    monkeypatch.setattr(Path, "write_text", boom)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        assert emu.save_and_exit(1)["state_saved"] is True

    assert "could not mark game.state as belonging to" in caplog.text


def test_a_state_with_no_rom_recorded_is_left_unmarked(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A marker naming no rom would claim the state for nothing, so none is written."""
    state = _touch(data_dir / "game.state")

    with caplog.at_level("WARNING"):
        flycast._write_owner_marker(state, None)

    assert not (data_dir / ("game.state" + flycast.OWNER_SUFFIX)).exists()
    assert "no rom recorded for this session" in caplog.text


def test_a_state_marked_for_another_disc_is_never_resumed_from(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A region pair in two folders shares one state filename; the marker is what tells them apart."""
    usa = rom_root / "USA" / "game.chd"
    usa.parent.mkdir()
    usa.write_bytes(b"")
    eur = rom_root / "EUR" / "game.chd"
    eur.parent.mkdir()
    eur.write_bytes(b"")
    _mark(_touch(data_dir / "game.state", b"the USA player's progress"), usa)

    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: flycast.Flycast, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)

    with caplog.at_level("WARNING"):
        flycast.Flycast().launch(eur, resume_slot=1)

    assert "config:Dreamcast.AutoLoadState=yes" not in spawned["cmd"][2]
    assert "booting clean" in caplog.text


def test_a_state_marked_for_this_disc_is_resumed_from(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker naming the booting rom is what lets its own state load."""
    rom = rom_root / "USA" / "game.chd"
    rom.parent.mkdir()
    rom.write_bytes(b"")
    _mark(_touch(data_dir / "game.state", b"progress"), rom)

    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: flycast.Flycast, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)

    flycast.Flycast().launch(rom, resume_slot=1)

    assert "config:Dreamcast.AutoLoadState=yes" in spawned["cmd"][2]


def test_a_state_with_no_marker_still_resumes(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmarked state is what archives written before markers existed carry."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    _touch(data_dir / "game.state", b"progress")

    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: flycast.Flycast, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)

    flycast.Flycast().launch(rom, resume_slot=1)

    assert "config:Dreamcast.AutoLoadState=yes" in spawned["cmd"][2]


def test_a_marker_that_cannot_be_read_is_logged_and_the_state_still_resumes(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable marker is no worse than none: refusing every resume over it helps nobody."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = _touch(data_dir / "game.state", b"progress")
    _mark(state, rom)

    def boom(self: Path, encoding: Optional[str] = None) -> NoReturn:
        raise OSError("stale nfs handle")

    monkeypatch.setattr(Path, "read_text", boom)

    with caplog.at_level("WARNING"):
        assert flycast._state_belongs_to(state, rom) is True

    assert "could not read the state marker" in caplog.text


def test_an_empty_marker_names_no_rom_and_does_not_block_a_resume(
    data_dir: Path, rom_root: Path
) -> None:
    """A truncated marker claims nothing, so it is treated as no marker at all."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = _touch(data_dir / "game.state", b"progress")
    _touch(state.with_name(state.name + flycast.OWNER_SUFFIX), b"")

    assert flycast._state_owner(state) is None
    assert flycast._state_belongs_to(state, rom) is True


def test_a_set_aside_state_takes_its_marker_with_it(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker left behind would claim the next state flycast writes under the same basename."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"
    _mark(_touch(state), rom)

    def fake_stop(self: flycast.Flycast) -> None:
        _touch(state, b"maybe torn")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -9
    monkeypatch.setattr(emu, "alive", lambda: True)

    emu.save_and_exit(1)

    assert not (data_dir / ("game.state" + flycast.OWNER_SUFFIX)).exists()
    aside_marker = data_dir / (
        "game.state" + flycast.UNTRUSTED_SUFFIX + flycast.OWNER_SUFFIX
    )
    assert aside_marker.read_text(encoding="utf-8").strip() == str(rom.resolve())


def test_a_marker_that_cannot_travel_with_its_state_is_logged(
    data_dir: Path, rom_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The state still gets set aside when only its marker cannot follow."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = _touch(data_dir / "game.state")
    _mark(state, rom)
    real_replace = Path.replace

    def flaky_replace(self: Path, target: Path) -> Path:
        if self.name.endswith(flycast.OWNER_SUFFIX):
            raise OSError("read-only filesystem")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    with caplog.at_level("WARNING"):
        flycast.Flycast()._set_aside_untrusted_state(state)

    assert (data_dir / ("game.state" + flycast.UNTRUSTED_SUFFIX)).exists()
    assert "could not set aside the state marker" in caplog.text


def test_the_owner_marker_rides_the_archive_as_ordinary_save_data() -> None:
    """A marker is not a state RomM can offer the player, so it is labelled as save data."""
    emulator = flycast.Flycast()
    data = flycast.DATA_DIR.name

    assert emulator.save_file_kind(f"{data}/Game.state{flycast.OWNER_SUFFIX}") == "save"
    assert emulator.save_file_kind(f"{data}/Game.state{flycast.UNTRUSTED_SUFFIX}") == "save"


# ── clear_working_slot ──────────────────────────────────────────────────


def test_clear_working_slot_is_a_noop_without_a_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the working slot without a data dir does nothing and does not raise."""
    missing = tmp_path / "data" / "flycast"
    monkeypatch.setattr(flycast, "DATA_DIR", missing)

    flycast.Flycast().clear_working_slot()  # must not raise

    assert not missing.exists()


def test_clear_working_slot_wipes_every_leftover_resume_state(data_dir: Path) -> None:
    """A leftover resume state from an earlier session must not outrank an incoming archive restore by mtime.

    The filename is only unambiguous once the rom is known, and
    clear_working_slot runs before that determination.
    """
    stale_a = _touch(data_dir / "game.state")
    stale_b = _touch(data_dir / "other.state")

    flycast.Flycast().clear_working_slot()

    assert not stale_a.exists()
    assert not stale_b.exists()
    assert data_dir.is_dir()


def test_clear_working_slot_sweeps_the_previous_players_loose_save_data(data_dir: Path) -> None:
    """VMU images, arcade nvmem and eeprom are not namespaced by title or player.

    Left in place they are handed to the next player and swept into that
    player's exit archive as their own progress.
    """
    leftovers = [
        _touch(data_dir / "vmu_save_A1.bin"),
        _touch(data_dir / "vmu_save_B1.bin"),
        _touch(data_dir / "nvmem.bin"),
        _touch(data_dir / "eeprom.bin"),
        _touch(data_dir / "game.state"),
        _touch(data_dir / ("game.state" + flycast.OWNER_SUFFIX)),
    ]

    flycast.Flycast().clear_working_slot()

    assert [p for p in leftovers if p.exists()] == []
    assert data_dir.is_dir()


def test_clear_working_slot_keeps_the_container_owned_bios_and_config(data_dir: Path) -> None:
    """The BIOS pair and emu.cfg are container setup every session needs, not one player's data."""
    kept = [
        _touch(data_dir / "dc_boot.bin", b"bios"),
        _touch(data_dir / "dc_flash.bin", b"flash"),
        _touch(data_dir / "emu.cfg", b"[config]"),
    ]

    flycast.Flycast().clear_working_slot()

    assert all(p.exists() for p in kept)


def test_clear_working_slot_keeps_a_quarantined_state_and_its_marker(data_dir: Path) -> None:
    """A set-aside state can be the only copy of that progress, and no resume can pick it up anyway."""
    aside = _touch(data_dir / ("game.state" + flycast.UNTRUSTED_SUFFIX), b"kept")
    marker = _touch(
        data_dir / ("game.state" + flycast.UNTRUSTED_SUFFIX + flycast.OWNER_SUFFIX), b"/romm/game.chd"
    )

    flycast.Flycast().clear_working_slot()

    assert aside.exists()
    assert marker.exists()


def test_clear_working_slot_leaves_subdirectories_alone(data_dir: Path) -> None:
    """Flycast's controller mappings are container setup, not session save data."""
    mapping = data_dir / "mappings" / "SDL_Xbox.cfg"
    _touch(mapping, b"[emulator]")

    flycast.Flycast().clear_working_slot()

    assert mapping.exists()


def test_clear_working_slot_tolerates_a_file_it_cannot_delete(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that cannot be deleted is logged and does not raise."""
    stuck = _touch(data_dir / "game.state")

    def boom(self: Path) -> NoReturn:
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", boom)

    with caplog.at_level("WARNING"):
        flycast.Flycast().clear_working_slot()  # must not raise

    assert stuck.exists()
    assert "could not clear stale save data" in caplog.text


def test_clear_working_slot_tolerates_a_data_dir_it_cannot_list(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A data dir that cannot be listed is logged and does not raise."""

    def boom(self: Path) -> NoReturn:
        raise OSError("stale nfs handle")

    monkeypatch.setattr(Path, "iterdir", boom)

    with caplog.at_level("WARNING"):
        flycast.Flycast().clear_working_slot()  # must not raise

    assert "could not list" in caplog.text


# ── class attributes (API surface parity with the other exit-only emulators) ──


def test_class_attributes_match_the_exit_only_api_surface(data_dir: Path) -> None:
    """The class attributes match the exit-only API surface shared with other emulators."""
    emu = flycast.Flycast()

    assert emu.rom_extensions == (".chd", ".gdi", ".cdi", ".cue", ".elf")
    assert emu.supports_states is False
    assert emu.supports_disc_swap is False


def test_the_savestate_is_labelled_apart_from_the_vmu_saves() -> None:
    """The savestate is labelled apart from the VMU images it sits beside."""
    emulator = flycast.Flycast()
    data = flycast.DATA_DIR.name

    assert emulator.save_file_kind(f"{data}/Game.state") == "state"
    assert emulator.save_file_kind(f"{data}/vmu_save_A1.bin") == "save"
