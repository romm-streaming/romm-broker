"""The registry and the contract every emulator in it has to hold up.

Covers registry lookups, the declarations the routes read off each emulator, and the orphan pid
record.
"""

import inspect
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker import emulators, imports, saves
from webstation_broker.emulators import base, retroarch

from .conftest import DETACHED_CMD, SLEEPER_CMD, await_cmdline, await_gone, import_zip, preflight_import


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
def test_every_emulator_that_carries_saves_clears_the_last_session(name: str) -> None:
    """An emulator holding save data has to empty it before the next player arrives.

    A restore only writes the members the incoming archive names, so whatever
    the last session left under a subtree the archive does not mention survives
    into this session and into this player's dump. Both halves are asserted:
    the flag the routes read, and a hook that actually does the clearing.
    """
    emu = emulators.get_emulator(name)
    if not emu.save_subtrees:
        return

    assert emu.clears_stale_saves
    # Either activate hook may be the one that does it: the clear belongs in
    # prepare_restore for an emulator whose save tree is not ready to be
    # emptied until that hook has made it reachable.
    assert (
        type(emu).clear_working_slot is not emulators.Emulator.clear_working_slot
        or type(emu).prepare_restore is not emulators.Emulator.prepare_restore
    )


@pytest.mark.parametrize("name", sorted(emulators.REGISTRY))
def test_a_clear_takes_the_subtrees_activate_did_not_exclude(name: str) -> None:
    """Every clear accepts the subtrees the whole-card routes carry this session.

    Activate hands the exclusions positionally, so a hook that never grew the
    parameter would raise there rather than here. What each clear then deletes
    is the emulator's own layout, and is asserted in its own tests.
    """
    emu = emulators.get_emulator(name)
    signature = inspect.signature(type(emu).clear_working_slot)

    assert "excluded" in signature.parameters


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


# ---- xdg_config_dir / xdg_data_dir ----


def test_an_absolute_xdg_root_is_used_as_it_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absolute XDG root takes the app directory directly underneath it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/config")
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")

    assert base.xdg_config_dir("dolphin-emu") == Path("/custom/config/dolphin-emu")
    assert base.xdg_data_dir("dolphin-emu") == Path("/custom/data/dolphin-emu")


def test_an_unset_xdg_root_falls_back_under_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no XDG root set, the spec's `$HOME`-relative defaults are used."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/testuser")

    assert base.xdg_config_dir("Cemu") == Path("/home/testuser/.config/Cemu")
    assert base.xdg_data_dir("Cemu") == Path("/home/testuser/.local/share/Cemu")


def test_a_relative_xdg_root_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative XDG root is ignored, as the spec requires, not resolved against the cwd."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    monkeypatch.setenv("HOME", "/home/testuser")

    assert base.xdg_config_dir("azahar-emu") == Path("/home/testuser/.config/azahar-emu")


def test_no_home_at_all_falls_back_to_the_container_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither the XDG root nor `HOME` set, the container's own `/config` is used.

    s6 hands a service a near-empty environment, so the broker can genuinely
    start with no `HOME`; resolving to a relative path there would put the
    seeded config wherever the service happened to be started from.
    """
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    assert base.xdg_data_dir("dolphin-emu") == Path("/config/.local/share/dolphin-emu")


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


def test_reaping_kills_what_the_orphan_had_detached(
    pid_record: Path, desktop_tree: tuple[subprocess.Popen[bytes], int, str]
) -> None:
    """Reaping an orphan takes down the apps it had started in sessions of their own.

    This is the desktop record after a broker restart: the shell is still up with the user's
    emulators open, and killing its process group alone would clear the record while leaving those
    running with nothing that knows about them. The tag is read off the recorded pid, so the record
    itself does not have to carry one.
    """
    shell, app_pid, _tag = desktop_tree
    base._record_pid("desktop", shell.pid, SLEEPER_CMD)

    assert base.reap_orphan()["pid"] == shell.pid

    assert await_gone(app_pid, DETACHED_CMD)
    assert not pid_record.exists()


def test_the_pid_record_carries_the_session_tag(pid_record: Path, tmp_path: Path) -> None:
    """A spawn stamped with a tag records it, so a later broker does not need the process.

    The environment is readable only while the process lives, and the shell is the first thing to
    go, so a record that did not carry the tag would leave a dead session's apps unfindable.
    """
    tag = uuid.uuid4().hex

    class _Tagged(base.Emulator):
        """A stand-in emulator that spawns whatever it is handed."""

        name = "tagged"
        """Registry key this stand-in would be registered under."""
        log_path = tmp_path / "tagged.log"
        """Kept inside the test's own directory rather than /config."""

    emu = _Tagged()
    try:
        emu._spawn(SLEEPER_CMD, {**os.environ, base.SESSION_TAG_ENV: tag})
        assert json.loads(pid_record.read_text())["tag"] == tag
    finally:
        base.Emulator.stop(emu)


