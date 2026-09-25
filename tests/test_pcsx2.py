"""PCSX2 state naming and slot resolution.

Also covers the memory card contract and the boot watchdog that flags a VM
that never comes up.
"""

import os
import struct
import time
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, Optional

import pytest

from webstation_broker import imports
from webstation_broker.emulators import pcsx2

from .conftest import import_zip, preflight_import, restore_import


@pytest.fixture
def sstate_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the PCSX2 state and memcard directories under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The state directory.
    """
    d = tmp_path / "sstates"
    d.mkdir()
    monkeypatch.setattr(pcsx2, "SSTATE_DIR", d)
    monkeypatch.setattr(pcsx2, "MEMCARD_DIR", tmp_path / "memcards")
    # The save subtrees hang off the data root, which the class reads once at
    # import, so the clear would reach outside tmp_path without this.
    monkeypatch.setattr(pcsx2.Pcsx2, "save_root", tmp_path)
    return d


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point PCSX2's ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(pcsx2, "ROM_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def _no_real_patches_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every test in this module off the network, sudo and `/usr/share`.

    A launch fetches patches.zip when the installed one is missing, and the
    real path is missing on any dev machine, so an unstubbed launch test would
    otherwise shell out to curl and sudo for real.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.
    """
    resources = tmp_path / "usr-share-PCSX2-resources"
    resources.mkdir()
    monkeypatch.setattr(pcsx2, "PATCHES_ZIP", resources / "patches.zip")

    def refuse(cmd: list[str], **kwargs: object) -> NoReturn:
        raise AssertionError(f"unstubbed subprocess.run in a pcsx2 test: {cmd}")

    monkeypatch.setattr(pcsx2.subprocess, "run", refuse)


def _touch(path: Path, mtime: Optional[float] = None) -> Path:
    """Write a placeholder state file, optionally with a fixed mtime.

    Args:
        path: The file to create.
        mtime: Modification time to stamp on it, if any.

    Returns:
        The path that was written.
    """
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_patches_zip(path: Path, members: Optional[dict[str, str]] = None) -> Path:
    """Write a small zip shaped like PCSX2's patch bundle.

    Args:
        path: Where to write it.
        members: Archive name to text content; one `.pnach` entry by default.

    Returns:
        The path that was written.
    """
    if members is None:
        members = {"SLUS-20946_7D3A8B4E.pnach": "patch=1,EE,00000000,extended,00000000"}
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path


def _fake_fetch_run(
    calls: list[list[str]],
    download: Optional[bytes] = None,
    fail: Optional[str] = None,
) -> Callable[..., object]:
    """Build a `subprocess.run` stand-in that plays curl, install and mv.

    Args:
        calls: List each argv is appended to, in order.
        download: Bytes curl "downloads"; a valid patch bundle when None.
        fail: Basename of the binary to fail (`"curl"`, `"install"`, `"mv"`), if any.

    Returns:
        The fake.
    """

    def run(cmd: list[str], **kwargs: object) -> object:
        calls.append(cmd)
        tool = Path(cmd[2] if cmd[0] == pcsx2._SUDO else cmd[0]).name
        if tool == fail:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": f"{tool} said no"})()
        if tool == "curl":
            out = Path(cmd[cmd.index("--output") + 1])
            if download is None:
                _write_patches_zip(out)
            else:
                out.write_bytes(download)
        elif tool == "install":
            Path(cmd[-1]).write_bytes(Path(cmd[-2]).read_bytes())
        elif tool == "mv":
            Path(cmd[-2]).replace(cmd[-1])
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    return run


def _swallow_thread(
    target: Callable[..., object], args: tuple[object, ...] = (), daemon: bool = False
) -> object:
    """Stand in for `Thread` when a test only needs the background work never to run.

    Args:
        target: The callable the real `Thread` would run.
        args: Positional args the real `Thread` would pass to `target`.
        daemon: Whether the real `Thread` would be a daemon thread.

    Returns:
        An object whose `start` does nothing.
    """
    return type("MockThread", (), {"start": lambda s: None})()


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("SLUS-20946 (7D3A8B4E).01.p2s", "SLUS-20946 (7D3A8B4E).10.p2s"),
        # A capture from a single-digit slot still lands in the working slot.
        ("SLUS-20946 (7D3A8B4E).9.p2s", "SLUS-20946 (7D3A8B4E).10.p2s"),
        ("SLUS-20946 (7D3A8B4E).10.p2s", "SLUS-20946 (7D3A8B4E).10.p2s"),
    ],
)
def test_restamp_keeps_the_serial_and_rewrites_the_slot(filename: str, expected: str) -> None:
    """Restamping keeps the serial and CRC and rewrites only the slot number."""
    assert pcsx2._restamp_slot(filename, 10) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "SLUS-20946.p2s",
        "SLUS-20946.10.sav",
        "card.bin",
        "",
        # A serial cannot span a path separator, whoever else is checking.
        "../escape.01.p2s",
        "sub/SLUS-20946.01.p2s",
        "/abs/SLUS-20946.01.p2s",
    ],
)
def test_restamp_refuses_anything_that_is_not_a_state_name(filename: str) -> None:
    """Restamping returns None for a name that is not a PCSX2 state name."""
    assert pcsx2._restamp_slot(filename, 10) is None


@pytest.mark.parametrize(
    ("name", "slot", "expected"),
    [
        ("SLUS-20946.10.p2s", 10, True),
        ("SLUS-20946.01.p2s", 1, True),
        ("SLUS-20946.1.p2s", 1, True),
        ("SLUS-20946.02.p2s", 1, False),
    ],
)
def test_slot_match_accepts_both_widths_pcsx2_writes(name: str, slot: int, expected: bool) -> None:
    """A slot matches whether PCSX2 wrote it with one digit or two."""
    assert pcsx2._matches_slot(Path(name), slot) is expected


def test_working_slot_reads_the_newest_state_in_it(sstate_dir: Path) -> None:
    """The working slot resolves to the newest state in that slot, ignoring other slots."""
    _touch(sstate_dir / "SLUS-1.10.p2s", mtime=1000)
    newest = _touch(sstate_dir / "SLUS-2.10.p2s", mtime=3000)
    _touch(sstate_dir / "SLUS-3.02.p2s", mtime=9000)

    assert pcsx2.newest_state_for_slot(10) == newest


def test_working_slot_is_empty_when_it_holds_nothing(sstate_dir: Path) -> None:
    """The working slot resolves to None when only other slots hold states."""
    _touch(sstate_dir / "SLUS-3.02.p2s")

    assert pcsx2.newest_state_for_slot(10) is None


def test_state_target_names_a_push_for_the_working_slot(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pushed state is targeted at the working slot under its own serial."""
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)

    target = pcsx2.Pcsx2().state_target("SLUS-20946 (7D3A8B4E).03.p2s")

    assert target == sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s"


def test_state_target_refuses_another_disc_over_the_state_in_the_slot(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push for another disc cannot land on top of the state already in the slot."""
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)
    existing = _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s")

    assert pcsx2.Pcsx2().state_target("SLUS-20946 (7D3A8B4E).01.p2s") == existing
    assert pcsx2.Pcsx2().state_target("SLES-51234 (00000000).01.p2s") is None


