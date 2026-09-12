"""The registry and the contract every emulator in it has to hold up.

Covers registry lookups, the declarations the routes read off each emulator, and the orphan pid
record.
"""

import json
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker import emulators
from webstation_broker.emulators import base

from .conftest import SLEEPER_CMD


def test_an_unknown_name_resolves_to_nothing() -> None:
    """An unknown name resolves to nothing."""
    assert emulators.get_emulator("gameboy") is None
    assert emulators.get_emulator("") is None


def test_each_name_builds_its_own_instance() -> None:
    """Each lookup of a name builds its own instance."""
    first = emulators.get_emulator("pcsx2")
    second = emulators.get_emulator("pcsx2")

    assert isinstance(first, emulators.Pcsx2)
    assert first is not second


@pytest.mark.parametrize("name", sorted(emulators.REGISTRY))
def test_every_emulator_declares_what_the_routes_read_off_it(name: str) -> None:
    """Every emulator declares what the routes read off it."""
    emu = emulators.get_emulator(name)

    assert emu.name and emu.display_name
    assert isinstance(emu.save_subtrees, tuple)
    assert isinstance(emu.rom_extensions, tuple)
    # Boot-failure detection is a base-class field every emulator carries,
    # even though only Pcsx2 populates it today (2026-08-14 boot-failure spec).
    assert emu.boot_failed is False
    # A state slot of 0 means the routes have nowhere to put a state, so an
    # emulator that claims states has to name one.
    if emu.supports_states:
        # The state routes read the slot's file off the emulator and validate
        # a pushed name against it, so the base no-op stubs are not enough.
        assert type(emu).state_path is not emulators.Emulator.state_path
        assert type(emu).state_target is not emulators.Emulator.state_target
        assert emu.state_slot >= 0


@pytest.mark.parametrize("name", sorted(emulators.REGISTRY))
def test_every_declared_state_subtree_ships_in_the_save_archive(name: str) -> None:
    """Every declared state subtree ships in the save archive."""
    emu = emulators.get_emulator(name)

    assert isinstance(emu.state_subtrees, tuple)
    # A state subtree the dump never walks would label nothing.
    for sub in emu.state_subtrees:
        assert sub in emu.save_subtrees


@pytest.mark.parametrize("name", sorted(emulators.REGISTRY))
def test_every_archive_member_gets_a_kind_the_parent_knows(name: str) -> None:
    """Every archive member gets a kind the parent knows."""
    emu = emulators.get_emulator(name)
    kinds = {"state", "state_screenshot", "memcard", "save"}

    for sub in emu.save_subtrees:
        assert emu.save_file_kind(f"{sub}/title/data.bin") in kinds
    assert emu.save_file_kind("unmapped/data.bin") in kinds


def test_the_default_classifier_sorts_by_subtree() -> None:
    """The default classifier sorts a member by the subtree it sits in."""
    emu = emulators.get_emulator("pcsx2")

    assert emu.save_file_kind("sstates/game.p2s") == "state"
    assert emu.save_file_kind("sstates/game.png") == "state_screenshot"
    assert emu.save_file_kind("memcards/Mcd001.ps2") == "memcard"
    assert emu.save_file_kind("something/else.bin") == "save"


def test_a_subtree_name_is_matched_whole() -> None:
    """A subtree name only matches on a path boundary, never a prefix of a sibling."""
    emu = emulators.get_emulator("duckstation")

    assert emu.save_file_kind("savestates") == "state"
    assert emu.save_file_kind("savestates-old/game.p2s") == "save"


def test_an_emulator_without_a_launcher_core_reports_none() -> None:
    """An emulator that is its own backend names no core."""
    assert emulators.get_emulator("ppsspp").archive_core() is None


@pytest.mark.parametrize("name", sorted(emulators.REGISTRY))
def test_a_memory_card_comes_with_everything_the_card_routes_need(name: str) -> None:
    """A memory card comes with everything the card routes need."""
    emu = emulators.get_emulator(name)
    if emu.memory_card_subtree is None:
        assert emu.memory_card_path() is None
        return

    # The card travels on its own routes, so it has to be removable from the
    # save archive. Findability is platform-gated for an emulator whose card
    # exists on only some of the platforms it serves (Dolphin: GC, not Wii),
    # so that half of the contract is exercised in that emulator's own tests
    # instead of here. A marker is only required for emulators (PCSX2) whose
    # own runtime refuses a markerless folder.
    assert emu.memory_card_subtree in emu.save_subtrees


def test_the_desktop_launcher_needs_no_rom() -> None:
    """The desktop launcher needs no ROM."""
    assert emulators.get_emulator("desktop").requires_rom is False


def _child_of(pid: int) -> Optional[int]:
    """Find the first process reporting `pid` as its parent.

    Read out of PPid rather than /proc/<pid>/task/<pid>/children, which needs a kernel built with
    CONFIG_PROC_CHILDREN and is missing on some of them.

    Args:
        pid: The parent to look for.

    Returns:
        The child's pid, or None when nothing reports that parent.
    """
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("PPid:"):
                if int(line.split()[1]) == pid:
                    return int(entry.name)
                break
    return None