def test_an_untagged_spawn_records_no_tag(pid_record: Path, tmp_path: Path) -> None:
    """Only the desktop stamps a tag, so every other record stays as it was."""

    class _Plain(base.Emulator):
        """A stand-in emulator spawned without a tag, as every non-desktop session is."""

        name = "plain"
        """Registry key this stand-in would be registered under."""
        log_path = tmp_path / "plain.log"
        """Kept inside the test's own directory rather than /config."""

    emu = _Plain()
    try:
        emu._spawn(SLEEPER_CMD, dict(os.environ))
        assert "tag" not in json.loads(pid_record.read_text())
    finally:
        base.Emulator.stop(emu)


def test_a_tty_spawn_gives_the_child_a_terminal_for_stdin(pid_record: Path, tmp_path: Path) -> None:
    """The child still sees a terminal on stdin after the broker has let go of its end.

    The child checks only after a delay, by which point `_spawn` has closed both of its pty fds.
    Were the child not holding the master itself, that close would hang the terminal up and
    `isatty` would come back false.
    """

    class _Tty(base.Emulator):
        """A stand-in emulator whose stdin should be a terminal."""

        name = "tty"
        """Registry key this stand-in would be registered under."""
        log_path = tmp_path / "tty.log"
        """Kept inside the test's own directory rather than /config."""

    emu = _Tty()
    cmd = [sys.executable, "-c", "import os, time; time.sleep(0.5); print('isatty', os.isatty(0))"]
    try:
        emu._spawn(cmd, dict(os.environ), stdin_tty=True)
        assert emu._proc is not None
        emu._proc.wait(timeout=10)
        assert "isatty True" in emu.log_path.read_text()
    finally:
        base.Emulator.stop(emu)


def test_a_spawn_cannot_ask_for_both_a_pipe_and_a_terminal(tmp_path: Path) -> None:
    """Asking for both stdin kinds is refused before anything starts."""

    class _Both(base.Emulator):
        """A stand-in emulator asked for two stdins at once."""

        name = "both"
        """Registry key this stand-in would be registered under."""
        log_path = tmp_path / "both.log"
        """Kept inside the test's own directory rather than /config."""

    emu = _Both()
    with pytest.raises(ValueError):
        emu._spawn(SLEEPER_CMD, dict(os.environ), stdin_pipe=True, stdin_tty=True)
    assert emu._proc is None


def test_reaping_closes_apps_a_shell_that_already_exited_left_open(
    pid_record: Path, desktop_tree: tuple[subprocess.Popen[bytes], int, str]
) -> None:
    """A record whose process is gone is still swept by the tag it carries.

    Quitting the desktop from inside the GUI ends the shell and leaves every app it started
    running. There is then no environment left to read a tag from, and dropping the record on the
    grounds that its pid no longer matches would strand those apps for good.
    """
    shell, app_pid, tag = desktop_tree
    base._record_pid("desktop", shell.pid, SLEEPER_CMD, tag=tag)
    shell.kill()
    shell.wait()

    assert base.reap_orphan() is None

    assert await_gone(app_pid, DETACHED_CMD)
    assert not pid_record.exists()


