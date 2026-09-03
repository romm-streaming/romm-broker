"""Tests for the RPCS3 emulator module.

Covers config/ipc patching, the savestates symlink, save_subtrees, resume
target selection, save-and-exit, and boot verification.
"""

import logging
import os
import shlex
import signal
import socket
import struct
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, NoReturn, Optional

import pytest

from webstation_broker.emulators import rpcs3


@pytest.fixture
def rpcs3_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point the RPCS3 emulator's dev_hdd0 layout at isolated temp directories."""
    data_dir = tmp_path / "data"
    dev_hdd0 = data_dir / "dev_hdd0"
    user_home = dev_hdd0 / "home" / "00000001"
    game_dir = dev_hdd0 / "game"
    sstate_root = data_dir / "savestates"
    sstate_link = dev_hdd0 / "savestates"
    dev_hdd0.mkdir(parents=True)
    game_dir.mkdir()
    (user_home / "savedata").mkdir(parents=True)

    monkeypatch.setattr(rpcs3, "DATA_DIR", data_dir)
    monkeypatch.setattr(rpcs3, "CONFIG_PATH", data_dir / "config.yml")
    monkeypatch.setattr(rpcs3, "IPC_PATH", data_dir / "ipc.yml")
    monkeypatch.setattr(rpcs3, "DEV_HDD0", dev_hdd0)
    monkeypatch.setattr(rpcs3, "USER_HOME", user_home)
    monkeypatch.setattr(rpcs3, "EXDATA_DIR", user_home / "exdata")
    monkeypatch.setattr(rpcs3, "GAME_DIR", game_dir)
    monkeypatch.setattr(rpcs3, "SSTATE_ROOT", sstate_root)
    monkeypatch.setattr(rpcs3, "_SSTATE_LINK", sstate_link)
    monkeypatch.setattr(rpcs3.Rpcs3, "save_root", dev_hdd0)
    return {
        "data_dir": data_dir,
        "dev_hdd0": dev_hdd0,
        "user_home": user_home,
        "game_dir": game_dir,
        "sstate_root": sstate_root,
        "sstate_link": sstate_link,
    }


def _touch(path: Path, mtime: Optional[float] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_pkg(path: Path, title_id: str) -> Path:
    """Write a .pkg whose header carries `title_id` in its content id.

    Only the 0x60-byte header `_pkg_title_id` reads is real; the payload a
    genuine PKG carries after it is irrelevant to every code path under test.
    """
    header = bytearray(0x60)
    header[0:4] = b"\x7fPKG"
    content_id = f"UP0001-{title_id}_00-0000000000000000".encode("ascii")
    header[0x30 : 0x30 + len(content_id)] = content_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header))
    return path


# ── config.yml / ipc.yml patching ───────────────────────────────────────


def test_patch_config_seeds_a_missing_file_with_every_forced_key(rpcs3_dirs: dict[str, Path]) -> None:
    """Patch config seeds a missing file with every forced key."""
    rpcs3._patch_config()

    text = rpcs3.CONFIG_PATH.read_text()
    assert "Miscellaneous:" in text
    assert "  Automatically start games after boot: true" in text
    assert "  Exit RPCS3 when process finishes: true" in text
    assert "  Pause emulation on RPCS3 focus loss: false" in text


def test_patch_config_overwrites_a_conflicting_value_but_keeps_the_rest(rpcs3_dirs: dict[str, Path]) -> None:
    """Patch config overwrites a conflicting value but keeps the rest."""
    rpcs3.CONFIG_PATH.write_text(
        "Miscellaneous:\n"
        "  Exit RPCS3 when process finishes: false\n"
        "  Some Other Setting: 5\n"
    )

    rpcs3._patch_config()

    text = rpcs3.CONFIG_PATH.read_text()
    assert "  Exit RPCS3 when process finishes: true" in text
    assert "  Some Other Setting: 5" in text


def test_patch_ipc_seeds_a_missing_file_as_a_flat_key(rpcs3_dirs: dict[str, Path]) -> None:
    """Patch IPC seeds a missing file as a flat key."""
    rpcs3._patch_ipc()

    assert rpcs3.IPC_PATH.read_text().strip() == "IPC Server enabled: true"


def test_patch_ipc_overwrites_an_existing_flat_key(rpcs3_dirs: dict[str, Path]) -> None:
    """Patch IPC overwrites an existing flat key."""
    rpcs3.IPC_PATH.write_text("IPC Server enabled: false\nIPC Port: 28080\n")

    rpcs3._patch_ipc()

    text = rpcs3.IPC_PATH.read_text()
    assert "IPC Server enabled: true" in text
    assert "IPC Port: 28080" in text


# ── savestates symlink ──────────────────────────────────────────────────


def test_ensure_sstate_link_creates_a_fresh_symlink(rpcs3_dirs: dict[str, Path]) -> None:
    """Ensure sstate link creates a fresh symlink."""
    rpcs3._ensure_sstate_link()

    link = rpcs3_dirs["sstate_link"]
    assert link.is_symlink()
    assert link.resolve() == rpcs3_dirs["sstate_root"].resolve()


def test_ensure_sstate_link_is_idempotent(rpcs3_dirs: dict[str, Path]) -> None:
    """Ensure sstate link is idempotent."""
    rpcs3._ensure_sstate_link()
    rpcs3._ensure_sstate_link()

    assert rpcs3_dirs["sstate_link"].resolve() == rpcs3_dirs["sstate_root"].resolve()


def test_ensure_sstate_link_repoints_a_symlink_at_the_wrong_target(
    rpcs3_dirs: dict[str, Path], tmp_path: Path
) -> None:
    """Ensure sstate link repoints a symlink at the wrong target."""
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    rpcs3_dirs["sstate_link"].symlink_to(wrong, target_is_directory=True)

    rpcs3._ensure_sstate_link()

    assert rpcs3_dirs["sstate_link"].resolve() == rpcs3_dirs["sstate_root"].resolve()


def test_ensure_sstate_link_leaves_a_real_directory_alone(rpcs3_dirs: dict[str, Path]) -> None:
    """Ensure sstate link leaves a real directory alone."""
    real = rpcs3_dirs["sstate_link"]
    real.mkdir()
    (real / "marker").write_text("do not touch")

    rpcs3._ensure_sstate_link()

    assert not real.is_symlink()
    assert (real / "marker").exists()


def test_clearing_the_working_slot_ensures_the_symlink(rpcs3_dirs: dict[str, Path]) -> None:
    """clear_working_slot creates the savestates symlink even with no prior state."""
    rpcs3.Rpcs3().clear_working_slot()

    assert rpcs3_dirs["sstate_link"].is_symlink()


def test_building_every_emulator_does_not_touch_the_symlink(rpcs3_dirs: dict[str, Path]) -> None:
    """Construction alone must not create the savestates symlink.

    clear_working_slot, not __init__/save_subtrees, is what creates the link:
    a registry sweep constructs every emulator against real paths with no
    per-emulator redirect, so construction alone must stay read-only.
    """
    rpcs3.Rpcs3()

    assert not rpcs3_dirs["sstate_link"].exists()


def test_clearing_the_working_slot_drops_the_incoming_titles_leftover_state(
    rpcs3_dirs: dict[str, Path], tmp_path: Path
) -> None:
    """A stale state from a previous session must not outrank a fresh restore.

    A state left behind by a previous session must not outrank (by mtime)
    whatever this session's own archive restore brings back.
    """
    stale = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    pkg = _write_pkg(tmp_path / "roms" / "game.pkg", "BLUS30443")

    emu = rpcs3.Rpcs3()
    assert emu.resolve_rom_file(pkg) == pkg
    emu.clear_working_slot()

    assert not stale.exists()
    assert rpcs3_dirs["sstate_root"].is_dir()


def test_clearing_the_working_slot_spares_every_other_titles_state(
    rpcs3_dirs: dict[str, Path], tmp_path: Path
) -> None:
    """Another title's state may not have been archived yet, so it must survive."""
    other_title = _touch(rpcs3_dirs["sstate_root"] / "OTHER0000" / "OTHER0000_1.SAVESTAT")
    pkg = _write_pkg(tmp_path / "roms" / "game.pkg", "BLUS30443")

    emu = rpcs3.Rpcs3()
    emu.resolve_rom_file(pkg)
    emu.clear_working_slot()

    assert other_title.exists()


def test_clearing_the_working_slot_keeps_states_when_the_title_is_unknown(
    rpcs3_dirs: dict[str, Path], tmp_path: Path
) -> None:
    """A bare .iso names no title, so the clear has nothing to scope to."""
    kept = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    iso = tmp_path / "roms" / "game.iso"
    iso.parent.mkdir(parents=True)
    iso.write_bytes(b"iso")

    emu = rpcs3.Rpcs3()
    emu.resolve_rom_file(iso)
    emu.clear_working_slot()

    assert kept.exists()


def test_clearing_the_working_slot_keeps_states_without_a_resolved_rom(
    rpcs3_dirs: dict[str, Path],
) -> None:
    """No resolved boot target means no title to clear for, so nothing goes."""
    kept = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")

    rpcs3.Rpcs3().clear_working_slot()

    assert kept.exists()