def test_reaping_kills_an_emulator_an_earlier_broker_left_running(
    pid_record: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """Reaping kills an emulator an earlier broker left running.

    A restarted broker has no handle on the emulator that outlived it, so the recorded pid is the
    only way it ever gets killed.
    """
    proc = sleeper()
    base._record_pid("fake", proc.pid, SLEEPER_CMD)

    killed = base.reap_orphan()

    assert killed["pid"] == proc.pid
    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert not pid_record.exists()


def test_reaping_leaves_a_recycled_pid_alone(
    pid_record: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """Reaping leaves a recycled pid alone.

    The pid may belong to something else entirely by now, so a record that does not match what is
    running is dropped rather than acted on.
    """
    proc = sleeper()
    base._record_pid("fake", proc.pid, ["/usr/bin/some-other-emulator"])

    assert base.reap_orphan() is None
    assert proc.poll() is None
    assert not pid_record.exists()


def test_reaping_leaves_a_pid_that_leads_no_process_group_alone(pid_record: Path) -> None:
    """Reaping leaves a pid that leads no process group alone.

    Emulators are spawned into their own session, so a recorded pid that is not a group leader is
    not the emulator, and killing its group would take down whatever unrelated process tree it
    belongs to.
    """
    parent = subprocess.Popen(["/bin/sh", "-c", "sleep 60; true"], start_new_session=True)
    try:
        deadline = time.monotonic() + 5.0
        child = None
        while child is None and time.monotonic() < deadline:
            child = _child_of(parent.pid)
            if child is None:
                time.sleep(0.1)
        assert child is not None
        base._record_pid("fake", child, base._cmdline(child))

        assert base.reap_orphan() is None
        assert parent.poll() is None
        assert not pid_record.exists()
    finally:
        parent.kill()
        parent.wait()


def test_reaping_a_record_that_names_no_command_does_nothing(
    pid_record: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """Reaping a record that names no command does nothing.

    An empty cmd matches the empty cmdline every dead pid reports, so a record that cannot identify
    its process must not be acted on.
    """
    proc = sleeper()
    pid_record.write_text(json.dumps({"name": "fake", "pid": proc.pid}))

    assert base.reap_orphan() is None
    assert proc.poll() is None
    assert not pid_record.exists()


def test_reaping_with_nothing_recorded_does_nothing(pid_record: Path) -> None:
    """Reaping with nothing recorded does nothing."""
    assert base.reap_orphan() is None


def test_a_graceful_exit_clears_the_record_the_same_as_a_kill(pid_record: Path) -> None:
    """A graceful exit clears the record the same as a kill.

    The emulators that quit over their own control channel never reach the kill path, so they have
    to drop the record themselves.
    """
    emu = emulators.get_emulator("shadps4")
    cmd = ["/bin/sh", "-c", "read line"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, start_new_session=True)
    emu._proc = proc
    base._record_pid("shadps4", proc.pid, cmd)

    emu.stop()

    assert proc.wait(timeout=10) == 0
    assert not pid_record.exists()


def test_stopping_clears_the_record_so_the_next_launch_reaps_nothing(
    pid_record: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """Stopping clears the record so the next launch reaps nothing.

    A clean stop has to take the record with it: leaving it behind would have the next activate
    hunting a pid nobody owns.
    """
    emu = emulators.Emulator()
    emu._proc = sleeper()
    base._record_pid("fake", emu._proc.pid, SLEEPER_CMD)

    emu.stop()

    assert not pid_record.exists()


def test_an_emulator_does_not_support_disc_swap_by_default() -> None:
    """An emulator does not support disc swap by default."""
    assert base.Emulator.supports_disc_swap is False


def test_swapping_a_disc_on_the_base_class_is_not_implemented() -> None:
    """Swapping a disc on the base class is not implemented."""
    with pytest.raises(NotImplementedError):
        base.Emulator().swap_disc(Path("/romm/game/disc2.chd"))


def test_the_launch_env_strips_named_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launch env strips named secrets."""
    monkeypatch.setenv("BROKER_SECRET", "s3cret")
    monkeypatch.setenv("SELKIES_MASTER_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")

    env = base.base_launch_env()

    assert "BROKER_SECRET" not in env
    assert "SELKIES_MASTER_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


@pytest.mark.parametrize(
    "name", ["SOME_API_SECRET", "OAUTH_TOKEN", "DB_PASSWORD", "AWS_ACCESS_KEY"]
)
def test_the_launch_env_strips_anything_secret_shaped(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The launch env strips anything secret-shaped."""
    monkeypatch.setenv(name, "sensitive")

    assert name not in base.base_launch_env()


def test_the_launch_env_keeps_ordinary_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launch env keeps ordinary variables."""
    monkeypatch.setenv("SOME_HARMLESS_VAR", "keep-me")

    assert base.base_launch_env()["SOME_HARMLESS_VAR"] == "keep-me"


def _no_wayland_display_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear every source `base_launch_env` reads for the display vars.

    Leaves `_detect_wayland_display` pointed at paths that don't exist, so it
    returns None instead of picking up whatever is actually running on the
    machine running the tests.
    """
    monkeypatch.delenv("BROKER_WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("BROKER_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(base, "_PROC_NET_UNIX", tmp_path / "no-such-file")
    monkeypatch.setattr(base, "_PROC_DIR", tmp_path / "no-such-proc")


def test_the_launch_env_prefers_the_broker_override_display(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch env prefers the broker override display over everything else."""
    _no_wayland_display_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BROKER_WAYLAND_DISPLAY", "wayland-9")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-5")
    monkeypatch.setenv("BROKER_DISPLAY", ":9")
    monkeypatch.setenv("DISPLAY", ":5")

    env = base.base_launch_env()

    assert env["WAYLAND_DISPLAY"] == "wayland-9"
    assert env["DISPLAY"] == ":9"


def test_the_launch_env_falls_back_to_the_inherited_display(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch env falls back to the inherited display when there is no override."""
    _no_wayland_display_env(monkeypatch, tmp_path)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-5")
    monkeypatch.setenv("DISPLAY", ":5")

    env = base.base_launch_env()

    assert env["WAYLAND_DISPLAY"] == "wayland-5"
    assert env["DISPLAY"] == ":5"


def test_the_launch_env_falls_back_to_a_default_display(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch env falls back to a hardcoded default when nothing else resolves."""
    _no_wayland_display_env(monkeypatch, tmp_path)

    env = base.base_launch_env()

    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["DISPLAY"] == ":0"


def test_the_launch_env_detects_the_wayland_display_selkies_is_capturing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch env detects the display when no override or inherited value is set."""
    _no_wayland_display_env(monkeypatch, tmp_path)
    _fake_selkies_wayland_socket(monkeypatch, tmp_path, socket_name="wayland-7")

    assert base.base_launch_env()["WAYLAND_DISPLAY"] == "wayland-7"


def _fake_selkies_wayland_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, socket_name: str
) -> None:
    """Fake a `/proc/net/unix` and a `/proc/<pid>/fd` that agree on a socket.

    Registers a listening `wayland-*` socket in a fake `/proc/net/unix` and a
    fake `selkies` process whose fd table holds that same socket's inode, the
    two things `_detect_wayland_display` cross-references.
    """
    runtime_dir = tmp_path / "xdg"
    runtime_dir.mkdir()
    monkeypatch.setattr(base, "XDG_RUNTIME_DIR", str(runtime_dir))

    inode = "123456"
    net_unix = tmp_path / "net_unix"
    net_unix.write_text(
        "Num       RefCount Protocol Flags    Type St Inode Path\n"
        f"0: 00000002 00000000 00010000 0001 01 {inode} {runtime_dir / socket_name}\n"
    )
    monkeypatch.setattr(base, "_PROC_NET_UNIX", net_unix)

    proc_dir = tmp_path / "proc"
    selkies_dir = proc_dir / "42"
    fd_dir = selkies_dir / "fd"
    fd_dir.mkdir(parents=True)
    selkies_dir.joinpath("cmdline").write_bytes(b"/lsiopy/bin/python3\x00/lsiopy/bin/selkies\x00")
    fd_dir.joinpath("5").symlink_to(f"socket:[{inode}]")
    monkeypatch.setattr(base, "_PROC_DIR", proc_dir)


def test_listening_wayland_sockets_ignores_a_socket_that_is_not_listening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `wayland-*` socket that isn't in the listening state is not returned."""
    runtime_dir = tmp_path / "xdg"
    runtime_dir.mkdir()
    monkeypatch.setattr(base, "XDG_RUNTIME_DIR", str(runtime_dir))

    net_unix = tmp_path / "net_unix"
    net_unix.write_text(
        "Num       RefCount Protocol Flags    Type St Inode Path\n"
        f"0: 00000003 00000000 00000000 0001 03 999 {runtime_dir / 'wayland-1'}\n"
    )
    monkeypatch.setattr(base, "_PROC_NET_UNIX", net_unix)

    assert base._listening_wayland_sockets() == {}


def test_detecting_the_wayland_display_finds_nothing_without_a_selkies_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Detection finds nothing when no process holds a listening socket open."""
    runtime_dir = tmp_path / "xdg"
    runtime_dir.mkdir()
    monkeypatch.setattr(base, "XDG_RUNTIME_DIR", str(runtime_dir))

    net_unix = tmp_path / "net_unix"
    net_unix.write_text(
        "Num       RefCount Protocol Flags    Type St Inode Path\n"
        f"0: 00000002 00000000 00010000 0001 01 111 {runtime_dir / 'wayland-0'}\n"
    )
    monkeypatch.setattr(base, "_PROC_NET_UNIX", net_unix)
    monkeypatch.setattr(base, "_PROC_DIR", tmp_path / "empty-proc")

    assert base._detect_wayland_display() is None