@pytest.mark.parametrize(
    "filename",
    [
        "../escape.01.p2s",
        "",
        ".",
        "..",
        "card.bin",
        "SLUS-20946.\u0660\u0661.p2s",
        "SLUS-20946.01.p2s\n",
        " .01.p2s",
    ],
)
def test_state_target_refuses_a_name_pcsx2_would_never_write(sstate_dir: Path, filename: str) -> None:
    """A push whose name PCSX2 would never write is refused.

    Args:
        sstate_dir: The patched state directory.
        filename: The pushed name.
    """
    assert pcsx2.Pcsx2().state_target(filename) is None


@pytest.mark.parametrize("raw", ["card\n", "card\nSlot2_Filename=x", "c\u0430rd"])
def test_a_card_name_with_a_line_break_or_non_ascii_falls_back(raw: str) -> None:
    """`$` matched before a trailing newline, which would have reached the ini as a new line.

    The non-ASCII case already fell back, because the character class is spelled out as ASCII; it
    pins that behaviour.

    Args:
        raw: The `PCSX2_SLOT1_CARD` value.
    """
    assert pcsx2._slot1_card_name(raw) == "romm-slot1"


def test_clearing_the_slot_takes_every_state_not_just_the_broker_slot(
    sstate_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state in any slot is the last session's, and nothing in its name says so."""
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)
    stale = _touch(sstate_dir / "SLUS-20946.10.p2s")
    other = _touch(sstate_dir / "SLUS-20946.02.p2s")
    slot = tmp_path / "memcards" / "Slot 1"
    slot.mkdir(parents=True)
    card = _touch(slot / "_pcsx2_superblock")

    pcsx2.Pcsx2().clear_working_slot()

    assert not stale.exists()
    assert not other.exists()
    assert not card.exists()
    assert sstate_dir.is_dir()


def test_clearing_the_slot_keeps_a_card_the_memory_route_just_synced(
    sstate_dir: Path, tmp_path: Path
) -> None:
    """The card is hydrated before activate, so a clear that took it would drop it."""
    slot = tmp_path / "memcards" / "Slot 1"
    slot.mkdir(parents=True)
    card = _touch(slot / "_pcsx2_superblock")
    stale = _touch(sstate_dir / "SLUS-20946.02.p2s")

    pcsx2.Pcsx2().clear_working_slot(("memcards",))

    assert card.exists()
    assert not stale.exists()


def test_the_card_the_whole_card_routes_sync_is_the_slot_1_folder() -> None:
    """The memory card the routes sync is the slot 1 folder card inside the save archive."""
    emu = pcsx2.Pcsx2()

    assert emu.memory_card_path().parent == pcsx2.MEMCARD_DIR
    # The card rides the memory-card routes, so activate has to be able to take
    # it back out of the save archive by name.
    assert emu.memory_card_subtree in emu.save_subtrees
    assert emu.memory_card_marker


def test_a_state_still_open_by_the_emulator_is_not_a_finished_write(sstate_dir: Path) -> None:
    """A state whose size sits still while pcsx2 still holds it open never counts as saved."""
    before = pcsx2._sstate_snapshot()
    target = sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s"
    with target.open("wb") as fh:
        fh.write(b"half a state")
        fh.flush()

        settled = pcsx2._wait_for_sstate_write(
            before, time.monotonic() + 0.9, 10, os.getpid()
        )

    assert settled is False


def test_a_state_the_emulator_has_closed_counts_as_a_finished_write(sstate_dir: Path) -> None:
    """A non-empty state with no descriptor left on it settles as saved."""
    before = pcsx2._sstate_snapshot()
    _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s")

    assert pcsx2._wait_for_sstate_write(before, time.monotonic() + 5.0, 10, os.getpid()) is True