def test_clearing_the_working_slot_snapshots_leftovers_when_the_title_is_unknown(
    rpcs3_dirs: dict[str, Path], tmp_path: Path
) -> None:
    """An unknown title id must still record a pre-restore snapshot to diff against later.

    clear_working_slot cannot scope a clear without a title id, but the
    files sitting under SSTATE_ROOT right now are exactly what a later
    deferred clear needs to tell a leftover apart from this session's
    restore, once PINE names the title.
    """
    leftover = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    iso = tmp_path / "roms" / "game.iso"
    iso.parent.mkdir(parents=True)
    iso.write_bytes(b"iso")

    emu = rpcs3.Rpcs3()
    emu.resolve_rom_file(iso)
    emu.clear_working_slot()

    assert emu._leftover_snapshot == {leftover: (leftover.stat().st_size, leftover.stat().st_mtime)}


def test_clearing_the_working_slot_records_no_snapshot_when_the_title_is_known(
    rpcs3_dirs: dict[str, Path], tmp_path: Path
) -> None:
    """A known title id already scopes its own clear, so no deferred snapshot is needed."""
    pkg = _write_pkg(tmp_path / "roms" / "game.pkg", "BLUS30443")

    emu = rpcs3.Rpcs3()
    emu.resolve_rom_file(pkg)
    emu.clear_working_slot()

    assert emu._leftover_snapshot is None


def test_clearing_the_working_slot_enters_restoring_mode(rpcs3_dirs: dict[str, Path]) -> None:
    """clear_working_slot must flip _restoring itself, not prepare_restore().

    api.py reads save_subtrees for the restore extract before it calls
    prepare_restore(), so clear_working_slot has to flip _restoring itself.
    """
    emu = rpcs3.Rpcs3()

    emu.clear_working_slot()

    assert emu._restoring is True
    assert emu.save_subtrees == ("home/00000001/savedata", "game", "savestates")


# ── save_subtrees ───────────────────────────────────────────────────────


def test_save_subtrees_includes_savestates_when_dumping(rpcs3_dirs: dict[str, Path]) -> None:
    """Save subtrees includes savestates when dumping."""
    emu = rpcs3.Rpcs3()

    assert "savestates" in emu.save_subtrees
    assert "home/00000001/savedata" in emu.save_subtrees


def test_save_subtrees_includes_savestates_when_restoring(rpcs3_dirs: dict[str, Path]) -> None:
    """Save subtrees includes savestates when restoring."""
    emu = rpcs3.Rpcs3()
    emu._restoring = True

    assert emu.save_subtrees == ("home/00000001/savedata", "game", "savestates")


# ── state snapshot / diff ───────────────────────────────────────────────


def test_newest_state_reads_the_newest_file_for_the_title(rpcs3_dirs: dict[str, Path]) -> None:
    """Newest state reads the newest file for the title."""
    _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT", mtime=1000)
    newest = _touch(
        rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_2.SAVESTAT", mtime=3000
    )
    _touch(rpcs3_dirs["sstate_root"] / "OTHER00000" / "OTHER00000_9.SAVESTAT", mtime=9000)

    assert rpcs3._newest_state("BLUS30443") == newest


def test_newest_state_is_none_without_a_title_dir(rpcs3_dirs: dict[str, Path]) -> None:
    """Newest state is none without a title dir."""
    assert rpcs3._newest_state("BLUS30443") is None
    assert rpcs3._newest_state(None) is None


@pytest.mark.parametrize("ext", [".SAVESTAT", ".SAVESTAT.zst", ".SAVESTAT.gz"])
def test_newest_state_matches_every_extension_rpcs3_actually_writes(
    rpcs3_dirs: dict[str, Path], ext: str
) -> None:
    """Newest state matches every extension RPCS3 actually writes."""
    state = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / f"BLUS30443_1{ext}")

    assert rpcs3._newest_state("BLUS30443") == state


def test_changed_state_finds_the_file_that_appeared(rpcs3_dirs: dict[str, Path]) -> None:
    """Changed state finds the file that appeared."""
    before = rpcs3._state_snapshot("BLUS30443")
    new = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")

    assert rpcs3._changed_state("BLUS30443", before) == new


def test_changed_state_finds_a_rewritten_file_by_size(rpcs3_dirs: dict[str, Path]) -> None:
    """Changed state finds a rewritten file by size."""
    p = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    before = rpcs3._state_snapshot("BLUS30443")
    p.write_bytes(b"a longer state than before")

    assert rpcs3._changed_state("BLUS30443", before) == p


def test_changed_state_is_none_when_nothing_moved(rpcs3_dirs: dict[str, Path]) -> None:
    """Changed state is none when nothing moved."""
    _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    before = rpcs3._state_snapshot("BLUS30443")

    assert rpcs3._changed_state("BLUS30443", before) is None


def test_changed_state_picks_the_newest_when_several_files_changed(rpcs3_dirs: dict[str, Path]) -> None:
    """_changed_state must pick the newest file, not just any changed file.

    Must agree with _newest_state, which resume selection relies on, not
    just report whichever changed file the directory glob happens to yield
    first.
    """
    before = rpcs3._state_snapshot("BLUS30443")
    _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT", mtime=1000)
    newest = _touch(
        rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_2.SAVESTAT", mtime=3000
    )

    assert rpcs3._changed_state("BLUS30443", before) == newest


def test_all_state_files_spans_every_title_dir(rpcs3_dirs: dict[str, Path]) -> None:
    """All state files spans every title dir."""
    a = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT", mtime=1000)
    b = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_2.SAVESTAT.zst", mtime=2000)
    c = _touch(rpcs3_dirs["sstate_root"] / "OTHER00000" / "OTHER00000_1.SAVESTAT.gz", mtime=3000)

    snap = rpcs3._all_state_files()

    assert set(snap) == {a, b, c}
    assert snap[c] == (a.stat().st_size, 3000)


def test_all_state_files_is_empty_when_the_root_does_not_exist(
    rpcs3_dirs: dict[str, Path]
) -> None:
    """A savestates root that was never created must read as no states, not raise."""
    assert not rpcs3_dirs["sstate_root"].exists()

    assert rpcs3._all_state_files() == {}


def test_all_state_files_ignores_files_sitting_directly_in_the_root(
    rpcs3_dirs: dict[str, Path]
) -> None:
    """Only title dirs hold states, so a loose file in the root must be skipped."""
    rpcs3_dirs["sstate_root"].mkdir(parents=True)
    (rpcs3_dirs["sstate_root"] / "stray.SAVESTAT").write_bytes(b"state")
    kept = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")

    assert set(rpcs3._all_state_files()) == {kept}


def test_all_state_files_reports_a_root_it_cannot_list(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unlistable savestates root must log and read as no states, not raise."""
    rpcs3_dirs["sstate_root"].mkdir(parents=True)

    def deny(_self: Path) -> NoReturn:
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "iterdir", deny)

    with caplog.at_level(logging.WARNING):
        assert rpcs3._all_state_files() == {}

    assert "could not list" in caplog.text


# ── deferred leftover-savestate clear ───────────────────────────────────


def test_clear_leftover_states_drops_files_unchanged_since_the_snapshot(
    rpcs3_dirs: dict[str, Path]
) -> None:
    """A file that matches the pre-restore snapshot exactly must be dropped."""
    stale = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT", mtime=1000)
    before = rpcs3._state_snapshot("BLUS30443")

    rpcs3._clear_leftover_states("BLUS30443", before)

    assert not stale.exists()


def test_clear_leftover_states_keeps_a_file_the_restore_changed(
    rpcs3_dirs: dict[str, Path]
) -> None:
    """A file whose size/mtime moved since the snapshot was taken must be kept.

    The restore can overwrite a leftover in place with a state carrying an
    older archived mtime, so identity, not recency, is what tells a kept
    file apart from a stale one.
    """
    path = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT", mtime=1000)
    before = rpcs3._state_snapshot("BLUS30443")
    _touch(path, mtime=500)  # restore overwrote it with an older archived save

    rpcs3._clear_leftover_states("BLUS30443", before)

    assert path.exists()


def test_clear_leftover_states_keeps_a_file_the_restore_added(
    rpcs3_dirs: dict[str, Path]
) -> None:
    """A file absent from the pre-restore snapshot must be kept, not swept."""
    before = rpcs3._state_snapshot("BLUS30443")
    added = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_2.SAVESTAT")

    rpcs3._clear_leftover_states("BLUS30443", before)

    assert added.exists()


def test_clear_leftover_states_is_a_noop_with_an_empty_snapshot(
    rpcs3_dirs: dict[str, Path]
) -> None:
    """A known title id with nothing recorded beforehand must touch nothing."""
    kept = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")

    rpcs3._clear_leftover_states("BLUS30443", {})

    assert kept.exists()


def test_clear_leftover_states_logs_a_file_it_cannot_remove(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unremovable leftover must be logged, not raise past the caller."""
    stale = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT", mtime=1000)
    before = rpcs3._state_snapshot("BLUS30443")

    def deny(_self: Path) -> NoReturn:
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "unlink", deny)

    with caplog.at_level(logging.WARNING):
        rpcs3._clear_leftover_states("BLUS30443", before)

    assert "could not clear leftover savestate" in caplog.text
    assert stale.exists()


# ── launch process group / open descriptors ─────────────────────────────

needs_procfs = pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(), reason="requires a Linux procfs"
)


