"""PCSX2 state naming and slot resolution.

Also covers the memory card contract and the boot watchdog that flags a VM
that never comes up.
"""

import os
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import pcsx2


@pytest.fixture
def sstate_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the PCSX2 state directory at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The state directory.
    """
    d = tmp_path / "sstates"
    d.mkdir()
    monkeypatch.setattr(pcsx2, "SSTATE_DIR", d)
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


@pytest.mark.parametrize("filename", ["../escape.01.p2s", "", ".", "..", "card.bin"])
def test_state_target_refuses_a_name_pcsx2_would_never_write(sstate_dir: Path, filename: str) -> None:
    """A push whose name PCSX2 would never write is refused."""
    assert pcsx2.Pcsx2().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the working slot removes its state and keeps the other slots."""
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)
    stale = _touch(sstate_dir / "SLUS-20946.10.p2s")
    other = _touch(sstate_dir / "SLUS-20946.02.p2s")

    pcsx2.Pcsx2().clear_working_slot()

    assert not stale.exists()
    assert other.exists()


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


def test_a_launch_stops_at_an_unpatchable_ini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch whose ini patch failed never spawns pcsx2."""
    spawned = []

    def refuse() -> None:
        raise RuntimeError("no ini")

    monkeypatch.setattr(pcsx2, "_patch_ini", refuse)
    monkeypatch.setattr(pcsx2.Pcsx2, "_ensure_folder_card", lambda self: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_spawn", lambda self, cmd, env: spawned.append(cmd))

    with pytest.raises(RuntimeError):
        pcsx2.Pcsx2().launch(tmp_path / "g.iso", None)

    assert spawned == []


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
    """Each instance resolves, targets and clears its own slot, not a shared one."""
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

    first.clear_working_slot()

    assert not mine.exists()
    assert theirs.exists()


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