def test_an_empty_state_file_is_never_a_finished_write(sstate_dir: Path) -> None:
    """A zero-byte state is a write that produced nothing, not a save."""
    before = pcsx2._sstate_snapshot()
    (sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s").write_bytes(b"")

    assert pcsx2._wait_for_sstate_write(before, time.monotonic() + 0.9, 10) is False


class _FakePineSocket:
    """A PINE socket stand-in that replays a canned reply.

    Attributes:
        reply: The bytes the peer sends back, handed out in recv-sized slices.
        requested: Every byte count recv was asked for, so a test can prove
            the broker never tried to buy the whole declared reply.
    """

    def __init__(self, reply: bytes) -> None:
        """Start with the whole reply still to be read.

        Args:
            reply: The bytes the fake peer sends back.
        """
        self.reply = reply
        self.requested: list[int] = []

    def __enter__(self) -> "_FakePineSocket":
        """Return the socket itself, the way socket.socket's context manager does."""
        return self

    def __exit__(self, *exc: object) -> bool:
        """Leave any exception to propagate."""
        return False

    def settimeout(self, timeout: Optional[float]) -> None:
        """Accept the timeout the broker sets; nothing here ever blocks."""

    def connect(self, address: str) -> None:
        """Accept the connect; the fake peer is always up."""

    def sendall(self, data: bytes) -> None:
        """Accept the request packet unread."""

    def recv(self, n: int) -> bytes:
        """Hand back up to `n` bytes of the canned reply.

        Args:
            n: The most bytes the caller will take.

        Returns:
            The next slice of the reply, empty once it is spent.
        """
        self.requested.append(n)
        chunk, self.reply = self.reply[:n], self.reply[n:]
        return chunk


def test_a_pine_reply_that_declares_a_huge_body_is_refused(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply header claiming gigabytes is dropped instead of accumulated."""
    sock = _FakePineSocket(struct.pack("<IB", 0xFFFFFFFF, 0))
    monkeypatch.setattr(pcsx2._socket, "socket", lambda family, kind: sock)

    assert pcsx2._pine_request(pcsx2._PINE_MSG_EMU_STATUS) is None
    # Only the 5-byte header was ever read for.
    assert max(sock.requested) <= 5


def test_a_pine_reply_within_the_ceiling_still_comes_back(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed status reply is read and returned."""
    sock = _FakePineSocket(struct.pack("<IB", 9, 0) + struct.pack("<I", 0))
    monkeypatch.setattr(pcsx2._socket, "socket", lambda family, kind: sock)

    assert pcsx2._pine_emu_status() == 0


class _FakeClock:
    """A monotonic() stand-in that advances by `step` seconds each call.

    A 90s deadline resolves in microseconds of real test time.

    Attributes:
        now: The time the last call returned.
        step: Seconds added on every call.
    """

    def __init__(self, step: float = 30.0) -> None:
        """Start the clock at zero.

        Args:
            step: Seconds the clock advances on every call.
        """
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        """Advance the clock and return the new time.

        Returns:
            The current fake monotonic time.
        """
        self.now += self.step
        return self.now


@pytest.fixture
def watchdog_env(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Patch sleeping and the clock so _boot_watchdog tests finish instantly.

    No real sleeping, no real PINE socket, a clock that reaches the 90s
    deadline in a handful of calls.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fake clock installed as time.monotonic.
    """
    monkeypatch.setattr(pcsx2.time, "sleep", lambda _seconds: None)
    clock = _FakeClock()
    monkeypatch.setattr(pcsx2.time, "monotonic", clock)
    return clock


def test_boot_watchdog_clears_the_flag_when_the_vm_boots_promptly(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog leaves boot_failed clear when PINE reports the VM running."""
    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: 0)
    monkeypatch.setattr(pcsx2.Pcsx2, "wait_for_state", lambda self, deadline: True)
    monkeypatch.setattr(pcsx2.Pcsx2, "load_state", lambda self, slot: True)
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(1, emu._launch_seq)

    assert emu.boot_failed is False


def test_the_resume_state_wait_does_not_ride_the_boot_deadline(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """A slow boot still leaves the resume wait its full budget for the pushed state."""
    seen: list[float] = []

    def record(self: pcsx2.Pcsx2, deadline: float) -> bool:
        """Record the deadline the watchdog gave the state wait."""
        seen.append(deadline)
        return True

    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: 0)
    monkeypatch.setattr(pcsx2, "RESUME_STATE_WAIT", 1000.0)
    monkeypatch.setattr(pcsx2.Pcsx2, "wait_for_state", record)
    monkeypatch.setattr(pcsx2.Pcsx2, "load_state", lambda self, slot: True)
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(1, emu._launch_seq)

    # The boot deadline is RESUME_LOAD_WAIT (90s) from the start of the poll,
    # so anything past it can only have come from a budget of its own.
    assert seen and seen[0] > pcsx2.RESUME_LOAD_WAIT


def test_boot_watchdog_flags_a_hang_when_the_process_is_still_alive(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog sets boot_failed when the deadline passes with the process still alive."""
    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: True)
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(1, emu._launch_seq)

    assert emu.boot_failed is True


def test_boot_watchdog_does_not_flag_a_process_that_already_exited(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog does not flag a hang when the process has already exited."""
    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: False)
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(1, emu._launch_seq)

    assert emu.boot_failed is False


def test_boot_watchdog_abandons_a_superseded_launch(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog gives up without flagging when a relaunch bumps the launch sequence."""
    emu = pcsx2.Pcsx2()
    seq = emu._launch_seq
    calls = {"n": 0}

    def status() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            emu._launch_seq += 1  # a relaunch/stop landed mid-wait
        return None

    monkeypatch.setattr(pcsx2, "_pine_emu_status", status)
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: True)

    emu._boot_watchdog(1, seq)

    assert emu.boot_failed is False


def test_boot_watchdog_runs_and_can_flag_a_hang_with_no_resume_slot(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog flags a hang with no resume slot and never attempts a load."""
    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: True)
    load_calls = []
    monkeypatch.setattr(pcsx2.Pcsx2, "load_state", lambda self, slot: load_calls.append(slot))
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(None, emu._launch_seq)

    assert emu.boot_failed is True
    assert load_calls == []


def test_launch_always_spawns_the_watchdog_even_with_no_resume_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch spawns the boot watchdog thread even when there is no resume slot.

    Regression test: ensures the 'if resume_slot is not None:' guard is never
    accidentally restored.
    """
    started = []
    monkeypatch.setattr(pcsx2, "_patch_ini", lambda: None)
    monkeypatch.setattr(pcsx2, "_ensure_patches_zip", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_ensure_folder_card", lambda self: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_spawn", lambda self, cmd, env: None)

    def mock_thread(
        target: Callable[..., object], args: tuple[object, ...], daemon: bool
    ) -> object:
        """Capture Thread calls and record (target.__name__, args)."""
        started.append((target.__name__, args))
        return type("MockThread", (), {"start": lambda s: None})()

    monkeypatch.setattr(pcsx2, "Thread", mock_thread)
    emu = pcsx2.Pcsx2()

    emu.launch(tmp_path / "g.iso", None)

    assert len(started) == 1
    assert started[0][0] == "_boot_watchdog"
    assert started[0][1] == (None, emu._launch_seq)


def test_an_unpatchable_ini_is_raised_rather_than_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ini the broker cannot rewrite fails loudly instead of leaving PINE off."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"")
    monkeypatch.setattr(pcsx2, "INI_PATH", blocked / "PCSX2.ini")

    with pytest.raises(RuntimeError):
        pcsx2._patch_ini()


def test_patches_zip_points_at_the_only_path_pcsx2_reads() -> None:
    """PCSX2 opens the bundle from its install resources, never from the user tree.

    Probed live on 2.6.3: a copy under `$XDG_CONFIG_HOME/PCSX2/resources` was
    ignored and the warning stayed until the system copy existed.
    """
    assert Path("/usr/share/PCSX2/resources/patches.zip") == pcsx2._PATCHES_ZIP_SYSTEM_PATH


def test_patches_zip_problem_reports_a_missing_file(tmp_path: Path) -> None:
    """No file is a problem, so the launch hook knows to fetch one."""
    assert pcsx2._patches_zip_problem(tmp_path / "patches.zip") == "missing"


def test_patches_zip_problem_accepts_a_good_bundle(tmp_path: Path) -> None:
    """A zip that opens, passes its CRC check and holds a `.pnach` is usable."""
    assert pcsx2._patches_zip_problem(_write_patches_zip(tmp_path / "patches.zip")) is None


def test_patches_zip_problem_rejects_an_empty_file(tmp_path: Path) -> None:
    """A zero-byte file, the shape a cut-off transfer leaves, is not usable."""
    path = tmp_path / "patches.zip"
    path.write_bytes(b"")
    assert pcsx2._patches_zip_problem(path) == "empty"


def test_patches_zip_problem_rejects_something_that_is_not_a_zip(tmp_path: Path) -> None:
    """An HTML error page served with a 200 is refused rather than installed."""
    path = tmp_path / "patches.zip"
    path.write_bytes(b"<html>rate limited</html>")
    assert pcsx2._patches_zip_problem(path) is not None


def test_patches_zip_problem_rejects_a_zip_with_no_pnach_entries(tmp_path: Path) -> None:
    """A valid zip that holds no patches is not the bundle PCSX2 wants."""
    path = _write_patches_zip(tmp_path / "patches.zip", {"README.md": "moved"})
    assert pcsx2._patches_zip_problem(path) == "holds no .pnach patches"


def test_patches_zip_problem_rejects_a_zip_that_fails_its_crc(tmp_path: Path) -> None:
    """A member whose bytes no longer match its CRC is refused."""
    path = tmp_path / "patches.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("SLUS-20946_7D3A8B4E.pnach", "patch=1,EE,00000000,extended,00000000")
    raw = bytearray(path.read_bytes())
    raw[raw.index(b"patch=1")] ^= 0xFF
    path.write_bytes(bytes(raw))
    assert pcsx2._patches_zip_problem(path) is not None




def test_patches_zip_problem_reports_a_corrupt_deflate_stream_instead_of_raising(tmp_path: Path) -> None:
    """Damaged compressed bytes make zlib raise its own error, which becomes a reason."""
    path = tmp_path / "patches.zip"
    body = "".join(f"patch=1,EE,{n:08X},extended,{n * 7:08X}\n" for n in range(2000))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SLUS-20946_7D3A8B4E.pnach", body)
    raw = bytearray(path.read_bytes())
    for offset in range(60, 400):
        raw[offset] ^= 0x55
    path.write_bytes(bytes(raw))
    assert pcsx2._patches_zip_problem(path) is not None

@pytest.mark.parametrize(
    "error",
    [RuntimeError("File is encrypted, password required"), NotImplementedError("compression type 99")],
)
def test_patches_zip_problem_reports_an_unreadable_member_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """An encrypted or exotically compressed member becomes a reason, never an exception."""
    path = _write_patches_zip(tmp_path / "patches.zip")

    def _raise(self: zipfile.ZipFile) -> None:
        """Raise what ZipFile.open raises for a member it cannot read.

        Args:
            self: The zip under test.

        Raises:
            Exception: The parametrized error.
        """
        raise error

    monkeypatch.setattr(zipfile.ZipFile, "testzip", _raise)
    assert pcsx2._patches_zip_problem(path) == str(error)

def test_refresh_downloads_validates_and_moves_the_bundle_into_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A good download ends up at the path PCSX2 reads, and nothing is left staged."""
    calls: list[list[str]] = []
    monkeypatch.setattr(pcsx2.subprocess, "run", _fake_fetch_run(calls))

    assert pcsx2._refresh_patches_zip() is True

    assert pcsx2._patches_zip_problem(pcsx2.PATCHES_ZIP) is None
    assert [p.name for p in pcsx2.PATCHES_ZIP.parent.iterdir()] == ["patches.zip"]


def test_refresh_runs_curl_unprivileged_with_hardcoded_https_only_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl never runs under sudo, never skips TLS checks, and only speaks HTTPS."""
    calls: list[list[str]] = []
    monkeypatch.setattr(pcsx2.subprocess, "run", _fake_fetch_run(calls))

    pcsx2._refresh_patches_zip()

    curl = calls[0]
    assert curl[0] == "/usr/bin/curl"
    assert curl[-2:] == [
        "--",
        "https://github.com/PCSX2/pcsx2_patches/releases/download/latest/patches.zip",
    ]
    assert curl[curl.index("--proto") + 1] == "=https"
    assert curl[curl.index("--proto-redir") + 1] == "=https"
    assert "--fail" in curl
    assert not {"-k", "--insecure"} & set(curl)


def test_refresh_stages_then_renames_never_installs_over_the_live_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live path only ever changes by rename, so a booting PCSX2 never reads a half-copied zip."""
    calls: list[list[str]] = []
    monkeypatch.setattr(pcsx2.subprocess, "run", _fake_fetch_run(calls))

    pcsx2._refresh_patches_zip()

    install, mv = calls[1], calls[2]
    staged = str(pcsx2.PATCHES_ZIP.with_name(".patches.zip.new"))
    source = Path(install[-2])
    assert install[:3] == ["/usr/bin/sudo", "-n", "/usr/bin/install"]
    assert install[3:9] == ["-m", "0644", "-o", "root", "-g", "root"]
    assert install[-3:] == ["--", install[-2], staged]
    # The source is the unprivileged temp download, never a path already
    # sitting beside the live file.
    assert source.name == "patches.zip"
    assert source.parent != pcsx2.PATCHES_ZIP.parent
    assert mv == ["/usr/bin/sudo", "-n", "/usr/bin/mv", "-f", "--", staged, str(pcsx2.PATCHES_ZIP)]


def test_refresh_does_not_install_a_download_that_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error page served with a 200 never reaches sudo, and a working install survives."""
    _write_patches_zip(pcsx2.PATCHES_ZIP)
    before = pcsx2.PATCHES_ZIP.read_bytes()
    calls: list[list[str]] = []
    monkeypatch.setattr(pcsx2.subprocess, "run", _fake_fetch_run(calls, download=b"<html>"))

    assert pcsx2._refresh_patches_zip() is False

    assert len(calls) == 1
    assert pcsx2.PATCHES_ZIP.read_bytes() == before


@pytest.mark.parametrize(
    ("fail", "verb"), [("curl", "download"), ("install", "install"), ("mv", "rename")]
)
def test_refresh_failure_at_any_step_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fail: str, verb: str
) -> None:
    """A network, sudo or rename failure returns False and says which step broke, at warning level."""
    calls: list[list[str]] = []
    monkeypatch.setattr(pcsx2.subprocess, "run", _fake_fetch_run(calls, fail=fail))

    with caplog.at_level("WARNING", logger=pcsx2.log.name):
        assert pcsx2._refresh_patches_zip() is False

    assert f"{fail} said no" in caplog.text
    assert f"patches fetch: {verb}" in caplog.text
    matching = [r for r in caplog.records if f"patches fetch: {verb}" in r.message]
    assert matching and all(r.levelname == "WARNING" for r in matching)


def test_refresh_survives_a_timeout_or_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung curl or an image without curl is a logged skip, not an exception."""

    def hang(cmd: list[str], **kwargs: object) -> NoReturn:
        raise pcsx2.subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(pcsx2.subprocess, "run", hang)
    assert pcsx2._refresh_patches_zip() is False

    def missing(cmd: list[str], **kwargs: object) -> NoReturn:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(pcsx2.subprocess, "run", missing)
    assert pcsx2._refresh_patches_zip() is False


def test_refresh_skips_when_the_resources_dir_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No PCSX2 resources directory means no PCSX2 to patch: no fetch, no sudo."""
    monkeypatch.setattr(pcsx2, "PATCHES_ZIP", tmp_path / "absent" / "patches.zip")

    assert pcsx2._refresh_patches_zip() is False  # the autouse stub raises if anything runs


def test_refresh_is_skipped_while_another_is_running() -> None:
    """A second launch does not queue behind, or double up on, a fetch already in flight."""
    assert pcsx2._PATCHES_LOCK.acquire(blocking=False)
    try:
        assert pcsx2._refresh_patches_zip() is False  # the autouse stub raises if anything runs
    finally:
        pcsx2._PATCHES_LOCK.release()


def test_refresh_survives_a_filesystem_error_and_frees_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full or read-only TMPDIR is a logged skip, not an exception that escapes launch()."""

    def broken_tempdir(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("no space left on device")

    monkeypatch.setattr(pcsx2.tempfile, "TemporaryDirectory", broken_tempdir)

    assert pcsx2._refresh_patches_zip() is False

    assert pcsx2._PATCHES_LOCK.acquire(blocking=False)
    pcsx2._PATCHES_LOCK.release()


def test_ensure_fetches_synchronously_when_the_bundle_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing bundle is fetched before PCSX2 starts, since PCSX2 opens it at boot."""
    ran: list[str] = []
    monkeypatch.setattr(pcsx2, "_refresh_patches_zip", lambda: ran.append("sync") or True)
    monkeypatch.setattr(pcsx2, "Thread", lambda **kw: pytest.fail("fetched in the background"))

    pcsx2._ensure_patches_zip()

    assert ran == ["sync"]


def test_ensure_refetches_synchronously_when_the_bundle_is_corrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt install is treated like a missing one and replaced before boot."""
    pcsx2.PATCHES_ZIP.write_bytes(b"")
    ran: list[str] = []
    monkeypatch.setattr(pcsx2, "_refresh_patches_zip", lambda: ran.append("sync") or True)

    pcsx2._ensure_patches_zip()

    assert ran == ["sync"]


def test_ensure_leaves_a_fresh_bundle_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid bundle younger than the max age costs the launch nothing."""
    _write_patches_zip(pcsx2.PATCHES_ZIP)
    monkeypatch.setattr(pcsx2, "_refresh_patches_zip", lambda: pytest.fail("fetched"))
    monkeypatch.setattr(pcsx2, "Thread", lambda **kw: pytest.fail("fetched in the background"))

    pcsx2._ensure_patches_zip()


def test_ensure_refreshes_a_stale_bundle_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid but old bundle boots as-is while a newer one is fetched behind it."""
    _write_patches_zip(pcsx2.PATCHES_ZIP)
    old = time.time() - pcsx2.PATCHES_MAX_AGE - 60
    os.utime(pcsx2.PATCHES_ZIP, (old, old))
    started: list[object] = []
    monkeypatch.setattr(pcsx2, "_refresh_patches_zip", lambda: pytest.fail("fetched inline"))

    def mock_thread(target: Callable[..., object], daemon: bool) -> object:
        """Record the background target instead of running it."""
        started.append(target)
        return type("MockThread", (), {"start": lambda s: None})()

    monkeypatch.setattr(pcsx2, "Thread", mock_thread)

    pcsx2._ensure_patches_zip()

    assert started == [pcsx2._refresh_patches_zip]


def test_ensure_does_nothing_when_the_fetch_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PCSX2_PATCHES_FETCH=false` means no network and no sudo, even with no bundle."""
    monkeypatch.setattr(pcsx2, "PATCHES_FETCH", False)
    monkeypatch.setattr(pcsx2, "_refresh_patches_zip", lambda: pytest.fail("fetched"))

    pcsx2._ensure_patches_zip()


def test_a_failed_patches_fetch_still_launches_pcsx2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No network and no sudo still boots the game, just without patches."""
    spawned: list[list[str]] = []
    calls: list[list[str]] = []
    monkeypatch.setattr(pcsx2, "_patch_ini", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_ensure_folder_card", lambda self: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_spawn", lambda self, cmd, env: spawned.append(cmd))
    monkeypatch.setattr(pcsx2.subprocess, "run", _fake_fetch_run(calls, fail="curl"))
    monkeypatch.setattr(pcsx2, "Thread", _swallow_thread)

    pcsx2.Pcsx2().launch(tmp_path / "g.iso", None)

    assert [c[0] for c in calls] == [pcsx2._CURL]
    assert len(spawned) == 1
    assert not pcsx2.PATCHES_ZIP.exists()


def test_launch_ensures_patches_before_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bundle check runs before pcsx2-qt starts, and before the old game is stopped.

    PCSX2 opens the file at boot, so a fetch after the spawn is too late; a
    fetch after the stop leaves the player on a dead stream while it runs.
    """
    order: list[str] = []
    monkeypatch.setattr(pcsx2, "_patch_ini", lambda: None)
    monkeypatch.setattr(pcsx2, "_ensure_patches_zip", lambda: order.append("patches"))
    monkeypatch.setattr(pcsx2.Pcsx2, "stop", lambda self: order.append("stop"))
    monkeypatch.setattr(pcsx2.Pcsx2, "_ensure_folder_card", lambda self: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_spawn", lambda self, cmd, env: order.append("spawn"))
    monkeypatch.setattr(pcsx2, "Thread", _swallow_thread)

    pcsx2.Pcsx2().launch(tmp_path / "g.iso", None)

    assert order == ["patches", "stop", "spawn"]


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", " Off "])
def test_the_fetch_switch_turns_off_for_every_off_spelling(value: str) -> None:
    """`0`, `no` and `off` disable the fetch too, not only the literal `false`."""
    assert pcsx2._fetch_enabled(value) is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", ""])
def test_the_fetch_switch_stays_on_otherwise(value: str) -> None:
    """Anything that is not an off spelling leaves the fetch on, its default."""
    assert pcsx2._fetch_enabled(value) is True


def test_a_launch_stops_at_an_unpatchable_ini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch whose ini patch failed never spawns pcsx2."""
    spawned = []

    def refuse() -> None:
        raise RuntimeError("no ini")

    monkeypatch.setattr(pcsx2, "_patch_ini", refuse)
    monkeypatch.setattr(pcsx2, "_ensure_patches_zip", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_ensure_folder_card", lambda self: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_spawn", lambda self, cmd, env: spawned.append(cmd))

    with pytest.raises(RuntimeError):
        pcsx2.Pcsx2().launch(tmp_path / "g.iso", None)

    assert spawned == []


def test_the_data_root_follows_the_config_variable_not_the_data_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PCSX2's tree hangs off `XDG_CONFIG_HOME`, whatever it holds.

    Probed against the container's build: a `-testconfig` run with only
    `XDG_DATA_HOME` set still built the tree under `$HOME/.config/PCSX2`.
    Following the data variable because the tree holds save states and memory
    cards would point the broker at a directory PCSX2 never writes.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert pcsx2._data_root() == tmp_path / "cfg" / "PCSX2"


def test_the_broker_directories_all_sit_under_the_data_root() -> None:
    """The ini, the states and the cards are all resolved from the one root.

    Any of them pinned somewhere else would stay put when the exported root
    moves, which is how the broker ends up patching an ini PCSX2 never opens
    or globbing a state directory nothing writes.
    """
    assert pcsx2.INI_PATH == pcsx2.DATA_DIR / "inis" / "PCSX2.ini"
    assert pcsx2.SSTATE_DIR == pcsx2.DATA_DIR / "sstates"
    assert pcsx2.MEMCARD_DIR == pcsx2.DATA_DIR / "memcards"


def test_the_save_root_is_the_data_root_the_subtrees_hang_off() -> None:
    """Restore and imports aim at the tree PCSX2 reads, wherever `XDG_CONFIG_HOME` puts it."""
    assert pcsx2.Pcsx2.save_root == pcsx2.DATA_DIR
    assert pcsx2.MEMCARD_DIR.parent == pcsx2.SSTATE_DIR.parent == pcsx2.Pcsx2.save_root


def test_a_launch_sends_pcsx2_to_the_data_root_the_broker_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch exports the XDG root PCSX2 resolves its own tree from.

    Nothing on pcsx2-qt's command line names that root, so the ini the broker
    just patched is only the one PCSX2 loads if the launch hands over the root
    the broker resolved.
    """
    data_dir = tmp_path / "xdg" / "PCSX2"
    spawned: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(pcsx2, "DATA_DIR", data_dir)
    monkeypatch.setattr(pcsx2, "_patch_ini", lambda: None)
    monkeypatch.setattr(pcsx2, "_ensure_patches_zip", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_ensure_folder_card", lambda self: None)
    monkeypatch.setattr(
        pcsx2.Pcsx2, "_spawn", lambda self, cmd, env: spawned.update(env=env)
    )
    monkeypatch.setattr(pcsx2, "Thread", _swallow_thread)

    pcsx2.Pcsx2().launch(tmp_path / "g.iso", None)

    monkeypatch.setenv("XDG_CONFIG_HOME", spawned["env"]["XDG_CONFIG_HOME"])
    assert pcsx2._data_root() == data_dir


def test_resolve_refuses_a_direct_path_that_is_a_symlink_out_of_the_library(
    rom_root: Path, tmp_path: Path
) -> None:
    """A direct path that is a symlink escaping the ROM library resolves to None."""
    outside = tmp_path / "elsewhere.iso"
    outside.write_bytes(b"iso")
    linked = rom_root / "Game.iso"
    linked.symlink_to(outside)

    assert pcsx2.Pcsx2().resolve_rom_file(linked) is None


def test_two_sessions_keep_their_own_working_slot(sstate_dir: Path) -> None:
    """Each instance resolves and targets its own slot, not a shared one."""
    first = pcsx2.Pcsx2()
    second = pcsx2.Pcsx2()
    first.state_slot = 3
    second.state_slot = 7
    mine = _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).03.p2s")
    theirs = _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).07.p2s")

    assert first.state_path() == mine
    assert second.state_path() == theirs
    assert first.state_target("SLUS-20946 (7D3A8B4E).01.p2s") == mine
    assert second.state_target("SLUS-20946 (7D3A8B4E).01.p2s") == theirs


def test_a_save_addresses_the_slot_of_the_instance_that_asked(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PINE save payload and the reported slot both come off the instance."""
    sent: list[bytes] = []
    monkeypatch.setattr(
        pcsx2, "_pine_request", lambda opcode, payload=b"", timeout=5.0: sent.append(payload) or b""
    )
    monkeypatch.setattr(
        pcsx2, "_wait_for_sstate_write", lambda before, deadline, slot=None, pid=None: True
    )
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: True)
    monkeypatch.setattr(pcsx2.Pcsx2, "stop", lambda self: None)
    emu = pcsx2.Pcsx2()
    emu.state_slot = 4
    _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).04.p2s")

    result = emu.save_and_exit(1)

    assert sent == [bytes([4])]
    assert result["state_slot"] == 4
    assert result["state_saved"] is True


def test_a_save_that_never_lands_is_discarded_instead_of_shipped(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that never settles is deleted, so the exit archive cannot ship a torn state."""
    monkeypatch.setattr(pcsx2, "_pine_request", lambda opcode, payload=b"", timeout=5.0: b"")
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: True)
    monkeypatch.setattr(pcsx2.Pcsx2, "stop", lambda self: None)
    emu = pcsx2.Pcsx2()
    emu.state_slot = 10
    survivor = _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).02.p2s")

    def half_write(
        before: dict[Path, tuple[int, float]],
        deadline: float,
        slot: Optional[int] = None,
        pid: Optional[int] = None,
    ) -> bool:
        """Leave a truncated state behind the way a stalled PCSX2 save does."""
        (sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s").write_bytes(b"torn")
        return False

    monkeypatch.setattr(pcsx2, "_wait_for_sstate_write", half_write)

    result = emu.save_and_exit(1)

    assert result["state_saved"] is False
    assert result["state_file"] is None
    assert not (sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s").exists()
    # Only what the failed save touched goes; the other slot is untouched.
    assert survivor.exists()


def test_a_state_already_in_the_slot_survives_a_failed_save(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed save never deletes the good state the slot already held."""
    monkeypatch.setattr(pcsx2, "_pine_request", lambda opcode, payload=b"", timeout=5.0: b"")
    monkeypatch.setattr(
        pcsx2, "_wait_for_sstate_write", lambda before, deadline, slot=None, pid=None: False
    )
    emu = pcsx2.Pcsx2()
    emu.state_slot = 10
    existing = _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s", mtime=1000)

    assert emu.save_state(1) is False
    assert existing.exists()


def test_a_load_is_refused_when_the_state_belongs_to_another_disc(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state captured from another disc is not loaded, however PINE would ack it."""
    loads: list[int] = []
    monkeypatch.setattr(pcsx2, "_pine_game_serial", lambda: "SLES-51234")
    monkeypatch.setattr(
        pcsx2, "_pine_request", lambda opcode, payload=b"", timeout=5.0: loads.append(opcode) or b""
    )
    emu = pcsx2.Pcsx2()
    emu.state_slot = 10
    _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s")

    assert emu.load_state(1) is False
    assert loads == []


def test_a_load_goes_through_for_the_disc_the_state_came_from(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state whose serial matches the running disc is loaded from the instance's slot."""
    sent: list[tuple[int, bytes]] = []
    monkeypatch.setattr(pcsx2, "_pine_game_serial", lambda: "SLUS-20946")
    monkeypatch.setattr(
        pcsx2,
        "_pine_request",
        lambda opcode, payload=b"", timeout=5.0: sent.append((opcode, payload)) or b"",
    )
    emu = pcsx2.Pcsx2()
    emu.state_slot = 6
    _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).06.p2s")

    assert emu.load_state(1) is True
    assert sent == [(pcsx2._PINE_MSG_LOAD_STATE, bytes([6]))]


def test_a_load_still_goes_through_when_pcsx2_names_no_disc(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable serial skips the check rather than costing the player the resume."""
    monkeypatch.setattr(pcsx2, "_pine_game_serial", lambda: None)
    monkeypatch.setattr(pcsx2, "_pine_request", lambda opcode, payload=b"", timeout=5.0: b"")
    emu = pcsx2.Pcsx2()
    emu.state_slot = 10
    _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s")

    assert emu.load_state(1) is True


def test_the_running_serial_is_read_off_the_pine_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """The game-id reply's length-prefixed, null-terminated string is decoded to a serial."""
    serial = b"SLUS-20946\x00"
    body = struct.pack("<I", len(serial)) + serial
    sock = _FakePineSocket(struct.pack("<IB", 5 + len(body), 0) + body)
    monkeypatch.setattr(pcsx2._socket, "socket", lambda family, kind: sock)

    assert pcsx2._pine_game_serial() == "SLUS-20946"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("SLUS-20946 (7D3A8B4E).10.p2s", "SLUS-20946"),
        ("SLUS-20946.10.p2s", "SLUS-20946"),
        ("card.bin", None),
    ],
)
def test_the_captured_serial_drops_the_crc_pcsx2_appends(
    filename: str, expected: Optional[str]
) -> None:
    """A state name yields the bare serial, which is what PINE reports for the disc."""
    assert pcsx2._state_serial(filename) == expected


def test_same_second_writes_are_broken_by_size_not_left_to_chance(sstate_dir: Path) -> None:
    """Two states sharing an mtime resolve to the larger, never to the truncated one."""
    torn = sstate_dir / "SLUS-1.10.p2s"
    torn.write_bytes(b"x")
    whole = sstate_dir / "SLUS-2.10.p2s"
    whole.write_bytes(b"a whole state")
    for p in (torn, whole):
        os.utime(p, (5000, 5000))

    assert pcsx2.newest_state_for_slot(10) == whole


def test_a_state_that_cannot_be_stated_is_logged_not_swallowed(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stat failure while scanning the slot leaves a log line behind."""
    _touch(sstate_dir / "SLUS-20946.10.p2s")
    real_stat = Path.stat

    def refuse(self: Path, **kwargs: object) -> os.stat_result:
        """Fail only on state files, so the directory checks around them still work."""
        if self.suffix == ".p2s":
            raise OSError("gone")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", refuse)

    with caplog.at_level("WARNING", logger=pcsx2.log.name):
        assert pcsx2.newest_state_for_slot(10) is None

    assert any("could not stat the state" in r.message for r in caplog.records)


# -- declared imports --

_ROMM_ID = imports.RomRef(1, "Game", "ps2", title_id="SLUS-20312")
"""An activate's rom, carrying the serial RomM holds for it."""

_SLOT1 = PurePosixPath("memcards", pcsx2.SLOT1_CARD_NAME)
"""Where an imported card lands, relative to `save_root`: the broker's slot-1 card."""


def _preflight(members: dict[str, bytes], **kwargs: Any) -> imports.PreflightResult:
    """Preflight an archive of import members on a fresh PCSX2, with no rom file.

    Args:
        members: `.import/<kind>/...` names mapped to bytes.
        **kwargs: Extra `preflight_import` arguments, such as `rom` or `excluded`.

    Returns:
        What preflight decided.
    """
    return preflight_import(pcsx2.Pcsx2(), import_zip(members), rom_file=None, **kwargs)


@pytest.mark.parametrize(
    "member",
    [
        ".import/save/mycard/_pcsx2_superblock",
        ".import/memcard/mycard/_pcsx2_superblock",
        ".import/save/memcards/mycard/_pcsx2_superblock",
        ".import/memcard/Mcd001.ps2/_pcsx2_superblock",
    ],
)
def test_a_folder_card_lands_as_the_slot_1_card(sstate_dir: Path, member: str) -> None:
    """A card's own folder, whatever its name, becomes the broker's slot-1 card.

    Either kind takes it, a `memcards/` wrapper is dropped, and a folder that
    keeps PCSX2's `.ps2` card name is still a folder.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
        member: The member's zip name.
    """
    result = _preflight({member: b"sb"})

    assert result.refusals == ()
    assert [p.dest for p in result.placements] == [_SLOT1 / pcsx2.SLOT1_MARKER]


def test_a_card_lands_with_its_game_folders(sstate_dir: Path) -> None:
    """Every file of the card keeps its place below the card's folder.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight(
        {
            ".import/save/mycard/_pcsx2_superblock": b"sb",
            ".import/save/mycard/BASLUS-20312/BASLUS-20312": b"save",
            ".import/save/mycard/BASLUS-20312/icon.sys": b"icon",
        }
    )

    assert result.refusals == ()
    assert sorted(p.dest for p in result.placements) == [
        _SLOT1 / "BASLUS-20312" / "BASLUS-20312",
        _SLOT1 / "BASLUS-20312" / "icon.sys",
        _SLOT1 / pcsx2.SLOT1_MARKER,
    ]


def test_a_card_holding_another_games_saves_is_taken(sstate_dir: Path) -> None:
    """One card holds every game's saves, so a folder for another serial is no mismatch.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight(
        {
            ".import/save/mycard/_pcsx2_superblock": b"sb",
            ".import/save/mycard/BESLES-50000/BESLES-50000": b"save",
        },
        rom=_ROMM_ID,
    )

    assert result.refusals == ()


@pytest.mark.parametrize(
    "member",
    [
        ".import/save/card.ps2",
        ".import/save/Card.PS2",
        ".import/memcard/Mcd001.mcd",
        ".import/save/x.max",
        ".import/save/x.psu",
        ".import/memcard/memcards/x.ps2",
    ],
)
def test_a_card_image_or_a_single_save_export_needs_converting(sstate_dir: Path, member: str) -> None:
    """A lone file is a whole card as one image, or one game's export, and the slot-1 card is a folder.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
        member: The member's zip name.
    """
    result = _preflight({member: b"card"})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("needs_conversion", "a card image or a single-save export; send the folder card instead")
    ]


@pytest.mark.parametrize(
    "member",
    [
        ".import/save/_pcsx2_superblock",
        ".import/memcard/notes.txt",
        ".import/save/memcards/_pcsx2_superblock",
    ],
)
def test_a_lone_file_that_is_no_card_is_not_recognised(sstate_dir: Path, member: str) -> None:
    """A single file with no card folder above it is no card, and no known card image either.

    A card folder named `memcards` reads as the wrapper around one, so its
    superblock is a lone file too.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
        member: The member's zip name.
    """
    result = _preflight({member: b"sb"})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("unrecognised_layout", "expected a folder card: <card>/_pcsx2_superblock and its game folders")
    ]


def test_two_cards_in_one_archive_are_refused(sstate_dir: Path) -> None:
    """Slot 1 mounts one card, so members from two card folders cannot all land in it.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight(
        {
            ".import/save/cardA/_pcsx2_superblock": b"sb",
            ".import/memcard/cardB/BASLUS-20312/BASLUS-20312": b"save",
        }
    )

    detail = "cards declared: cardA, cardB"
    assert sorted((r.member, r.reason, r.detail) for r in result.refusals) == [
        (".import/memcard/cardB/BASLUS-20312/BASLUS-20312", "destination_conflict", detail),
        (".import/save/cardA/_pcsx2_superblock", "destination_conflict", detail),
    ]


def test_two_whole_cards_refuse_each_member_once(sstate_dir: Path) -> None:
    """Two superblocks land on one file, which the shared check refuses; the card check skips them.

    The game folders are still refused as a second card: a superblock
    refused as a clash still counts toward the cards declared.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight(
        {
            ".import/save/cardA/_pcsx2_superblock": b"sb",
            ".import/save/cardA/BASLUS-20312/BASLUS-20312": b"save",
            ".import/save/cardB/_pcsx2_superblock": b"sb",
            ".import/save/cardB/BESLES-50000/BESLES-50000": b"save",
        }
    )

    same = ("destination_conflict", "another member lands on the same file")
    two = ("destination_conflict", "cards declared: cardA, cardB")
    assert sorted((r.member, r.reason, r.detail) for r in result.refusals) == [
        (".import/save/cardA/BASLUS-20312/BASLUS-20312", *two),
        (".import/save/cardA/_pcsx2_superblock", *same),
        (".import/save/cardB/BESLES-50000/BESLES-50000", *two),
        (".import/save/cardB/_pcsx2_superblock", *same),
    ]


@pytest.mark.parametrize(
    ("archived", "detail"),
    [
        ("memcards/othercard/_pcsx2_superblock", "the archive already carries a memcards/ card"),
        (f"memcards/{pcsx2.SLOT1_CARD_NAME}/_pcsx2_superblock", "another member lands on the same file"),
    ],
)
def test_a_card_beside_the_archives_own_is_refused_once(sstate_dir: Path, archived: str, detail: str) -> None:
    """An archive that already carries a card takes no imported one.

    A card elsewhere under `memcards/` would sit beside the import, mounted
    nowhere. The slot-1 card's own superblock is the same file as the
    import's, which the shared check refuses, so the card check adds nothing.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
        archived: The archive's own card member.
        detail: The refusal's detail.
    """
    body = import_zip({".import/save/mycard/_pcsx2_superblock": b"sb"}, v1={archived: b"old"})

    result = preflight_import(pcsx2.Pcsx2(), body, rom_file=None)

    assert [(r.reason, r.detail) for r in result.refusals] == [("destination_conflict", detail)]


def test_a_card_without_its_superblock_is_not_a_whole_card(sstate_dir: Path) -> None:
    """PCSX2 skips a folder with no `_pcsx2_superblock`, so game folders alone are no card.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight({".import/save/mycard/BASLUS-20312/BASLUS-20312": b"save"})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("shape_unverified", "no _pcsx2_superblock: not a whole folder card")
    ]


def test_a_card_with_an_empty_superblock_is_incomplete(sstate_dir: Path) -> None:
    """An empty superblock is a card cut short in the copy.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight({".import/save/mycard/_pcsx2_superblock": b""})

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("incomplete_unit", "the card's _pcsx2_superblock is empty")
    ]


def test_a_card_synced_on_its_own_routes_is_refused_for_that_alone(sstate_dir: Path) -> None:
    """With the card on the whole-card routes, an imported one is refused, and its shape is moot.

    This card has no superblock, which would otherwise be `shape_unverified`.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight(
        {".import/memcard/mycard/BASLUS-20312/BASLUS-20312": b"save"},
        excluded=("memcards",),
        memory_card_synced=True,
    )

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("memcard_synced_separately", "the card travels on its own routes this session")
    ]


def test_a_declared_state_is_pointed_at_the_push_route(sstate_dir: Path) -> None:
    """PCSX2 takes states on the push route only, so a declared one is sent there.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight({".import/state/SLUS-20312 (ABCDEF12).01.p2s": b"progress"})

    assert [(r.reason, r.detail) for r in result.refusals] == [("state_uses_push", None)]


def test_the_spec_takes_one_card_shape_under_either_kind_and_pushes_states() -> None:
    """A player calls the card a save or a memory card, so both kinds take the same shape."""
    spec = pcsx2.Pcsx2().import_spec()

    assert [k.kind for k in spec.kinds] == ["save", "memcard"]
    assert spec.kinds[0].shapes == spec.kinds[1].shapes
    assert (spec.state_channel, spec.card_subtree) == ("push", "memcards")


def test_a_push_after_an_import_is_held_to_the_sessions_serial(sstate_dir: Path) -> None:
    """Preflight records the session's serial, and the push route refuses a state named for another.

    A name whose serial does not normalise is taken on trust, as every push
    was before.

    Args:
        sstate_dir: The patched state directory.
    """
    emu = pcsx2.Pcsx2()
    body = import_zip({".import/save/mycard/_pcsx2_superblock": b"sb"})

    result = preflight_import(emu, body, rom_file=None, rom=_ROMM_ID)

    assert result.refusals == ()
    assert emu.import_identity == imports.SessionIdentity("SLUS-20312", "romm")
    assert emu.state_target("SLES-50000 (12345678).03.p2s") is None
    slot = f"{emu.state_slot:02d}"
    for stem in ("SLUS-20312 (ABCDEF12)", "HOMEBREW (1234ABCD)"):
        assert emu.state_target(f"{stem}.03.p2s") == sstate_dir / f"{stem}.{slot}.p2s"


@pytest.mark.parametrize("suffix", [".mc2", ".mcr", ".bin", ".cbs", ".xps", ".sps"])
def test_every_card_image_and_export_suffix_needs_converting(sstate_dir: Path, suffix: str) -> None:
    """Each suffix the spec names for a card image or a single-save export is refused the same way.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
        suffix: The lone file's suffix.
    """
    result = _preflight({f".import/save/card{suffix}": b"card"})

    assert [r.reason for r in result.refusals] == ["needs_conversion"]


def test_a_card_image_suffix_inside_a_folder_card_is_the_cards_own_data(sstate_dir: Path) -> None:
    """A file below the card's folder lands with the card, whatever its suffix.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    result = _preflight(
        {
            ".import/save/mycard/_pcsx2_superblock": b"sb",
            ".import/save/mycard/BASLUS-20312/backup.ps2": b"save",
        }
    )

    assert result.refusals == ()
    assert sorted(p.dest for p in result.placements) == [
        _SLOT1 / "BASLUS-20312" / "backup.ps2",
        _SLOT1 / pcsx2.SLOT1_MARKER,
    ]


def test_an_archive_card_is_refused_before_a_missing_superblock(sstate_dir: Path) -> None:
    """The plan checks run in order, so a card beside the archive's own is refused for that first.

    Args:
        sstate_dir: The patched state directory; its fixture also points the save root at tmp_path.
    """
    body = import_zip(
        {".import/save/mycard/BASLUS-20312/BASLUS-20312": b"save"},
        v1={"memcards/othercard/_pcsx2_superblock": b"old"},
    )

    result = preflight_import(pcsx2.Pcsx2(), body, rom_file=None)

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("destination_conflict", "the archive already carries a memcards/ card")
    ]


def test_a_push_after_an_imported_card_keeps_the_occupied_slot_rule(sstate_dir: Path) -> None:
    """An imported card leaves the push route as it was: the state in the slot is the name to match.

    PCSX2 takes no imported state, so the slot's state is the first push
    after the import. A later push of the same capture under another slot
    restamps onto it, and another stem or another serial is refused.

    Args:
        sstate_dir: The patched state directory.
    """
    emu = pcsx2.Pcsx2()
    members = {
        ".import/save/mycard/_pcsx2_superblock": b"sb",
        ".import/save/mycard/BASLUS-20312/BASLUS-20312": b"save",
    }
    result = _preflight(members, rom=_ROMM_ID)

    report = restore_import(emu, import_zip(members), result)
    emu.import_identity = result.identity

    card = pcsx2.MEMCARD_DIR / pcsx2.SLOT1_CARD_NAME
    assert (report["imported"], report["failed"]) == (2, 0)
    assert (card / pcsx2.SLOT1_MARKER).read_bytes() == b"sb"
    assert (card / "BASLUS-20312" / "BASLUS-20312").read_bytes() == b"save"
    first = emu.state_target("SLUS-20312 (ABCDEF12).03.p2s")
    assert first == sstate_dir / f"SLUS-20312 (ABCDEF12).{emu.state_slot:02d}.p2s"
    first.write_bytes(b"progress")
    assert emu.state_target("SLUS-20312 (ABCDEF12).07.p2s") == first
    assert emu.state_target("SLUS-20312 (12345678).07.p2s") is None
    assert emu.state_target("HOMEBREW (1234ABCD).07.p2s") is None
    assert emu.state_target("SLES-50000 (12345678).07.p2s") is None


def test_a_push_refused_for_another_serial_logs_both_ids_and_the_override(
    sstate_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log line tells an identity refusal from a bad name: both serials, RomM as the source, and the fix.

    Args:
        sstate_dir: The patched state directory.
        caplog: The pytest log capture fixture.
    """
    emu = pcsx2.Pcsx2()
    emu.import_identity = imports.SessionIdentity("SLUS-20312", "romm")

    with caplog.at_level("WARNING"):
        assert emu.state_target("SLES-50000 (12345678).03.p2s") is None

    assert (
        "pcsx2: refusing pushed state SLES-50000 (12345678).03.p2s, which names another game:"
        " member SLES-50000, session SLUS-20312 (from romm)"
        " - fix via PUT /api/roms/{id}/identity if RomM is wrong"
    ) in caplog.text