def _run_in_own_session(script: str) -> "subprocess.Popen[bytes]":
    """Start `script` under `sh` in a session of its own, the way `_spawn` does."""
    return subprocess.Popen(
        ["sh", "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    """Tear down a whole test launch, children included."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        proc.kill()
    proc.wait(timeout=10)


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    """Poll `predicate` until it holds or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_launch_pids_is_empty_without_a_process_handle() -> None:
    """No handle means no pids to inspect, not a crash."""
    assert rpcs3._launch_pids(None) == []


@needs_procfs
def test_launch_pids_finds_children_of_the_group_leader() -> None:
    """The AppImage wrapper's children must be found, not just the leader.

    RPCS3_BIN is an AppRun wrapper, so the pid the broker holds is never the
    process that writes a savestate; the whole process group has to come back.
    """
    proc = _run_in_own_session("sleep 30 & wait")
    try:
        assert _wait_until(lambda: len(rpcs3._launch_pids(proc.pid)) > 1)
        pids = rpcs3._launch_pids(proc.pid)
        assert proc.pid in pids
        assert all(os.getpgid(p) == proc.pid for p in pids)
    finally:
        _kill_group(proc)


@needs_procfs
def test_launch_pids_falls_back_to_the_leader_when_it_is_already_gone() -> None:
    """A pid that has exited still reports itself, and never raises."""
    proc = _run_in_own_session("exit 0")
    proc.wait(timeout=10)

    # The Popen handle is reaped, so the pid resolves to nothing at all.
    assert rpcs3._launch_pids(proc.pid) in ([proc.pid], [])


def test_launch_pids_falls_back_to_the_leader_when_proc_cannot_be_scanned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable /proc must degrade to the leader alone, not raise."""

    def deny(_self: Path) -> NoReturn:
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "iterdir", deny)

    with caplog.at_level(logging.DEBUG, logger=rpcs3.log.name):
        assert rpcs3._launch_pids(os.getpid()) == [os.getpid()]

    assert "could not scan /proc" in caplog.text


@needs_procfs
def test_launch_pids_skips_a_process_that_exits_mid_scan(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pid that disappears between listing /proc and reading it must be skipped."""
    real_getpgid = os.getpgid
    mine = os.getpid()

    def vanishing(pid: int) -> int:
        if pid != mine:
            raise ProcessLookupError(pid)
        return real_getpgid(pid)

    monkeypatch.setattr(rpcs3.os, "getpgid", vanishing)

    assert rpcs3._launch_pids(mine) == [mine]


def test_holds_open_is_false_without_a_process_handle(tmp_path: Path) -> None:
    """No handle means the descriptor test cannot contribute, so it says False."""
    target = tmp_path / "BLUS30443_1.SAVESTAT"
    target.write_bytes(b"state")

    assert rpcs3._holds_open(None, target) is False


@needs_procfs
def test_holds_open_sees_a_descriptor_the_test_process_itself_holds(tmp_path: Path) -> None:
    """An open descriptor on the state file reads as a write still in flight."""
    target = tmp_path / "BLUS30443_1.SAVESTAT"
    target.write_bytes(b"state")

    with target.open("rb"):
        assert rpcs3._holds_open(os.getpid(), target) is True
    assert rpcs3._holds_open(os.getpid(), target) is False


@needs_procfs
def test_holds_open_sees_a_descriptor_held_by_a_child_of_the_leader(tmp_path: Path) -> None:
    """The wrapper's child, not the leader, is what holds the state file open."""
    target = tmp_path / "BLUS30443_1.SAVESTAT"
    target.write_bytes(b"state")
    quoted = shlex.quote(str(target))
    proc = _run_in_own_session(f"sh -c 'exec 3<{quoted}; sleep 30' & wait")
    try:
        assert _wait_until(lambda: rpcs3._holds_open(proc.pid, target))
        # The leader never opened the file, so only the group scan can see the write.
        leader_fds = {os.path.realpath(fd) for fd in Path(f"/proc/{proc.pid}/fd").iterdir()}
        assert os.path.realpath(target) not in leader_fds
    finally:
        _kill_group(proc)


@needs_procfs
def test_holds_open_resolves_the_state_file_through_a_symlink(tmp_path: Path) -> None:
    """dev_hdd0/savestates is a symlink, so both spellings must compare equal."""
    real = tmp_path / "real"
    real.mkdir()
    target = real / "BLUS30443_1.SAVESTAT"
    target.write_bytes(b"state")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with target.open("rb"):
        assert rpcs3._holds_open(os.getpid(), link / target.name) is True


def test_holds_open_is_false_when_the_descriptor_list_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A /proc entry that vanishes or is unreadable must log and read as False."""
    target = tmp_path / "BLUS30443_1.SAVESTAT"
    target.write_bytes(b"state")
    monkeypatch.setattr(rpcs3, "_launch_pids", lambda _pid: [4242])

    def deny(_self: Path) -> NoReturn:
        raise FileNotFoundError("gone")

    monkeypatch.setattr(Path, "iterdir", deny)

    with caplog.at_level(logging.DEBUG, logger=rpcs3.log.name):
        assert rpcs3._holds_open(4242, target) is False

    assert "could not read the open files" in caplog.text


# ── save state write confirmation ───────────────────────────────────────


class _SleepClock:
    """A monotonic clock that only advances when the code under test sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def sleep_clock(monkeypatch: pytest.MonkeyPatch) -> _SleepClock:
    """Drive `_wait_for_state_write`'s poll loop without real sleeping."""
    clock = _SleepClock()
    monkeypatch.setattr(rpcs3.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(rpcs3.time, "sleep", clock.sleep)
    return clock


def test_wait_for_state_write_returns_the_file_once_its_size_stops_changing(
    rpcs3_dirs: dict[str, Path]
) -> None:
    """Wait for state write returns the file once its size stops changing."""
    serial = "BLUS30443"
    before = rpcs3._state_snapshot(serial)
    target = _touch(rpcs3_dirs["sstate_root"] / serial / f"{serial}_1.SAVESTAT")

    result = rpcs3._wait_for_state_write(serial, before, time.monotonic() + 5.0)

    assert result == target


def test_wait_for_state_write_returns_none_when_the_file_never_stabilizes(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file still growing past the deadline must be reported as unsaved.

    A file that keeps growing past the deadline must be reported as
    unsaved, not handed back as if the write had finished.
    """
    monkeypatch.setattr(rpcs3.time, "sleep", lambda _seconds: None)
    serial = "BLUS30443"
    target = _touch(rpcs3_dirs["sstate_root"] / serial / f"{serial}_1.SAVESTAT")
    counter = {"n": 0}

    def growing_changed_state(_serial: Optional[str], _before: dict) -> Path:
        counter["n"] += 1
        target.write_bytes(b"x" * counter["n"])
        return target

    monkeypatch.setattr(rpcs3, "_changed_state", growing_changed_state)

    result = rpcs3._wait_for_state_write(serial, {}, time.monotonic() + 0.2)

    assert result is None
    assert counter["n"] > 1


def test_wait_for_state_write_returns_none_when_nothing_ever_appears(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wait for state write returns none when nothing ever appears."""
    monkeypatch.setattr(rpcs3.time, "sleep", lambda _seconds: None)

    result = rpcs3._wait_for_state_write("BLUS30443", {}, time.monotonic() + 0.1)

    assert result is None


def test_wait_for_state_write_never_hands_back_a_zero_byte_state(
    rpcs3_dirs: dict[str, Path], sleep_clock: _SleepClock
) -> None:
    """A created-but-never-written state holds a steady size of nothing.

    Size stability alone would call that finished and archive an empty file.
    """
    serial = "BLUS30443"
    before = rpcs3._state_snapshot(serial)
    empty = rpcs3_dirs["sstate_root"] / serial / f"{serial}_1.SAVESTAT"
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")

    result = rpcs3._wait_for_state_write(serial, before, sleep_clock.monotonic() + 5.0)

    assert result is None


def test_wait_for_state_write_waits_while_rpcs3_still_holds_the_file_open(
    rpcs3_dirs: dict[str, Path], sleep_clock: _SleepClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A steady size over an open descriptor is a stalled write, not a finished one.

    RPCS3 compresses a state as it writes it, so a busy host can hold the
    size still for a whole poll window mid-write.
    """
    serial = "BLUS30443"
    before = rpcs3._state_snapshot(serial)
    target = _touch(rpcs3_dirs["sstate_root"] / serial / f"{serial}_1.SAVESTAT")
    calls = {"n": 0}

    def open_for_a_while(pid: Optional[int], path: Path) -> bool:
        assert pid == 4242
        assert path == target
        calls["n"] += 1
        return calls["n"] < 4

    monkeypatch.setattr(rpcs3, "_holds_open", open_for_a_while)

    result = rpcs3._wait_for_state_write(serial, before, sleep_clock.monotonic() + 30.0, 4242)

    assert result == target
    assert calls["n"] == 4


def test_wait_for_state_write_times_out_while_the_file_is_still_held_open(
    rpcs3_dirs: dict[str, Path], sleep_clock: _SleepClock, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A descriptor never released before the deadline must report no save."""
    serial = "BLUS30443"
    before = rpcs3._state_snapshot(serial)
    _touch(rpcs3_dirs["sstate_root"] / serial / f"{serial}_1.SAVESTAT")
    monkeypatch.setattr(rpcs3, "_holds_open", lambda _pid, _path: True)

    with caplog.at_level(logging.WARNING):
        result = rpcs3._wait_for_state_write(serial, before, sleep_clock.monotonic() + 5.0, 4242)

    assert result is None
    assert "never settled" in caplog.text


def test_wait_for_state_write_falls_back_to_size_alone_without_a_pid(
    rpcs3_dirs: dict[str, Path], sleep_clock: _SleepClock
) -> None:
    """No process handle leaves the size test as the only confirmation there is."""
    serial = "BLUS30443"
    before = rpcs3._state_snapshot(serial)
    target = _touch(rpcs3_dirs["sstate_root"] / serial / f"{serial}_1.SAVESTAT")

    result = rpcs3._wait_for_state_write(serial, before, sleep_clock.monotonic() + 5.0, None)

    assert result == target


# ── launch() resume selection ───────────────────────────────────────────


@pytest.fixture
def no_boot_watchdog(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple]]:
    """Stub out the boot watchdog thread and record what it would have started."""
    started = []

    def mock_thread(target: Callable[..., object], args: tuple, daemon: bool) -> object:
        started.append((target.__name__, args))
        return type("MockThread", (), {"start": lambda s: None})()

    monkeypatch.setattr(rpcs3, "Thread", mock_thread)
    return started


def test_launch_boots_the_state_file_when_a_resume_is_found(
    rpcs3_dirs: dict[str, Path],
    no_boot_watchdog: list[tuple[str, tuple]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Launch boots the state file when a resume is found."""
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)
    state = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_3.SAVESTAT")

    rpcs3.Rpcs3().launch(eboot, 1)

    assert spawned["cmd"][-1] == str(state)


def test_launch_falls_back_to_a_fresh_boot_with_no_resume_state(
    rpcs3_dirs: dict[str, Path], no_boot_watchdog: list[tuple[str, tuple]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch falls back to a fresh boot with no resume state."""
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)

    rpcs3.Rpcs3().launch(eboot, 1)

    assert spawned["cmd"][-1] == str(eboot)


def test_launch_falls_back_to_a_fresh_boot_with_no_known_serial(
    rpcs3_dirs: dict[str, Path],
    no_boot_watchdog: list[tuple[str, tuple]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Launch falls back to a fresh boot with no known serial."""
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"iso")

    rpcs3.Rpcs3().launch(iso, 1)

    assert spawned["cmd"][-1] == str(iso)


def test_launch_boots_normally_without_a_resume_slot(
    rpcs3_dirs: dict[str, Path], no_boot_watchdog: list[tuple[str, tuple]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch boots normally without a resume slot."""
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)
    _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_3.SAVESTAT")

    rpcs3.Rpcs3().launch(eboot, None)

    assert spawned["cmd"][-1] == str(eboot)


def test_launch_always_spawns_the_boot_watchdog(
    rpcs3_dirs: dict[str, Path], no_boot_watchdog: list[tuple[str, tuple]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch always spawns the boot watchdog."""
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    monkeypatch.setattr(rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: None)
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)

    emu = rpcs3.Rpcs3()
    emu.launch(eboot, None)

    assert len(no_boot_watchdog) == 1
    assert no_boot_watchdog[0][0] == "_boot_watchdog"
    assert no_boot_watchdog[0][1] == (emu._launch_seq,)


def test_launch_clears_leftovers_once_the_serial_is_known_synchronously(
    rpcs3_dirs: dict[str, Path], no_boot_watchdog: list[tuple[str, tuple]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot target whose serial resolves inside launch() itself must run the deferred clear too.

    An installed/disc-rip EBOOT resolves its serial synchronously in
    launch(), before the boot watchdog ever starts, so a pending
    pre-restore snapshot from clear_working_slot must be consumed here,
    not left for a PINE round trip that will never happen.
    """
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    monkeypatch.setattr(rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: None)
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)
    stale = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")

    emu = rpcs3.Rpcs3()
    emu._leftover_snapshot = rpcs3._state_snapshot("BLUS30443")
    emu.launch(eboot, None)

    assert not stale.exists()
    assert emu._leftover_snapshot is None


# ── save_and_exit ───────────────────────────────────────────────────────


def test_exit_without_a_slot_saves_nothing(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit without a slot saves nothing."""
    emu = rpcs3.Rpcs3()
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: pytest.fail("hotkey sent with no slot requested")

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}


def test_exit_with_a_slot_but_no_known_serial_saves_nothing(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit with a slot but no known serial saves nothing."""
    emu = rpcs3.Rpcs3()
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._session_serial = None
    emu._send_key = lambda key: pytest.fail("hotkey sent with no known title id")

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


def test_exit_reports_the_state_the_hotkey_wrote(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit reports the state the hotkey wrote."""
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)

    written = rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT"

    def fake_send_key(key: str) -> bool:
        _touch(written)
        return True

    emu._send_key = fake_send_key

    report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert report["state_slot"] == 1
    assert report["state_file"]["path"] == str(written)


def test_exit_reports_no_save_when_the_hotkey_fails_to_send(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit reports no save when the hotkey fails to send."""
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: False

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


def test_exit_reports_no_save_when_no_new_state_appears(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit reports no save when no new state appears."""
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: True
    monkeypatch.setattr(
        rpcs3, "_wait_for_state_write", lambda serial, before, deadline, pid=None: None
    )

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


def test_exit_does_not_abort_the_save_dump_when_the_state_file_disappears_before_stat(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state file vanishing before stat() must not abort save_and_exit.

    _wait_for_state_write can hand back a path that's gone by the time we
    stat it (e.g. RPCS3 renames it during compression); that must not raise
    out of save_and_exit and skip the stop()/dump that follows.
    """
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: True
    missing = rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT"
    monkeypatch.setattr(
        rpcs3, "_wait_for_state_write", lambda serial, before, deadline, pid=None: missing
    )
    stopped = {"called": False}
    emu.stop = lambda: stopped.__setitem__("called", True)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert stopped["called"] is True


def test_exit_hands_the_running_pid_to_the_write_confirmation(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write confirmation needs the launch pid to read its open descriptors.

    Without it every write looks closed and a stalled save is archived as a
    finished one.
    """
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    emu._proc = SimpleNamespace(pid=4242)
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: True
    emu.stop = lambda: None
    seen: dict = {}

    def record(serial: Optional[str], before: dict, deadline: float, pid: Optional[int]) -> None:
        seen["pid"] = pid
        return None

    monkeypatch.setattr(rpcs3, "_wait_for_state_write", record)

    emu.save_and_exit(1)

    assert seen["pid"] == 4242


def test_exit_passes_no_pid_when_the_broker_holds_no_handle(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save with no process handle must still run, on the size test alone."""
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: True
    emu.stop = lambda: None
    seen: dict = {}

    def record(serial: Optional[str], before: dict, deadline: float, pid: Optional[int]) -> None:
        seen["pid"] = pid
        return None

    monkeypatch.setattr(rpcs3, "_wait_for_state_write", record)

    emu.save_and_exit(1)

    assert seen["pid"] is None


# ── PINE wire protocol ────────────────────────────────────────────────────


@pytest.fixture
def pine_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the PINE unix socket path at an isolated temp file."""
    sock_path = tmp_path / "rpcs3.sock"
    monkeypatch.setattr(rpcs3, "PINE_SOCKET", sock_path)
    return sock_path


def _serve_pine_reply(sock_path: Path, reply: Optional[bytes]) -> threading.Thread:
    """Serve a single PINE reply over a Unix socket for one test connection.

    Accepts one connection, reads and discards the request, then writes
    back `reply` (or nothing, to simulate a closed/no-reply connection).
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def run() -> None:
        conn, _ = server.accept()
        with conn:
            conn.settimeout(5)
            header = conn.recv(5)
            if len(header) == 5:
                remaining = struct.unpack("<I", header[:4])[0] - 5
                got = 0
                while got < remaining:
                    chunk = conn.recv(remaining - got)
                    if not chunk:
                        break
                    got += len(chunk)
            if reply is not None:
                conn.sendall(reply)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_pine_request_returns_the_reply_body_on_success(pine_socket: Path) -> None:
    """PINE request returns the reply body on success."""
    body = b"status-body"
    reply = struct.pack("<IB", 5 + len(body), 0) + body
    thread = _serve_pine_reply(pine_socket, reply)

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result == body


def test_pine_request_returns_none_on_a_nonzero_result_byte(pine_socket: Path) -> None:
    """PINE request returns none on a nonzero result byte."""
    reply = struct.pack("<IB", 5, 1)
    thread = _serve_pine_reply(pine_socket, reply)

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result is None


def test_pine_request_returns_none_on_a_truncated_header(pine_socket: Path) -> None:
    """PINE request returns none on a truncated header."""
    thread = _serve_pine_reply(pine_socket, b"\x05\x00")

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result is None


def test_pine_request_coerces_a_truncated_body_to_empty(pine_socket: Path) -> None:
    """A truncated PINE body is coerced to empty bytes, not treated as failure.

    The declared size promises 5 payload bytes but the connection closes
    after 2; _pine_recv_exact's None gets folded into b"" rather than
    propagated. Both current callers (_pine_status, _pine_title_id) treat an
    empty body the same as None, so this documents the actual coercion
    rather than asserting a failure path that doesn't exist.
    """
    reply = struct.pack("<IB", 10, 0) + b"ab"
    thread = _serve_pine_reply(pine_socket, reply)

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result == b""


class _StubSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_pine_recv_exact_accumulates_across_several_recv_calls() -> None:
    """PINE recv exact accumulates across several recv calls."""
    sock = _StubSocket([b"ab", b"cde", b"f"])

    assert rpcs3._pine_recv_exact(sock, 6, time.monotonic() + 5) == b"abcdef"


def test_pine_recv_exact_returns_none_when_the_socket_closes_early() -> None:
    """PINE recv exact returns none when the socket closes early."""
    sock = _StubSocket([b"ab", b""])

    assert rpcs3._pine_recv_exact(sock, 6, time.monotonic() + 5) is None


def test_pine_recv_exact_gives_up_once_the_shared_deadline_passes() -> None:
    """A deadline already in the past stops the read before any recv."""
    sock = _StubSocket([b"ab", b"cd", b"ef"])

    assert rpcs3._pine_recv_exact(sock, 6, time.monotonic() - 1) is None
    assert sock.timeouts == []


def test_pine_recv_exact_spends_the_budget_rather_than_restarting_it() -> None:
    """Each recv gets the time remaining, never a fresh full timeout.

    A peer dribbling one byte per call would otherwise hold the caller
    forever, since a per-recv timeout never expires.
    """
    sock = _StubSocket([b"a", b"b", b"c"])

    rpcs3._pine_recv_exact(sock, 3, time.monotonic() + 5)

    assert sock.timeouts == sorted(sock.timeouts, reverse=True)
    assert all(t <= 5 for t in sock.timeouts)


def test_pine_request_refuses_a_reply_larger_than_the_cap(pine_socket: Path) -> None:
    """A declared reply above the cap is refused instead of accumulated."""
    reply = struct.pack("<IB", rpcs3._PINE_MAX_REPLY_BYTES + 1, 0)
    thread = _serve_pine_reply(pine_socket, reply)

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result is None


# ── boot watchdog ───────────────────────────────────────────────────────


class _FakeClock:
    def __init__(self, step: float = 30.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture
def watchdog_env(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Patch time.sleep and time.monotonic with a fast, deterministic fake clock."""
    monkeypatch.setattr(rpcs3.time, "sleep", lambda _seconds: None)
    clock = _FakeClock()
    monkeypatch.setattr(rpcs3.time, "monotonic", clock)
    return clock


def test_boot_watchdog_clears_the_flag_when_the_game_reports_running(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """Boot watchdog clears the flag when the game reports running."""
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    # alive() forced True: the deadline path would also leave boot_failed
    # False on a real (unpatched) instance, so without this the test would
    # pass even if the "reported running" early return were deleted.
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"

    emu._boot_watchdog(emu._launch_seq)

    assert emu.boot_failed is False


def test_boot_watchdog_resolves_an_unknown_serial_once_running(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """Boot watchdog resolves an unknown serial once running."""
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    monkeypatch.setattr(rpcs3, "_pine_title_id", lambda: "BLUS30443")
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu = rpcs3.Rpcs3()
    emu._session_serial = None

    emu._boot_watchdog(emu._launch_seq)

    assert emu._session_serial == "BLUS30443"
    assert emu.boot_failed is False


def test_boot_watchdog_clears_leftovers_once_the_title_resolves(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """A bare .iso's leftover states must be cleared once PINE names the title.

    clear_working_slot could not scope its clear at activate time, so the
    boot watchdog is the one place left that ever learns the title id for
    a bare .iso boot; it must finish the deferred clear _all_state_files
    was built to feed.
    """
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    monkeypatch.setattr(rpcs3, "_pine_title_id", lambda: "BLUS30443")
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    stale = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    emu = rpcs3.Rpcs3()
    emu._session_serial = None
    emu._leftover_snapshot = rpcs3._state_snapshot("BLUS30443")

    emu._boot_watchdog(emu._launch_seq)

    assert not stale.exists()
    assert emu._leftover_snapshot is None


def test_boot_watchdog_keeps_a_restored_state_once_the_title_resolves(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """A state the archive restore already wrote must survive the deferred clear."""
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    monkeypatch.setattr(rpcs3, "_pine_title_id", lambda: "BLUS30443")
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu = rpcs3.Rpcs3()
    emu._session_serial = None
    emu._leftover_snapshot = rpcs3._state_snapshot("BLUS30443")  # taken before the restore
    restored = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")

    emu._boot_watchdog(emu._launch_seq)

    assert restored.exists()


def test_boot_watchdog_discards_a_title_lookup_that_lands_after_a_supersede(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """A stale title lookup must not stamp a session that has moved on.

    _pine_title_id() can block past a relaunch/stop; the resolved title
    must not be stamped onto a session that has already moved on.
    """
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    emu = rpcs3.Rpcs3()
    seq = emu._launch_seq
    emu._session_serial = None

    def title_id() -> str:
        emu._launch_seq += 1  # a relaunch/stop landed during the PINE round trip
        return "BLUS30443"

    monkeypatch.setattr(rpcs3, "_pine_title_id", title_id)

    emu._boot_watchdog(seq)

    assert emu._session_serial is None


def test_boot_watchdog_flags_a_hang_when_the_process_is_still_alive(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """Boot watchdog flags a hang when the process is still alive."""
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: None)
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu = rpcs3.Rpcs3()

    emu._boot_watchdog(emu._launch_seq)

    assert emu.boot_failed is True


def test_boot_watchdog_does_not_flag_a_process_that_already_exited(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """Boot watchdog does not flag a process that already exited."""
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: None)
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: False)
    emu = rpcs3.Rpcs3()

    emu._boot_watchdog(emu._launch_seq)

    assert emu.boot_failed is False


def test_boot_watchdog_abandons_a_superseded_launch(
    rpcs3_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A step of 1.0 (vs watchdog_env's 30.0) keeps the loop under the 90s
    # deadline long enough to re-enter the body after the seq bump below,
    # which is what actually exercises the in-loop supersede check rather
    # than just falling through to the post-loop one.
    """Boot watchdog abandons a superseded launch."""
    monkeypatch.setattr(rpcs3.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(rpcs3.time, "monotonic", _FakeClock(step=1.0))
    emu = rpcs3.Rpcs3()
    seq = emu._launch_seq
    calls = {"n": 0}

    def status() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            emu._launch_seq += 1
        return None

    monkeypatch.setattr(rpcs3, "_pine_status", status)
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)

    emu._boot_watchdog(seq)

    assert emu.boot_failed is False
    # Exactly 2: the 3rd loop iteration re-enters with the bumped seq and
    # returns from the in-loop check before calling _pine_status again.
    assert calls["n"] == 2


def test_stop_invalidates_an_in_flight_boot_watchdog(rpcs3_dirs: dict[str, Path]) -> None:
    """Stop invalidates an in flight boot watchdog."""
    emu = rpcs3.Rpcs3()
    seq_before = emu._launch_seq

    emu.stop()

    assert emu._launch_seq != seq_before


# ── xdotool window targeting ────────────────────────────────────────────


def test_send_key_activates_then_sends_the_key(rpcs3_dirs: dict[str, Path]) -> None:
    """Send key activates then sends the key."""
    emu = rpcs3.Rpcs3()
    calls = []
    emu._xdotool = lambda *args: (calls.append(args), "111\n")[1] if args[0] == "search" else (
        calls.append(args), "ok"
    )[1]

    assert emu._send_key("ctrl+alt+1") is True
    assert calls[0] == ("search", "--class", rpcs3._WINDOW_CLASS)
    assert calls[1][0] == "windowactivate"
    assert calls[2] == ("key", "--clearmodifiers", "ctrl+alt+1")


def test_send_key_fails_with_no_window(rpcs3_dirs: dict[str, Path]) -> None:
    """Send key fails with no window."""
    emu = rpcs3.Rpcs3()
    emu._xdotool = lambda *args: None

    assert emu._send_key("ctrl+alt+1") is False


# ── archive extraction / extraction cache ─────────────────────────────


@pytest.mark.parametrize("setting,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("", False),
])
def test_the_cache_enabled_switch_reads_the_usual_spellings(setting: str, expected: bool) -> None:
    """_truthy recognizes the usual truthy and falsy string spellings."""
    assert rpcs3._truthy(setting) is expected


@pytest.fixture
def cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the archive extraction cache at an isolated temp directory with caching disabled."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(rpcs3, "CACHE_DIR", cache)
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", False)
    return cache


def test_archive_dir_size_sums_files_and_skips_the_marker(cache_dir: Path) -> None:
    """Archive dir size sums files and skips the marker."""
    game_dir = cache_dir / "Game"
    _touch(game_dir / "EBOOT.BIN")
    _touch(game_dir / "PARAM.SFO")
    _touch(game_dir / rpcs3._LAST_ACCESSED_MARKER)

    assert rpcs3._archive_dir_size(game_dir) == 10


def test_cache_size_bytes_sums_across_every_game_dir(cache_dir: Path) -> None:
    """Cache size bytes sums across every game dir."""
    _touch(cache_dir / "GameA" / "EBOOT.BIN")
    _touch(cache_dir / "GameB" / "EBOOT.BIN")

    assert rpcs3._cache_size_bytes() == 10


def test_cache_size_bytes_is_zero_without_a_cache_dir(cache_dir: Path) -> None:
    """Cache size bytes is zero without a cache dir."""
    assert rpcs3._cache_size_bytes() == 0


def test_touch_last_accessed_writes_a_marker_file(cache_dir: Path) -> None:
    """Touch last accessed writes a marker file."""
    game_dir = cache_dir / "Game"
    _touch(game_dir / "EBOOT.BIN")

    rpcs3._touch_last_accessed(game_dir)

    assert (game_dir / rpcs3._LAST_ACCESSED_MARKER).exists()


def test_evict_lru_is_a_noop_when_disabled(cache_dir: Path) -> None:
    """Evict LRU is a no-op when disabled."""
    game_dir = cache_dir / "GameA"
    _touch(game_dir / "EBOOT.BIN")

    rpcs3._evict_lru(10**9, "SomethingElse")

    assert game_dir.exists()


def test_evict_lru_removes_the_least_recently_used_entry_first(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU removes the least recently used entry first."""
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", True)
    monkeypatch.setattr(rpcs3, "CACHE_MAX_GB", 8 / 1024**3)
    old = cache_dir / "Old"
    new = cache_dir / "New"
    _touch(old / "EBOOT.BIN")
    _touch(new / "EBOOT.BIN")
    _touch(old / rpcs3._LAST_ACCESSED_MARKER, mtime=1000)
    _touch(new / rpcs3._LAST_ACCESSED_MARKER, mtime=2000)

    rpcs3._evict_lru(2, "Incoming")

    assert not old.exists()
    assert new.exists()


def test_evict_lru_never_removes_the_entry_being_extracted(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU never removes the entry being extracted."""
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", True)
    monkeypatch.setattr(rpcs3, "CACHE_MAX_GB", 1 / 1024**3)
    keep = cache_dir / "Incoming"
    _touch(keep / "EBOOT.BIN")
    _touch(keep / rpcs3._LAST_ACCESSED_MARKER, mtime=1)

    rpcs3._evict_lru(50, "Incoming")

    assert keep.exists()


def test_evict_lru_gives_up_and_proceeds_when_nothing_is_left_to_evict(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU gives up and proceeds when nothing is left to evict."""
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", True)
    monkeypatch.setattr(rpcs3, "CACHE_MAX_GB", 1 / 1024**3)
    cache_dir.mkdir(parents=True, exist_ok=True)

    rpcs3._evict_lru(10**9, "Incoming")  # must not raise


def test_archive_boot_target_prefers_eboot_over_iso(cache_dir: Path) -> None:
    """Archive boot target prefers eboot over ISO."""
    root = cache_dir / "Game"
    _touch(root / "disc.iso")
    _touch(root / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    assert rpcs3._archive_boot_target(root).name == "EBOOT.BIN"


def test_archive_boot_target_falls_back_to_iso(cache_dir: Path) -> None:
    """Archive boot target falls back to ISO."""
    root = cache_dir / "Game"
    iso = _touch(root / "disc.iso")

    assert rpcs3._archive_boot_target(root) == iso


def test_archive_boot_target_is_none_when_nothing_bootable_is_present(cache_dir: Path) -> None:
    """Archive boot target is none when nothing bootable is present."""
    root = cache_dir / "Game"
    _touch(root / "readme.txt")

    assert rpcs3._archive_boot_target(root) is None


def test_archive_boot_target_refuses_an_eboot_that_symlinks_outside_root(
    cache_dir: Path, tmp_path: Path
) -> None:
    """Archive boot target refuses an eboot that symlinks outside root."""
    root = cache_dir / "Game"
    root.mkdir(parents=True)
    outside = _touch(tmp_path / "outside" / "secret.bin")
    (root / "EBOOT.BIN").symlink_to(outside)

    assert rpcs3._archive_boot_target(root) is None


def test_archive_boot_target_refuses_an_iso_that_symlinks_outside_root(
    cache_dir: Path, tmp_path: Path
) -> None:
    """Archive boot target refuses an ISO that symlinks outside root."""
    root = cache_dir / "Game"
    root.mkdir(parents=True)
    outside = _touch(tmp_path / "outside" / "disc.iso")
    (root / "disc.iso").symlink_to(outside)

    assert rpcs3._archive_boot_target(root) is None


def test_archive_boot_target_accepts_an_eboot_that_symlinks_inside_root(cache_dir: Path) -> None:
    """Archive boot target accepts an eboot that symlinks inside root."""
    root = cache_dir / "Game"
    real_eboot = _touch(root / "real" / "actual_eboot.bin")
    linked = root / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(real_eboot)

    assert rpcs3._archive_boot_target(root) == linked


def test_archive_boot_target_skips_a_dangling_eboot_symlink(cache_dir: Path) -> None:
    """Archive boot target skips a dangling eboot symlink."""
    root = cache_dir / "Game"
    root.mkdir(parents=True)
    (root / "EBOOT.BIN").symlink_to(root / "does-not-exist")
    iso = _touch(root / "disc.iso")

    assert rpcs3._archive_boot_target(root) == iso


def _make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_safe_extract_zip_extracts_normal_members(cache_dir: Path, tmp_path: Path) -> None:
    """Safe extract zip extracts normal members."""
    archive = _make_zip(tmp_path / "Game.zip", {"PS3_GAME/USRDIR/EBOOT.BIN": b"boot"})
    dest = cache_dir / "Game"
    dest.mkdir(parents=True)

    with zipfile.ZipFile(archive) as zf:
        rpcs3._safe_extract_zip(zf, dest)

    assert (dest / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").read_bytes() == b"boot"


def test_safe_extract_zip_rejects_a_member_that_escapes_the_dest(cache_dir: Path, tmp_path: Path) -> None:
    """Safe extract zip rejects a member that escapes the dest."""
    archive = _make_zip(tmp_path / "Evil.zip", {"../../etc/passwd": b"pwned"})
    dest = cache_dir / "Evil"
    dest.mkdir(parents=True)

    with zipfile.ZipFile(archive) as zf:
        with pytest.raises(RuntimeError, match="escapes"):
            rpcs3._safe_extract_zip(zf, dest)


def test_extract_archive_zip_raises_on_a_corrupt_file(cache_dir: Path, tmp_path: Path) -> None:
    """Extract archive zip raises on a corrupt file."""
    archive = tmp_path / "Corrupt.zip"
    archive.write_bytes(b"not a zip")
    dest = cache_dir / "Corrupt"
    dest.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="zip extraction"):
        rpcs3._extract_archive(archive, dest)


def test_extract_archive_dispatches_rar_to_unrar(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract archive dispatches rar to unrar."""
    calls = []
    monkeypatch.setattr(rpcs3, "_run_extractor", lambda cmd, what: calls.append(cmd) or "")
    archive = tmp_path / "Game.rar"
    archive.write_bytes(b"rar")

    rpcs3._extract_archive(archive, cache_dir / "Game")

    assert calls[0] == ["unrar", "lb", "-y", str(archive)]
    assert calls[1][0] == "unrar" and calls[1][1] == "x"
    assert str(archive) in calls[1]


def test_extract_archive_dispatches_7z_and_unknown_exts_to_7z(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract archive dispatches .7z and unknown extensions to the 7z tool."""
    calls = []
    monkeypatch.setattr(rpcs3, "_run_extractor", lambda cmd, what: calls.append(cmd) or "")
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"7z")

    rpcs3._extract_archive(archive, cache_dir / "Game")

    assert calls[0] == ["7z", "l", "-slt", str(archive)]
    assert calls[1][0] == "7z" and calls[1][1] == "x"


def test_extract_archive_rar_rejects_a_member_that_escapes_the_dest(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract archive rar rejects a member that escapes the dest."""
    calls = []

    def fake_run(cmd: list[str], what: str) -> str:
        calls.append(cmd)
        return "../../etc/passwd\n" if cmd[1] == "lb" else ""

    monkeypatch.setattr(rpcs3, "_run_extractor", fake_run)
    archive = tmp_path / "Evil.rar"
    archive.write_bytes(b"rar")

    with pytest.raises(RuntimeError, match="escapes"):
        rpcs3._extract_archive(archive, cache_dir / "Evil")

    assert calls == [["unrar", "lb", "-y", str(archive)]]


def test_extract_archive_7z_rejects_a_member_that_escapes_the_dest(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract archive 7z rejects a member that escapes the dest."""
    calls = []
    slt_output = "Path = Evil.7z\n\n----------\nPath = ../../etc/passwd\nSize = 4\n"

    def fake_run(cmd: list[str], what: str) -> str:
        calls.append(cmd)
        return slt_output if cmd[1] == "l" else ""

    monkeypatch.setattr(rpcs3, "_run_extractor", fake_run)
    archive = tmp_path / "Evil.7z"
    archive.write_bytes(b"7z")

    with pytest.raises(RuntimeError, match="escapes"):
        rpcs3._extract_archive(archive, cache_dir / "Evil")

    assert calls == [["7z", "l", "-slt", str(archive)]]


def test_reject_unsafe_members_rejects_a_traversal_path(tmp_path: Path) -> None:
    """Reject unsafe members rejects a traversal path."""
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(RuntimeError, match="escapes"):
        rpcs3._reject_unsafe_members(dest, ["../outside.txt"])


def test_reject_unsafe_members_allows_normal_paths(tmp_path: Path) -> None:
    """Reject unsafe members allows normal paths."""
    dest = tmp_path / "dest"
    dest.mkdir()

    rpcs3._reject_unsafe_members(dest, ["a/b/c.txt", "top.txt"])


def test_reject_escaped_tree_allows_a_normal_extraction(tmp_path: Path) -> None:
    """Reject escaped tree allows a normal extraction."""
    dest = tmp_path / "dest"
    (dest / "sub").mkdir(parents=True)
    (dest / "sub" / "file.txt").write_bytes(b"x")

    rpcs3._reject_escaped_tree(dest)


def test_reject_escaped_tree_rejects_a_symlink_that_resolves_outside_dest(tmp_path: Path) -> None:
    """A symlink resolving outside dest must be caught, not just literal paths.

    The pre-extraction listing check can be fooled by a control character
    that renders differently in unrar/7z's text listing than in the
    archive's real central directory, so this walks what actually landed on
    disk -- a symlink is exactly the kind of real filesystem object that
    check could never catch before extraction ran.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="escapes cache dir"):
        rpcs3._reject_escaped_tree(dest)


def test_extract_and_cache_serializes_a_second_call_racing_the_same_archive(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A racing second call must wait for _CACHE_LOCK, not extract concurrently.

    A second launch racing in while the first is mid-extraction must wait
    for _CACHE_LOCK rather than run its own extraction concurrently, which
    could interleave writes into the same not-yet-populated game_dir or
    have one call evict the directory the other is about to boot from.
    """
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", True)
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")

    entered = threading.Event()
    release = threading.Event()
    entry_count = []

    def fake_extract_archive(_archive: Path, dest: Path) -> None:
        entry_count.append(1)
        entered.set()
        release.wait(timeout=5)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "EBOOT.BIN").write_bytes(b"x")

    monkeypatch.setattr(rpcs3, "_extract_archive", fake_extract_archive)

    first = threading.Thread(
        target=rpcs3._extract_and_cache, args=(archive, rpcs3.Rpcs3())
    )
    first.start()
    assert entered.wait(timeout=5)

    second = threading.Thread(
        target=rpcs3._extract_and_cache, args=(archive, rpcs3.Rpcs3())
    )
    second.start()
    time.sleep(0.2)
    # Still 1: the second call is blocked on _CACHE_LOCK, not free to run its
    # own extraction while the first is still inside the critical section.
    assert entry_count == [1]

    release.set()
    first.join(timeout=5)
    second.join(timeout=5)


def test_rar_member_paths_parses_bare_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_rar_member_paths parses a bare listing with no archive header."""
    monkeypatch.setattr(rpcs3, "_run_extractor", lambda cmd, what: "sub\nsub/file.txt\ntop.txt\n")

    assert rpcs3._rar_member_paths(tmp_path / "Game.rar") == ["sub", "sub/file.txt", "top.txt"]


def test_7z_member_paths_parses_slt_listing_and_skips_the_archive_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_7z_member_paths parses an slt listing and skips the archive header line."""
    slt_output = (
        "Path = Game.7z\nType = 7z\n\n"
        "----------\n"
        "Path = sub\nSize = 0\n\n"
        "Path = sub/file.txt\nSize = 3\n"
    )
    monkeypatch.setattr(rpcs3, "_run_extractor", lambda cmd, what: slt_output)

    assert rpcs3._7z_member_paths(tmp_path / "Game.7z") == ["sub", "sub/file.txt"]


def test_run_extractor_raises_on_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run extractor raises on a nonzero exit."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 2, "stderr": "boom"})(),
    )

    with pytest.raises(RuntimeError, match="exited 2"):
        rpcs3._run_extractor(["7z", "x"], "7z (Game.7z)")


def test_run_extractor_raises_when_the_binary_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run extractor raises when the binary is missing."""
    def raise_oserror(*a: object, **k: object) -> NoReturn:
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(RuntimeError, match="failed to run"):
        rpcs3._run_extractor(["unrar", "x"], "unrar (Game.rar)")


def test_cache_key_differs_for_archives_sharing_a_stem_but_not_an_extension(tmp_path: Path) -> None:
    """Cache key differs for archives sharing a stem but not an extension."""
    zip_archive = tmp_path / "Game.zip"
    zip_archive.write_bytes(b"zip")
    rar_archive = tmp_path / "Game.rar"
    rar_archive.write_bytes(b"rar")

    assert rpcs3._cache_key(zip_archive) != rpcs3._cache_key(rar_archive)


def test_cache_key_changes_when_a_same_named_archive_is_replaced(tmp_path: Path) -> None:
    """Cache key changes when a same named archive is replaced."""
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"original")
    original_key = rpcs3._cache_key(archive)

    archive.write_bytes(b"a completely different dump")
    os.utime(archive, (archive.stat().st_mtime + 5, archive.stat().st_mtime + 5))

    assert rpcs3._cache_key(archive) != original_key


def test_extract_and_cache_does_not_reuse_a_stale_entry_from_a_replaced_archive(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache does not reuse a stale entry from a replaced archive."""
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"original dump")
    stale_eboot = _touch(cache_dir / rpcs3._cache_key(archive) / "EBOOT.BIN")

    archive.write_bytes(b"a completely different, much longer replacement dump")
    os.utime(archive, (archive.stat().st_mtime + 5, archive.stat().st_mtime + 5))

    def fake_extract(archive_: Path, dest: Path) -> None:
        _touch(dest / "EBOOT.BIN")

    monkeypatch.setattr(rpcs3, "_extract_archive", fake_extract)

    boot = rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert boot != stale_eboot
    assert boot.parent != stale_eboot.parent


def test_extract_and_cache_reuses_an_existing_bootable_extraction(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache reuses an existing bootable extraction."""
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"7z")
    key = rpcs3._cache_key(archive)
    eboot = _touch(cache_dir / key / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")
    called = []
    monkeypatch.setattr(rpcs3, "_extract_archive", lambda *a: called.append(a))

    boot = rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert boot == eboot
    assert called == []
    assert (cache_dir / key / rpcs3._LAST_ACCESSED_MARKER).exists()


def test_extract_and_cache_re_extracts_a_stale_cache_dir_with_no_boot_target(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache re extracts a stale cache dir with no boot target."""
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"7z")
    stale = cache_dir / rpcs3._cache_key(archive)
    _touch(stale / "readme.txt")

    def fake_extract(archive_: Path, dest: Path) -> None:
        _touch(dest / "EBOOT.BIN")

    monkeypatch.setattr(rpcs3, "_extract_archive", fake_extract)

    boot = rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert boot.name == "EBOOT.BIN"
    assert not (stale / "readme.txt").exists()


def test_extract_and_cache_extracts_and_returns_the_boot_target_on_a_miss(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache extracts and returns the boot target on a miss."""
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")

    def fake_extract(archive_: Path, dest: Path) -> None:
        _touch(dest / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    monkeypatch.setattr(rpcs3, "_extract_archive", fake_extract)

    boot = rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert boot.name == "EBOOT.BIN"
    assert (cache_dir / rpcs3._cache_key(archive) / rpcs3._LAST_ACCESSED_MARKER).exists()


def test_extract_and_cache_cleans_up_and_raises_when_extraction_fails(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache cleans up and raises when extraction fails."""
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")

    def fake_extract(archive_: Path, dest: Path) -> NoReturn:
        raise RuntimeError("boom")

    monkeypatch.setattr(rpcs3, "_extract_archive", fake_extract)

    with pytest.raises(RuntimeError, match="boom"):
        rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert not (cache_dir / rpcs3._cache_key(archive)).exists()


def test_extract_and_cache_sets_and_clears_the_extraction_phase(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction reports the extracting_archive phase, then clears it on success."""
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")
    emulator = rpcs3.Rpcs3()
    seen_phase = []

    def fake_extract(archive_: Path, dest: Path) -> None:
        seen_phase.append(emulator.extraction_phase)
        _touch(dest / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    monkeypatch.setattr(rpcs3, "_extract_archive", fake_extract)

    rpcs3._extract_and_cache(archive, emulator)

    assert seen_phase == ["extracting_archive"]
    assert emulator.extraction_phase is None


def test_extract_and_cache_clears_the_phase_when_extraction_fails(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extraction phase resets to None even when extraction raises."""
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")
    emulator = rpcs3.Rpcs3()

    def fake_extract(archive_: Path, dest: Path) -> NoReturn:
        raise RuntimeError("boom")

    monkeypatch.setattr(rpcs3, "_extract_archive", fake_extract)

    with pytest.raises(RuntimeError, match="boom"):
        rpcs3._extract_and_cache(archive, emulator)

    assert emulator.extraction_phase is None


def test_extract_and_cache_cleans_up_and_raises_when_nothing_bootable_was_extracted(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache cleans up and raises when nothing bootable was extracted."""
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")
    monkeypatch.setattr(rpcs3, "_extract_archive", lambda a, d: None)

    with pytest.raises(RuntimeError, match="no EBOOT.BIN"):
        rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert not (cache_dir / rpcs3._cache_key(archive)).exists()


def test_a_partial_extraction_never_appears_at_the_cache_key(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is written under the cache key until a boot target is confirmed.

    A process killed mid-extraction must not leave a truncated EBOOT.BIN
    where the next launch would take it for a finished cache entry, so the
    game dir may only appear once, complete, by rename.
    """
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")
    game_dir = cache_dir / rpcs3._cache_key(archive)
    seen = []

    def fake_extract(archive_: Path, dest: Path) -> None:
        # Mid-extraction: whatever has been unpacked so far is not yet
        # reachable under the cache key.
        seen.append(game_dir.exists())
        _touch(dest / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    monkeypatch.setattr(rpcs3, "_extract_archive", fake_extract)

    rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert seen == [False]
    assert (game_dir / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").is_file()


def test_extraction_reclaims_orphaned_scratch_before_sizing_the_cache(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scratch left by a dead process is removed before eviction, not evicted around.

    Scratch is exempt from eviction but still counts toward CACHE_MAX_GB, so
    leaving an orphan in place would make eviction delete real cache entries
    to make room for space that is already garbage.
    """
    orphan = _touch(cache_dir / rpcs3._SCRATCH_DIR_NAME / "dead-run" / "huge.iso").parent
    scratch_at_eviction = []
    monkeypatch.setattr(
        rpcs3,
        "_evict_lru",
        lambda needed, keep: scratch_at_eviction.append(orphan.exists()),
    )
    monkeypatch.setattr(
        rpcs3, "_extract_archive", lambda a, d: _touch(d / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")
    )
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")

    rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert scratch_at_eviction == [False]


def test_an_extraction_too_big_for_the_cap_is_refused_before_it_starts(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive that cannot fit under CACHE_MAX_GB is refused, not attempted anyway.

    Eviction cannot help when there is nothing left to evict, and starting
    regardless means filling the disk over the minutes the unpack takes
    before failing on a write error.
    """
    monkeypatch.setattr(rpcs3, "CACHE_MAX_GB", 0.000001)
    extracted = []
    monkeypatch.setattr(rpcs3, "_extract_archive", lambda a, d: extracted.append(a))
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"x" * 4096)

    with pytest.raises(RuntimeError, match="RPCS3_CACHE_MAX_GB"):
        rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert extracted == []
    assert not (cache_dir / rpcs3._cache_key(archive)).exists()


def test_an_extraction_larger_than_the_free_disk_is_refused_before_it_starts(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive that would not fit on the filesystem is refused even when the cap allows it.

    CACHE_MAX_GB bounds the cache's own contents, not the disk it shares
    with the rest of /config, so the cap passing proves nothing about there
    being room to write.
    """
    monkeypatch.setattr(
        rpcs3.shutil, "disk_usage", lambda p: SimpleNamespace(total=1, used=1, free=1)
    )
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"x" * 4096)

    with pytest.raises(RuntimeError, match="is free on"):
        rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())


def test_an_uncleared_cache_dir_fails_the_extraction_instead_of_silently_escaping(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A game dir that would not delete raises RuntimeError, not a bare OSError.

    The stale-entry cleanup ignores errors, so the rename into place can
    still find the old directory there; launch only translates RuntimeError
    into a useful message.
    """
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"zip")
    game_dir = _touch(cache_dir / rpcs3._cache_key(archive) / "junk.txt").parent
    monkeypatch.setattr(rpcs3.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(
        rpcs3, "_extract_archive", lambda a, d: _touch(d / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")
    )

    with pytest.raises(RuntimeError, match="could not cache the extraction"):
        rpcs3._extract_and_cache(archive, rpcs3.Rpcs3())

    assert (game_dir / "junk.txt").is_file()


def test_sweep_stale_extractions_removes_orphaned_scratch_dirs_but_keeps_real_ones(
    cache_dir: Path,
) -> None:
    """Sweep stale extractions empties the scratch subtree but keeps real cache entries."""
    scratch = _touch(
        cache_dir / rpcs3._SCRATCH_DIR_NAME / "Game-abc123-xyz987" / "leftover.iso"
    ).parent
    game_dir = _touch(cache_dir / "Game-abc123" / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").parent

    rpcs3.sweep_stale_extractions()

    assert not scratch.exists()
    assert game_dir.exists()


def test_sweep_stale_extractions_removes_a_scratch_dir_holding_a_bootable_decoy(
    cache_dir: Path,
) -> None:
    """An orphaned scratch dir is swept even when it holds a bootable-looking file.

    Scratch holds an archive's raw contents, so an EBOOT.BIN under it proves
    nothing; living under the scratch subtree is what makes it scratch.
    """
    scratch = _touch(
        cache_dir / rpcs3._SCRATCH_DIR_NAME / "Game-abc123" / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"
    ).parent.parent.parent

    rpcs3.sweep_stale_extractions()

    assert not scratch.exists()


def test_sweep_stale_extractions_is_a_noop_without_a_cache_dir(cache_dir: Path) -> None:
    """Sweep stale extractions is a no-op when the cache dir does not exist yet."""
    rpcs3.sweep_stale_extractions()  # must not raise


def test_launch_extracts_and_boots_from_an_archive_rom(
    rpcs3_dirs: dict[str, Path],
    no_boot_watchdog: list[tuple[str, tuple]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Launch extracts and boots from an archive ROM."""
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    extracted_boot = _touch(tmp_path / "extracted" / "EBOOT.BIN")
    monkeypatch.setattr(rpcs3, "_extract_and_cache", lambda archive, emulator: extracted_boot)
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"7z")

    rpcs3.Rpcs3().launch(archive, None)

    assert spawned["cmd"][-1] == str(extracted_boot)


def test_stop_keeps_the_extraction_for_the_next_launch(
    rpcs3_dirs: dict[str, Path],
    cache_dir: Path,
    no_boot_watchdog: list[tuple[str, tuple]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stop leaves the cached extraction alone so the next launch reuses it."""
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    monkeypatch.setattr(rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: None)
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"7z")
    game_dir = cache_dir / rpcs3._cache_key(archive)
    monkeypatch.setattr(
        rpcs3, "_extract_and_cache", lambda archive, emulator: _touch(game_dir / "EBOOT.BIN")
    )

    emu = rpcs3.Rpcs3()
    emu.launch(archive, None)
    emu.stop()

    assert game_dir.exists()


def test_the_cache_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache is disabled by default when RPCS3_CACHE_ENABLED is unset."""
    monkeypatch.delenv("RPCS3_CACHE_ENABLED", raising=False)
    assert rpcs3._truthy(os.environ.get("RPCS3_CACHE_ENABLED", "false")) is False


# ── CACHE_ENABLED gating of archive ROMs ────────────────────────────────


@pytest.mark.parametrize("ext", [".7z", ".zip", ".rar"])
def test_resolve_rejects_an_archive_file_when_the_cache_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ext: str
) -> None:
    """An archive ROM is refused outright when the extraction cache is disabled.

    Without the cache, an extraction would just be discarded on every
    launch, so only natively bootable formats should resolve at all.
    """
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", False)
    rom = tmp_path / f"game{ext}"
    rom.write_bytes(b"")

    assert rpcs3.Rpcs3().resolve_rom_file(rom) is None


@pytest.mark.parametrize("ext", [".7z", ".zip", ".rar"])
def test_resolve_accepts_an_archive_file_when_the_cache_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ext: str
) -> None:
    """An archive ROM resolves normally once the extraction cache is enabled."""
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", True)
    rom = tmp_path / f"game{ext}"
    rom.write_bytes(b"")

    assert rpcs3.Rpcs3().resolve_rom_file(rom) == rom


@pytest.mark.parametrize("cache_enabled", [False, True])
def test_resolve_accepts_a_pkg_file_regardless_of_the_cache_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_enabled: bool
) -> None:
    """A .pkg ROM resolves the same whether the cache is on or off.

    .pkg installs through _install_pkgs into GAME_DIR, a separate always-on
    path unrelated to the archive extraction cache.
    """
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", cache_enabled)
    rom = tmp_path / "game.pkg"
    rom.write_bytes(b"")

    assert rpcs3.Rpcs3().resolve_rom_file(rom) == rom


def test_pick_rom_file_skips_an_archive_candidate_when_the_cache_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A folder holding only an archive yields nothing when the cache is disabled."""
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", False)
    monkeypatch.setattr(rpcs3, "ROM_ROOT", tmp_path)
    folder = tmp_path / "MyGame"
    archive = folder / "game.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"")

    assert rpcs3._pick_rom_file([archive], folder) is None


def test_pick_rom_file_accepts_an_archive_candidate_when_the_cache_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A folder holding an archive yields it once the cache is enabled."""
    monkeypatch.setattr(rpcs3, "CACHE_ENABLED", True)
    monkeypatch.setattr(rpcs3, "ROM_ROOT", tmp_path)
    folder = tmp_path / "MyGame"
    archive = folder / "game.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"")

    assert rpcs3._pick_rom_file([archive], folder) == archive


def test_session_save_dirs_is_empty_without_a_launch(
    rpcs3_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """With no launch in this process, no save dir is claimed as the session's.

    A zero baseline would match every file on disk and drag every other
    title's saves into the dump, so the absence of a baseline has to fail
    closed rather than select everything.
    """
    _touch(rpcs3.USER_HOME / "savedata" / "BLES00001" / "SAVE", mtime=1000)
    _touch(rpcs3.USER_HOME / "savedata" / "BLUS99999" / "SAVE", mtime=9_000_000_000)

    emu = rpcs3.Rpcs3()

    with caplog.at_level(logging.WARNING):
        assert emu._session_save_dirs() == []

    assert "no launch" in caplog.text