def test_reaping_does_not_sweep_by_a_tag_read_off_a_recycled_pid(
    pid_record: Path, desktop_tree: tuple[subprocess.Popen[bytes], int, str]
) -> None:
    """A record whose pid now belongs to something else is not swept by that process's tag.

    A record written before the tag was stored leaves the pid as the only place to read one from,
    and a pid the kernel has reissued carries whatever tag its new owner was launched with. Here
    that owner is a live desktop shell, so reading the tag before checking that the pid is still
    the recorded process would take a running session's apps down with a stale record.
    """
    shell, app_pid, _tag = desktop_tree
    base._record_pid("desktop", shell.pid, ["/usr/bin/some-other-emulator"])

    assert base.reap_orphan() is None

    assert base._cmdline(app_pid) == DETACHED_CMD
    assert shell.poll() is None
    assert not pid_record.exists()


def test_reaping_closes_apps_the_orphan_left_open_when_the_orphan_is_out_of_reach(
    pid_record: Path,
    desktop_tree: tuple[subprocess.Popen[bytes], int, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded pid that cannot be reached does not take its apps' only handle with it.

    The pid is checked against `/proc` and signalled a moment later, so it can die in between and
    leave the group lookup raising. That is the shell-already-exited case one moment later, and
    dropping the record there would strand the apps the shell detached for good.
    """
    shell, app_pid, tag = desktop_tree
    base._record_pid("desktop", shell.pid, SLEEPER_CMD, tag=tag)
    shell.kill()
    shell.wait()
    real_cmdline = base._cmdline

    def _one_step_behind(pid: int) -> list[str]:
        """Report the reaped shell as still running its recorded argv.

        Standing in for the `/proc` read that happened just before the process exited, which is
        what puts the reaper past the identity check and into a group lookup on a dead pid. Every
        other process is reported as it really is, so the sweep still works off the real table.

        Args:
            pid: The process to look up.

        Returns:
            The recorded argv for the shell, and the real argv for anything else.
        """
        return SLEEPER_CMD if pid == shell.pid else real_cmdline(pid)

    monkeypatch.setattr(base, "_cmdline", _one_step_behind)

    assert base.reap_orphan() is None

    assert await_gone(app_pid, DETACHED_CMD)
    assert not pid_record.exists()


def test_reaping_a_tagged_pid_that_leads_no_group_leaves_its_group_alone(
    pid_record: Path, tmp_path: Path
) -> None:
    """A pid refused for leading no process group is refused by the tag sweep as well.

    Signalling that pid reaches a group the record does not account for, which is why the reaper
    gives up on it. The sweep addresses groups the same way, so without being told to skip that
    pid it walks into the very group the branch above it exists to keep out of.
    """
    tag = uuid.uuid4().hex
    pid_file = tmp_path / "tagged-child.pid"
    # The tag goes on the forked child only, so the group leader is untagged and the sweep has
    # exactly one way to reach it: through the child it is told to leave alone.
    script = (
        "import os\n"
        f"env = dict(os.environ, **{{{base.SESSION_TAG_ENV!r}: {tag!r}}})\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        f"    os.execve({SLEEPER_CMD[0]!r}, {SLEEPER_CMD!r}, env)\n"
        f"tmp = {str(pid_file)!r} + '.tmp'\n"
        "open(tmp, 'w').write(str(pid))\n"
        f"os.rename(tmp, {str(pid_file)!r})\n"
        f"os.execv({SLEEPER_CMD[0]!r}, {SLEEPER_CMD!r})\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        deadline = time.monotonic() + 10.0
        while not pid_file.exists():
            if time.monotonic() >= deadline:
                pytest.fail("the parent stand-in never reported the pid it forked")
            time.sleep(0.01)
        child = int(pid_file.read_text())
        await_cmdline(child, SLEEPER_CMD)
        assert os.getpgid(child) == parent.pid
        base._record_pid("desktop", child, SLEEPER_CMD, tag=tag)

        assert base.reap_orphan() is None

        assert parent.poll() is None
        assert base._cmdline(child) == SLEEPER_CMD
        assert not pid_record.exists()
    finally:
        parent.kill()
        parent.wait()


def test_a_corrupt_record_is_dropped_rather_than_read_again(pid_record: Path) -> None:
    """A record that cannot be parsed is cleared, not left to be re-read on every activate.

    Nothing can be recovered from it, so keeping it only buys the same warning once per session
    start and once per activate for as long as the container runs.
    """
    pid_record.write_text("{not json")

    assert base.reap_orphan() is None

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


def test_the_launch_env_points_at_the_labwc_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launch env names labwc's socket and Xwayland, whatever the broker inherited."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("DISPLAY", ":1")

    env = base.base_launch_env()

    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["DISPLAY"] == ":0"


_IMPORTING: frozenset[str] = frozenset({"duckstation", "flycast", "retroarch"})
"""The emulators that accept declared imports; every other one inherits the refusing base hooks."""


@pytest.mark.parametrize("name", sorted(set(emulators.REGISTRY) - _IMPORTING))
def test_an_emulator_without_import_support_accepts_nothing(name: str) -> None:
    """An emulator that has not opted in declares an empty spec.

    Args:
        name: The registry name.
    """
    emu = emulators.get_emulator(name)
    assert emu is not None

    assert emu.import_spec().kinds == ()


@pytest.mark.parametrize("name", sorted(set(emulators.REGISTRY) - _IMPORTING))
def test_an_emulator_without_import_support_keeps_the_base_hooks(name: str) -> None:
    """An emulator that has not opted in inherits the base import hooks, which refuse everything.

    The behavioural test below only reaches the kind gate, so an override past
    it, or a platform-dependent spec, would slip by it. An emulator that gains
    real hooks moves into `_IMPORTING`.

    Args:
        name: The registry name.
    """
    emu = emulators.get_emulator(name)
    assert emu is not None

    for hook in ("import_spec", "place_import", "validate_import_plan", "identity_source"):
        assert getattr(type(emu), hook) is getattr(base.Emulator, hook), f"{name} overrides {hook}"


@pytest.mark.parametrize("name", sorted(set(emulators.REGISTRY) - _IMPORTING))
def test_an_emulator_without_import_support_refuses_a_declared_import(name: str) -> None:
    """Preflight refuses each member with `kind_not_accepted`.

    Args:
        name: The registry name.
    """
    emu = emulators.get_emulator(name)
    assert emu is not None
    body = import_zip({".import/save/a.sav": b"x"})

    result = preflight_import(emu, body, rom_file=None)

    assert [(r.reason, r.member) for r in result.refusals] == [("kind_not_accepted", ".import/save/a.sav")]
    assert result.placements == ()


@pytest.mark.parametrize("name", sorted(_IMPORTING))
def test_an_importing_emulator_places_members_itself(name: str) -> None:
    """An emulator listed as importing overrides the spec and placement hooks.

    Guards the list itself: a name left in it after its hooks were removed
    would otherwise escape every test above.

    Args:
        name: The registry name.
    """
    emu = emulators.get_emulator(name)
    assert emu is not None

    for hook in ("import_spec", "place_import"):
        assert getattr(type(emu), hook) is not getattr(base.Emulator, hook), f"{name} inherits {hook}"


@pytest.mark.parametrize("name", sorted(emulators.REGISTRY))
def test_restore_subtrees_covers_every_save_subtree(name: str) -> None:
    """Whatever a restore may write into includes every subtree a dump ships.

    Args:
        name: The registry name.
    """
    emu = emulators.get_emulator(name)
    assert emu is not None

    assert set(emu.save_subtrees) <= set(emu.restore_subtrees) | {
        s for s in emu.save_subtrees if any(s.startswith(r + "/") for r in emu.restore_subtrees)
    }


def _spec_cases() -> list[tuple[str, Optional[str]]]:
    """List every registry emulator once, and RetroArch once more per platform.

    RetroArch's spec depends on the loaded platform, so each mapped slug is
    checked on its own, and so is one no table maps.

    Returns:
        `(name, platform)` pairs; the platform is None except in RetroArch's extra cases.
    """
    cases: list[tuple[str, Optional[str]]] = [(name, None) for name in sorted(emulators.REGISTRY)]
    cases += [("retroarch", slug) for slug in sorted(retroarch.PLATFORMS)]
    cases.append(("retroarch", "not-a-platform"))
    return cases


def _on(name: str, platform: Optional[str]) -> base.Emulator:
    """Build a registry emulator with a platform loaded, as discovery does.

    Args:
        name: The registry name.
        platform: The RomM platform slug, or None.

    Returns:
        A fresh instance.
    """
    emu = emulators.get_emulator(name)
    assert emu is not None
    emu.platform = platform
    return emu


@pytest.mark.parametrize(("name", "platform"), _spec_cases())
def test_a_state_kind_rides_the_archive_and_waits_for_a_resume_slot(
    name: str, platform: Optional[str]
) -> None:
    """A declared state rides the archive, at most one per archive, and needs `save.resume_slot`.

    An archive state resumes through the slot the broker saves into on exit,
    so it needs no mid-session states: Flycast and DuckStation take one with
    `supports_states` off. Counting the archive's own state keeps two states
    from competing for that one slot.

    Args:
        name: The registry name.
        platform: The loaded platform.
    """
    spec = _on(name, platform).import_spec()
    state = spec.kind("state")

    assert (state is not None) == (spec.state_channel == "archive")
    if state is not None:
        assert (state.requires_resume_slot, state.max_members, state.counts_v1) == (True, 1, True)


@pytest.mark.parametrize(("name", "platform"), _spec_cases())
def test_a_push_state_channel_needs_mid_session_states(name: str, platform: Optional[str]) -> None:
    """An emulator only sends states to the push route when that route can take them.

    `state_uses_push` tells RomM to send the state to
    PUT /api/session/state-file after activate, and that route answers 400
    when `supports_states` is off.

    Args:
        name: The registry name.
        platform: The loaded platform.
    """
    emu = _on(name, platform)

    assert emu.import_spec().state_channel != "push" or emu.supports_states


@pytest.mark.parametrize(("name", "platform"), _spec_cases())
def test_each_kind_is_declared_once_and_only_a_state_needs_a_resume_slot(
    name: str, platform: Optional[str]
) -> None:
    """A spec declares each kind at most once, and only its state waits for a resume slot.

    `ImportSpec.kind` answers the first declaration, so a second one would be
    listed by discovery and never used.

    Args:
        name: The registry name.
        platform: The loaded platform.
    """
    kinds = _on(name, platform).import_spec().kinds

    assert len({k.kind for k in kinds}) == len(kinds)
    assert all(k.requires_resume_slot == (k.kind == "state") for k in kinds)


@pytest.mark.parametrize(("name", "platform"), _spec_cases())
def test_a_spec_names_no_card_subtree_but_the_emulators_own(name: str, platform: Optional[str]) -> None:
    """Discovery's `card_subtree` is either absent or the subtree the restore leaves out.

    The restore leaves out `memory_card_subtree` when the card is synced on
    its own routes. A spec naming any other subtree would tell RomM the card
    lives somewhere the broker does not treat as one.

    Args:
        name: The registry name.
        platform: The loaded platform.
    """
    emu = _on(name, platform)

    assert emu.import_spec().card_subtree in (None, emu.memory_card_subtree)


@pytest.mark.parametrize(("name", "platform"), _spec_cases())
def test_only_a_declared_kind_reaches_place_import(
    monkeypatch: pytest.MonkeyPatch, name: str, platform: Optional[str]
) -> None:
    """The kind gate stops every undeclared kind before the emulator's own hook sees it.

    A hook is written for the kinds its spec declares, so a member of any
    other kind must never reach it. A state on a push channel is pointed at
    the push route; every other undeclared member answers
    `kind_not_accepted`.

    Args:
        monkeypatch: Pytest's attribute patcher.
        name: The registry name.
        platform: The loaded platform.
    """
    emu = _on(name, platform)
    declared = emu.import_spec()
    seen: list[str] = []

    def spy(
        member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> imports.ImportRefusal:
        """Record the member's kind and refuse it.

        Args:
            member: The member that got past the kind gate.
            spec: The emulator's spec.
            ctx: The launch context.

        Returns:
            An `unrecognised_layout` refusal, so nothing is placed.
        """
        seen.append(member.kind)
        return imports.ImportRefusal("unrecognised_layout", member.name, None)

    monkeypatch.setattr(emu, "place_import", spy)
    members = [".import/save/Game.srm", ".import/state/Game.state", ".import/memcard/Game.mcd"]
    body = import_zip({m: b"x" for m in members})

    result = preflight_import(emu, body, rom_file=None, resume_slot=emu.state_slot)

    expected = []
    for m in members:
        kind = m.split("/")[1]
        if declared.kind(kind) is not None:
            expected.append((m, "unrecognised_layout"))
        elif kind == "state" and declared.state_channel == "push":
            expected.append((m, "state_uses_push"))
        else:
            expected.append((m, "kind_not_accepted"))
    assert sorted(seen) == sorted(k.kind for k in declared.kinds)
    assert sorted((r.member, r.reason) for r in result.refusals) == sorted(expected)


@pytest.mark.parametrize("name", ["duckstation", "flycast"])
def test_a_flat_card_is_an_ordinary_save(name: str) -> None:
    """Flycast and DuckStation keep their cards among the saves, not on the memory card routes.

    Each card is a file in a save subtree, so an imported card is restored
    with the rest of the archive, and syncing the card separately never
    turns it away as `memcard_synced_separately`.

    Args:
        name: The registry name.
    """
    emu = _on(name, None)

    assert (emu.memory_card_subtree, emu.import_spec().card_subtree) == (None, None)


_EXAMPLE_PLATFORM: dict[str, str] = {"duckstation": "psx", "flycast": "dc", "retroarch": "gb"}
"""The platform each importing emulator's examples below are placed on."""

_EXAMPLES: list[tuple[str, str, bytes]] = [
    ("duckstation", ".import/save/card.mcd", bytes(131072)),
    ("duckstation", ".import/memcard/card.mcr", bytes(131072)),
    ("duckstation", ".import/state/SLUS-00594_resume.sav", b"progress"),
    ("flycast", ".import/save/vmu_save_B2.bin", bytes(131072)),
    ("flycast", ".import/save/dc_nvmem.bin", b"flash"),
    ("flycast", ".import/memcard/card.bin", bytes(131072)),
    ("flycast", ".import/state/Game.state", b"progress"),
    ("retroarch", ".import/save/Game.srm", b"sram"),
]
"""One member each importing emulator accepts, for every kind it accepts on its example platform."""


def test_every_accepted_kind_has_an_example() -> None:
    """Each kind an importing emulator can place has an example in `_EXAMPLES`.

    A kind with shapes on the example platform is one the emulator places,
    so a newly accepted kind fails here until it has an example.
    """
    assert set(_EXAMPLE_PLATFORM) == _IMPORTING
    accepted = {
        (name, k.kind)
        for name, platform in _EXAMPLE_PLATFORM.items()
        for k in _on(name, platform).import_spec().kinds
        if k.shapes
    }

    assert accepted == {(name, member.split("/")[1]) for name, member, _ in _EXAMPLES}


@pytest.mark.parametrize(("name", "member", "data"), _EXAMPLES)
def test_an_accepted_member_lands_inside_the_save_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, member: str, data: bytes
) -> None:
    """An accepted member, and every sidecar it brings, lands strictly inside a restored subtree.

    `check_plan` holds a member's own destination to the save tree, but not
    its sidecars. A sidecar written outside the tree is never shipped by the
    exit dump, so it is lost with the session, and never cleared, so it
    outlives it.

    Args:
        monkeypatch: Pytest's attribute patcher.
        tmp_path: Per-test scratch directory.
        name: The registry name.
        member: The import member.
        data: Its bytes.
    """
    emu = _on(name, _EXAMPLE_PLATFORM[name])
    monkeypatch.setattr(emu, "save_root", tmp_path / "data")
    rom = tmp_path / "Game.bin"
    rom.write_bytes(b"rom")

    result = preflight_import(emu, import_zip({member: data}), rom_file=rom, resume_slot=emu.state_slot)

    assert result.refusals == ()
    assert [p.member.name for p in result.placements] == [member]
    subtrees = tuple(emu.restore_subtrees)
    for placement in result.placements:
        for dest in (placement.dest, *(d for d, _ in placement.sidecars)):
            assert saves.under_subtrees(dest, subtrees), dest
