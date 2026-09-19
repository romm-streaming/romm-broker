"""Emulator interface and shared launch plumbing.

Defines the `Emulator` base class every launcher in this package subclasses, the
environment apps are launched into, and the on-disk pid record that lets a broker
process kill an emulator it never spawned.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any, Optional, Union

from .. import imports

log = logging.getLogger(__name__)

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")
"""The session's runtime directory, from `XDG_RUNTIME_DIR` (default `/config/.XDG`)."""

PID_FILE = Path(os.environ.get("BROKER_PID_FILE", "/config/broker-emulator.json"))
"""Where the running emulator's pid is recorded, from `BROKER_PID_FILE`.

Defaults to `/config/broker-emulator.json`. The record exists so the emulator can
still be killed by a broker process that never spawned it. Emulators are started
in their own session, so nothing else ties them to the broker: a broker restart
(an s6 bounce, or uvicorn --reload picking up an edit mid-session) otherwise
leaves one playing with no handle on it, and the next launch stacks a second
emulator on top of the first.
"""

# Explicitly named broker secrets, plus a suffix pattern for anything shaped
# like one, stripped from every spawned emulator's environment. RetroArch in
# particular dlopen()s third-party cores with no sandboxing; a compromised
# core inheriting these could impersonate the broker's own API client.
_SENSITIVE_ENV_VARS = {"BROKER_SECRET", "SELKIES_MASTER_TOKEN", "GITHUB_TOKEN"}
_SENSITIVE_ENV_SUFFIXES = ("_SECRET", "_TOKEN", "_PASSWORD", "_KEY")

_DEFAULT_TERM_TIMEOUT = 5.0
"""Seconds SIGTERM gets before SIGKILL when nothing names a longer grace."""

_DEFAULT_KILL_TIMEOUT = 10.0
"""Seconds SIGKILL gets to land before the emulator is written off as unkillable.

Only spent on a process that already ignored SIGTERM for its whole
`term_timeout`, so this is the uninterruptible-sleep case (a stuck FUSE or NFS
read), not a slow shutdown: nothing the emulator does with the time can change
the outcome. Long enough for that I/O to come back, short enough that the exit
route still answers.
"""

SESSION_TAG_ENV = "WEBSTATION_SESSION_TAG"
"""Env var stamping a launched session, so what it starts can be found again at teardown.

Only the desktop sets it (see `Desktop.launch`). The desktop shell starts each
app with a double fork, which leaves the app parented to pid 1 and sitting in a
session whose leader has already exited, so nothing in the process tree ties it
back to the session that started it. An inherited environment variable does,
and survives the app re-execing itself, which the launcher-style entries in the
menu do.
"""

_PROC_DIR = Path("/proc")
"""Process table root, walked to find the apps carrying a session tag."""

WAYLAND_DISPLAY = "wayland-0"
"""labwc's socket under `XDG_RUNTIME_DIR`, the nested session every app renders into.

Selkies' own compositor listens on `wayland-1` and labwc runs inside it as a
client, so its whole desktop is what gets captured; an app that connects to
`wayland-1` directly sits outside the desktop.
"""

X_DISPLAY = ":0"
"""The Xwayland server labwc hosts.

The container's own `DISPLAY` names the Xvfb server of the X11 image
variant, which never starts in Wayland mode.
"""


def _xdg_dir(app: str, var: str, fallback: str) -> Path:
    """Resolve one XDG directory the way a Linux app following the spec does.

    A relative `XDG_*` value is ignored, as the spec requires, rather than
    resolved against the working directory the broker happens to have.

    Args:
        app: The application's own directory name under the XDG root.
        var: The XDG environment variable to honour when set to an absolute path.
        fallback: The path under `$HOME` used otherwise, such as `.config`.

    Returns:
        The app's directory under the chosen root.
    """
    xdg = os.environ.get(var)
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / app
    return Path(os.environ.get("HOME", "/config")) / fallback / app


def xdg_config_dir(app: str) -> Path:
    """Resolve an app's config directory: `$XDG_CONFIG_HOME/<app>`, else `$HOME/.config/<app>`.

    Args:
        app: The application's own directory name, such as `dolphin-emu`.

    Returns:
        The app's config directory.
    """
    return _xdg_dir(app, "XDG_CONFIG_HOME", ".config")


def xdg_data_dir(app: str) -> Path:
    """Resolve an app's data directory: `$XDG_DATA_HOME/<app>`, else `$HOME/.local/share/<app>`.

    Args:
        app: The application's own directory name, such as `dolphin-emu`.

    Returns:
        The app's data directory.
    """
    return _xdg_dir(app, "XDG_DATA_HOME", ".local/share")


def base_launch_env() -> dict[str, str]:
    """Build the environment apps are launched into.

    This is the broker's own environment, pointed at the labwc session's
    displays (`WAYLAND_DISPLAY` and `X_DISPLAY`), with secret-shaped variables
    stripped out (see `_SENSITIVE_ENV_VARS`).

    Returns:
        A copy of the broker's environment with the display variables set and
        the emulator binary directories appended to `PATH`.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _SENSITIVE_ENV_VARS and not k.endswith(_SENSITIVE_ENV_SUFFIXES)
    }
    env["WAYLAND_DISPLAY"] = WAYLAND_DISPLAY
    env["DISPLAY"] = X_DISPLAY
    # s6 services get a minimal PATH; emulator binaries live in /usr/games.
    path = env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    for extra in ("/usr/local/bin", "/usr/bin", "/usr/games", "/usr/local/games"):
        if extra not in path.split(":"):
            path = f"{path}:{extra}"
    env["PATH"] = path
    return env


def _record_pid(
    name: str,
    pid: int,
    cmd: list[str],
    term_timeout: Optional[float] = None,
    tag: Optional[str] = None,
) -> None:
    """Write the running emulator's pid record to `PID_FILE`.

    Written through a temp file: the record exists for the case where the broker
    dies, and a broker that dies mid-write would otherwise leave half a line of
    JSON and no way to find the emulator it left behind.

    Args:
        name: The emulator's `name`, so the reaper can say what it killed.
        pid: The spawned process's pid.
        cmd: The argv it was spawned with, used later to confirm the pid still
            runs that command.
        term_timeout: The emulator's own SIGTERM grace, stored so a later
            broker process reaps it on its own teardown budget rather than a
            fixed one. None leaves it out and the reaper falls back.
        tag: The session tag the process was launched with, stored because it
            is otherwise readable only off the live process: a shell that exits
            before its teardown takes the only copy with it, and the apps it
            detached would then be unfindable (see `SESSION_TAG_ENV`).

    Raises:
        OSError: When the record cannot be written. A session whose emulator
            has no record survives a broker restart unreapable, so the caller
            has to know rather than find out at the next launch.
    """
    tmp = PID_FILE.with_suffix(".tmp")
    record: dict[str, Any] = {"name": name, "pid": pid, "cmd": cmd}
    if term_timeout is not None:
        record["term_timeout"] = term_timeout
    if tag is not None:
        record["tag"] = tag
    try:
        tmp.write_text(json.dumps(record))
        tmp.replace(PID_FILE)
    except OSError as exc:
        log.error("could not record %s pid %d at %s: %s", name, pid, PID_FILE, exc)
        raise


def _clear_pid_record() -> None:
    """Remove `PID_FILE`, ignoring its absence and logging any other failure."""
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        log.debug("no emulator pid record to clear")
    except OSError as exc:
        log.warning("could not clear emulator pid record: %s", exc)


def _cmdline(pid: int) -> list[str]:
    """Read a process's argv out of `/proc`.

    Args:
        pid: The process to look up.

    Returns:
        The argv as a list of strings, or an empty list when the process is gone
        or unreadable.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        log.debug("could not read cmdline for pid %d: %s", pid, exc)
        return []
    return [part for part in raw.decode(errors="replace").split("\0") if part]


def _record_term_timeout(record: dict[str, Any]) -> float:
    """The SIGTERM grace a pid record asks for, in seconds.

    Args:
        record: The decoded pid record.

    Returns:
        The record's `term_timeout` when it is a usable positive number, else
        `_DEFAULT_TERM_TIMEOUT`. Records written before the field existed, and
        anything that did not survive the JSON round trip as a number, land on
        the fallback rather than on a grace of zero.
    """
    grace = record.get("term_timeout")
    if isinstance(grace, bool) or not isinstance(grace, (int, float)):
        return _DEFAULT_TERM_TIMEOUT
    if grace <= 0:
        log.warning(
            "pid record for %s names a non-positive term_timeout %s, using %s s",
            record.get("name", "emulator"),
            grace,
            _DEFAULT_TERM_TIMEOUT,
        )
        return _DEFAULT_TERM_TIMEOUT
    return float(grace)


def _environ(pid: int) -> dict[str, str]:
    """Read a process's environment out of `/proc`.

    Args:
        pid: The process to look up.

    Returns:
        The environment as a dict, or an empty one when the process is gone or
        belongs to another user. Only the block the process was exec'd with is
        visible here, which is the whole point: it is what the process
        inherited and cannot quietly drop.
    """
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        log.debug("could not read the environment of pid %d: %s", pid, exc)
        return {}
    env = {}
    for entry in raw.decode(errors="replace").split("\0"):
        key, sep, value = entry.partition("=")
        if sep:
            env[key] = value
    return env


def session_tag(pid: int) -> Optional[str]:
    """Read the session tag a process was launched with, if it carries one.

    Args:
        pid: The process to look up.

    Returns:
        The value of `SESSION_TAG_ENV` in the process's environment, or None
        when it has none.
    """
    return _environ(pid).get(SESSION_TAG_ENV) or None


def tagged_processes(tag: str, exclude: Collection[int] = ()) -> list[tuple[int, list[str]]]:
    """Find every process carrying a session tag.

    The tag is an environment variable, so it reaches an app however it was
    started: a process inherits its parent's environment across fork and exec
    and cannot shed it, which is what makes this hold where the process tree
    does not. A desktop app is double-forked away from its parent and into a
    session led by a pid that has already exited, so by the time the session
    ends nothing in the tree still connects it to the shell that started it.

    Args:
        tag: The tag to match, as returned by `session_tag`.
        exclude: Pids to leave out of the result, normally the tagged process
            that is being stopped through its own handle.

    Returns:
        A `(pid, argv)` pair per match. The argv is what the pid was running
        when it was found, which is what lets a later kill tell it apart from
        whatever the kernel may have given the pid to since.
    """
    found: list[tuple[int, list[str]]] = []
    for entry in _PROC_DIR.glob("[0-9]*"):
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if pid in exclude or pid == os.getpid():
            continue
        if _environ(pid).get(SESSION_TAG_ENV) != tag:
            continue
        cmd = _cmdline(pid)
        if cmd:
            found.append((pid, cmd))
    return found


def _still_running(pid: int, cmd: list[str]) -> bool:
    """Whether a pid is still running the argv it was snapshotted with.

    Args:
        pid: The process to check.
        cmd: The argv the process was running when it was snapshotted.

    Returns:
        True only while the pid is alive and its argv still matches, which is
        false for a pid that exited and false for one the kernel handed to an
        unrelated process since.
    """
    return _cmdline(pid) == cmd


def _signal_process(entry: tuple[int, list[str]], sig: signal.Signals) -> bool:
    """Send one signal to a snapshotted process, addressing its whole group.

    The group is what carries an app's own helper processes, so signalling it
    rather than the pid is what takes an emulator's children down with it. The
    group normally outlives the pid that led it: the desktop shell's second
    fork leaves each app in a group whose leader has already exited, and a
    group lives as long as any member of it does.

    Args:
        entry: The `(pid, argv)` pair to signal.
        sig: The signal to send.

    Returns:
        True when the signal was delivered, False when the process was already
        gone, had been replaced by an unrelated one, or could not be signalled.
    """
    pid, cmd = entry
    if not _still_running(pid, cmd):
        return False
    try:
        pgid = os.getpgid(pid)
        # Signalling a whole process group is worth one guard against ever
        # aiming one at the broker: the sweep is driven by what /proc reports,
        # and a match on the broker's own group would end the session that is
        # running the teardown.
        if pid == os.getpid() or pgid == os.getpgid(0):
            log.error(
                "refusing to signal pid %d (group %d), which is the broker's own", pid, pgid
            )
            return False
        # Looked at again with the group already resolved, so the last thing
        # before the signal is a fresh confirmation that the pid is still the
        # process that was snapshotted. The snapshot is taken before the shell
        # is signalled and acted on a grace period later, and a pid the kernel
        # reissued in between would otherwise take its new group down with it.
        if not _still_running(pid, cmd):
            return False
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError) as exc:
        log.warning("could not signal leftover pid %d (%s): %s", pid, cmd[0], exc)
        return False
    return True


def _await_exit(
    entries: list[tuple[int, list[str]]], timeout: float
) -> list[tuple[int, list[str]]]:
    """Wait out a grace period and report which snapshotted processes are left.

    Args:
        entries: The `(pid, argv)` pairs to wait on.
        timeout: Seconds to wait before giving up on the stragglers.

    Returns:
        The entries still running their snapshotted argv when the time ran out.
    """
    remaining = [entry for entry in entries if _still_running(*entry)]
    deadline = time.monotonic() + timeout
    while remaining and time.monotonic() < deadline:
        time.sleep(0.2)
        remaining = [entry for entry in remaining if _still_running(*entry)]
    return remaining


def term_processes(snapshot: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
    """SIGTERM every process in a snapshot without waiting on any of them.

    Split from the waiting half so a caller with its own grace period to spend
    can start this one first and let the two run down together. `kill_processes`
    is the version for callers with nothing else to do meanwhile.

    Args:
        snapshot: `(pid, argv)` pairs, normally from `tagged_processes`.

    Returns:
        The entries the signal reached, which is what `kill_survivors` then
        waits on. Empty is the ordinary case: it means whatever was launched
        had already been closed.
    """
    signalled = [entry for entry in snapshot if _signal_process(entry, signal.SIGTERM)]
    if signalled:
        log.info(
            "stopping %d process(es) left behind: %s",
            len(signalled),
            ", ".join(f"{cmd[0]} (pid {pid})" for pid, cmd in signalled),
        )
    return signalled


def kill_survivors(
    signalled: list[tuple[int, list[str]]],
    deadline: float,
    kill_timeout: float = _DEFAULT_KILL_TIMEOUT,
) -> int:
    """Wait out the rest of a SIGTERM grace period, then SIGKILL what is left.

    The grace is given as a deadline rather than a duration because the clock
    starts at the signal, not here: the caller may have spent some of it
    already, and charging it again would double the wait.

    Args:
        signalled: `(pid, argv)` pairs SIGTERM reached, from `term_processes`.
        deadline: `time.monotonic()` value the SIGTERM grace expires at.
        kill_timeout: Seconds SIGKILL gets before the survivors are written off.

    Returns:
        How many of the signalled processes are confirmed gone. Anything that
        outlived SIGKILL is left out, so the count never claims to have closed
        something still running.
    """
    if not signalled:
        return 0
    survivors = _await_exit(signalled, max(deadline - time.monotonic(), 0.0))
    if survivors:
        # How long this call waited is not how long the process was given: the
        # grace started at the signal, which the caller may have sent well
        # before handing the wait over here.
        log.warning(
            "%d leftover process(es) were still running when the SIGTERM grace ran out, "
            "killing: %s",
            len(survivors),
            ", ".join(f"{cmd[0]} (pid {pid})" for pid, cmd in survivors),
        )
        for entry in survivors:
            _signal_process(entry, signal.SIGKILL)
        survivors = _await_exit(survivors, kill_timeout)
        if survivors:
            log.error(
                "%d leftover process(es) outlived SIGKILL and are still running: %s",
                len(survivors),
                ", ".join(f"{cmd[0]} (pid {pid})" for pid, cmd in survivors),
            )
    return len(signalled) - len(survivors)


def kill_processes(
    snapshot: list[tuple[int, list[str]]],
    term_timeout: float = _DEFAULT_TERM_TIMEOUT,
    kill_timeout: float = _DEFAULT_KILL_TIMEOUT,
) -> int:
    """Put down every process in a snapshot, SIGTERM first and SIGKILL after.

    Every process is signalled before any of them is waited on, so a desktop
    session that left four emulators running costs one grace period rather than
    four in a row on the exit route.

    Args:
        snapshot: `(pid, argv)` pairs, normally from `tagged_processes`.
        term_timeout: Seconds SIGTERM gets before the escalation to SIGKILL.
        kill_timeout: Seconds SIGKILL gets before the survivors are written off.

    Returns:
        How many of the snapshotted processes were signalled and are confirmed
        gone. Zero is the ordinary case: it means whatever was launched had
        already been closed. Anything that outlived SIGKILL is left out, so the
        count never claims to have closed something still running.
    """
    signalled = term_processes(snapshot)
    return kill_survivors(signalled, time.monotonic() + term_timeout, kill_timeout)


def reap_orphan() -> Optional[dict[str, Any]]:
    """Kill an emulator left running by an earlier broker process.

    Only ever kills the pid the broker itself recorded, and only while that pid
    is still running the command it was recorded with, so a recycled pid is
    left alone. The process group gets SIGTERM, then SIGKILL if it is still
    running the recorded command once the grace in the record has passed. That
    grace is the emulator's own `term_timeout`, so an orphan gets the same
    teardown budget here as it would from `Emulator.stop`; a record written
    without one falls back to `_DEFAULT_TERM_TIMEOUT`. The record is cleared
    whatever happens.

    Anything sharing the orphan's session tag goes with it, which is what a
    reaped desktop record means in practice: the apps configured through that
    session are in neither its process group nor its process tree, and would
    otherwise be left running with nothing that knows about them (see
    `SESSION_TAG_ENV` and `Desktop.stop`). That sweep also runs when the record
    names a process that is already gone or out of reach: a shell that exited
    on its own is what strands the apps it detached, not what makes them safe
    to leave.

    Returns:
        The record that was acted on, a dict with `{"name", "pid", "cmd"}` and
        optionally `term_timeout` and `tag`, or None when the record named
        nothing this could reap. None does not mean nothing was closed: a
        record that names no reachable process is still swept by tag.
    """
    try:
        record = json.loads(PID_FILE.read_text())
    except FileNotFoundError:
        return None
    except ValueError as exc:
        # Dropped rather than left where it is: nothing can be recovered from
        # it, and a record that stays on disk is re-read and warned about again
        # on every activate for the rest of the container's life.
        log.warning("emulator pid record is corrupt, dropping it: %s", exc)
        _clear_pid_record()
        return None
    except OSError as exc:
        log.warning("could not read emulator pid record: %s", exc)
        _clear_pid_record()
        return None

    pid, cmd = record.get("pid"), record.get("cmd")
    grace = _record_term_timeout(record)
    # An empty cmd would match the empty cmdline every dead pid reports, so a
    # record that cannot identify its process is not acted on: the pid may
    # belong to something else entirely by now.
    names_its_process = isinstance(pid, int) and bool(cmd) and _cmdline(pid) == cmd

    # Preferred off the record, because the process is not always there to read
    # it from: the environment is readable only while the pid lives, and a
    # shell that exited already is the case where the apps it detached most
    # need finding. A record written before the tag was stored has only the
    # live process to offer, and only while that pid is still the process the
    # record names: a pid the kernel has reissued carries whatever tag its new
    # owner was launched with, and sweeping by that would take a live session's
    # apps down.
    tag = record.get("tag")
    if not isinstance(tag, str) or not tag:
        tag = session_tag(pid) if names_its_process else None

    def sweep_by_tag(exclude: Collection[int] = ()) -> None:
        """Close whatever still carries the record's tag, if it named one.

        Every path that gives up on the recorded pid goes through here first.
        The apps a desktop session detached are in neither its process group
        nor its process tree, so the tag is the only thing still connecting
        them to the record about to be dropped, and it finds them whether the
        process that started them is still around or not.

        Args:
            exclude: Pids to leave out, for the caller that gave up on the
                recorded pid because signalling it would reach the wrong
                process group. The sweep would otherwise walk straight into
                that group by the other route.
        """
        if tag:
            kill_processes(tagged_processes(tag, exclude=exclude), grace)

    if not names_its_process:
        sweep_by_tag()
        _clear_pid_record()
        return None

    try:
        # Emulators are spawned with start_new_session, so the recorded pid is
        # its own session and group leader. A pid that is not one is not the
        # process that was recorded, whatever its cmdline says.
        if os.getpgid(pid) != pid:
            # Left out of the sweep as well: it carries the tag if the session
            # started it, and signalling it there would take down the same
            # group this branch exists to keep out of.
            sweep_by_tag(exclude={pid})
            _clear_pid_record()
            return None
        left_open = tagged_processes(tag, exclude={pid}) if tag else []
    except (ProcessLookupError, PermissionError) as exc:
        # The orphan is out of reach; what it detached is not. This is the
        # branch above one moment later - the pid died between the cmdline
        # check and this call - so it ends the same way rather than dropping
        # the record on the apps.
        log.warning("could not reach orphaned pid %d: %s", pid, exc)
        sweep_by_tag()
        _clear_pid_record()
        return None

    log.warning("reaping orphaned %s (pid %d) from an earlier broker process",
                record.get("name", "emulator"), pid)
    # The orphan rides in the same snapshot as the apps it detached, so every
    # SIGTERM goes out before any of them is waited on and the whole teardown
    # costs one grace period rather than two in a row on the activate route.
    kill_processes([(pid, cmd)] + left_open, grace)
    _clear_pid_record()
    return record


def _under_subtree(rel: str, subtree: str) -> bool:
    """Whether a member path lies inside a subtree, or is the subtree itself.

    Args:
        rel: The member path, relative to the save root and posix-separated.
        subtree: The subtree name to test against.

    Returns:
        True when `rel` equals `subtree` or starts with it followed by a slash.
    """
    return rel == subtree or rel.startswith(subtree + "/")


class Emulator:
    """Contract every launcher implements, plus the process plumbing they share.

    A subclass describes itself through the class attributes below and must
    override `launch` and `resolve_rom_file`; both raise `NotImplementedError`
    here. Everything else is an optional hook with a safe default:

    * `save_state`, `load_state`, `state_path`, `state_target` and
      `wait_for_state` are the save-state hooks. The broker only calls the
      first two when `supports_states` is on; the defaults report an empty
      slot. The frame RomM shows beside a state is not the emulator's to
      produce: the broker captures the streamed desktop on the way into a
      save and keeps it on `state_screenshot`.
    * `swap_disc` is only called when `supports_disc_swap` is on.
    * `memory_card_path` pairs with `memory_card_subtree` for emulators whose
      whole memory card travels on its own routes.
    * `clear_working_slot` and `prepare_restore` both run at every activate.
      Any subclass with real save data has to clear the previous session's
      saves in one of them and declare `clears_stale_saves`; see
      `clear_working_slot` for the contract.
    * `save_and_exit` is the exit path; the default writes no state.

    The lifecycle as the broker drives it:

    1. Activate: `clear_working_slot` drops the previous session's leftover
       saves and states, `prepare_restore` runs (always, archive or not), and
       the incoming save archive is extracted into `save_root`, scoped to
       `save_subtrees`.
    2. `launch` spawns the process through `_spawn`, which captures output to
       `log_path`, starts it in its own session and records its pid so a later
       broker process can reap it (see `reap_orphan`).
    3. Mid-session: `save_state` and `load_state` work the one slot in
       `state_slot`. The state-file routes serve `state_path` and write through
       `state_target`, with `wait_for_state` bridging a resume state that
       arrives after launch.
    4. `save_and_exit` saves when asked and stops. `stop` sends SIGTERM to the
       process group, escalates to SIGKILL after `term_timeout`, and `_forget`
       drops both the handle and the pid record once the process is confirmed
       gone.

    Attributes:
        name: Registry key and log label for the emulator.
        display_name: Human-readable name the UI shows.
        platform: The RomM platform slug the session was activated for, or None.
        language: The language the rom was activated for, or None.
        gui_language: The player's own interface language, or None.
        requires_rom: Whether a launch needs a ROM; the desktop session does not.
        save_root: Root of the emulator's writable data.
        save_subtrees: Subtrees under `save_root` that hold save data; save
            restore and dump are scoped to these.
        state_subtrees: The subset of `save_subtrees` holding savestates, for
            labelling an archive's members.
        rom_extensions: File extensions the emulator will boot, in preference
            order.
        supports_states: Whether the emulator can save and load state
            mid-session.
        supports_disc_swap: Whether the emulator can change the mounted disc
            without restarting.
        state_slot: The one slot the broker saves into.
        state_dir: Where that slot's file lives.
        log_path: Where the emulator's stdout and stderr are appended.
        term_timeout: Seconds SIGTERM gets before escalating to SIGKILL.
        kill_timeout: Seconds SIGKILL gets before the process is written off.
        clears_stale_saves: The subclass's declaration that it wipes the
            previous session's save data before a restore.
        memory_card_subtree: The save subtree holding the whole memory card, or
            None for emulators without one.
        memory_card_marker: A file the emulator needs inside the card directory
            before it treats it as a card, or None.
        boot_failed: Set by an emulator that can tell its process is alive but
            never reached a running game.
        extraction_phase: Set while a slow pre-launch extraction is running,
            else None.
    """

    name: str = "base"
    """Registry key and log label for the emulator."""
    display_name: str = "Webstation"
    """Human-readable name the UI shows."""
    platform: Optional[str] = None
    """The RomM platform slug the session was activated for, or None.

    The activate route sets it on every instance it builds, before `launch`,
    so a launcher that is one shell over many backends (RetroArch picks its
    core from it) has the slug to dispatch on. Declared here because the route
    assigns it whether or not the emulator reads it, and a reader of any
    subclass has to be able to find where it comes from.
    """
    language: Optional[str] = None
    """The language the rom was activated for, or None.

    The activate route sets it on every instance it builds, before `launch`,
    the same way it sets `platform`. Only a launcher whose games ship several
    languages in one folder has any use for it (ScummVM registers one target
    per detected language and the target is what boots), and every other
    launcher ignores it.
    """
    gui_language: Optional[str] = None
    """The player's own interface language, or None.

    Set by the activate route on every instance it builds, like `platform` and
    `language`, but describing the player rather than the rom: it is set even
    for a launch with no rom. Only a launcher with a translated interface has
    any use for it (ScummVM pins it in scummvm.ini and falls back to it when
    the rom carries no language of its own).
    """
    requires_rom: bool = True
    """Whether a launch needs a ROM; the desktop session is the one that does not."""
    save_root: Path = Path("/config")
    """Root of the emulator's writable data."""
    save_subtrees: tuple[str, ...] = ()
    """The subtrees under `save_root` that hold save data; save restore and dump are scoped to these."""
    state_subtrees: tuple[str, ...] = ()
    """The subset of `save_subtrees` holding savestates rather than the game's own save data.

    Empty for emulators with no states, and for the few whose states share a
    directory with their saves; those tell the two apart in `save_file_kind`
    instead.
    """
    rom_extensions: tuple[str, ...] = ()
    """File extensions the emulator will boot, in preference order."""
    supports_states: bool = False
    """Whether the emulator can save and load state mid-session.

    Emulators whose only persistence is the game's own save data leave this off,
    so the state routes refuse instead of silently doing nothing.
    """
    supports_disc_swap: bool = False
    """Whether the emulator can change the mounted disc without restarting.

    Off by default so the swap route refuses instead of silently doing nothing on
    an emulator that has no tray.
    """
    state_slot: int = 0
    """The one slot the broker saves into.

    RomM is the library of states: every save is pulled out of the container and
    every stored state is pushed back into this slot, so nothing here needs to
    address more than one. Requested slots resolve to it rather than being
    honoured, which is why the routes echo the effective slot back.
    """
    state_dir: Path = Path("/config")
    """Where that slot's file lives, for the state-file routes to read and write."""
    log_path: Path = Path("/config/broker-app.log")
    """Where the emulator's stdout and stderr are appended."""
    term_timeout: float = _DEFAULT_TERM_TIMEOUT
    """Seconds SIGTERM gets before escalating to SIGKILL.

    Recorded alongside the pid so `reap_orphan` gives an orphan of this
    emulator the same grace a live `stop` would.
    """
    kill_timeout: float = _DEFAULT_KILL_TIMEOUT
    """Seconds SIGKILL gets before the process is written off as unkillable.

    Raise it for an emulator whose shutdown blocks on storage that can stall
    for longer than `_DEFAULT_KILL_TIMEOUT`.
    """
    clears_stale_saves: bool = False
    """Whether this emulator wipes the previous session's save data at activate.

    Set it True in a subclass whose `clear_working_slot` (or another activate
    hook) clears the whole save tree the archive restores into. See
    `clear_working_slot` for the contract itself; the flag is what the registry
    tests assert on, so an emulator that carries real save data and leaves both
    the flag and the hook alone is caught rather than silently leaking one
    player's saves into the next player's session.
    """
    memory_card_subtree: Optional[str] = None
    """The save subtree holding the whole memory card, for emulators that have one.

    With whole-card sync on, that subtree travels on the memory-card routes
    instead of inside the save archive, so activate drops it from the restore
    and exit drops it from the dump.
    """
    memory_card_marker: Optional[str] = None
    """A file the emulator looks for inside the card directory before it treats it as a card.

    The broker lays it down empty, the way the emulator does when it creates a
    card itself; a card holding nothing but this is still an empty slot.
    """

    def __init__(self) -> None:
        """Start with no process handle, no boot failure, no extraction running, and no frame."""
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self.boot_failed: bool = False
        """Whether the process is alive but never reached a running game.

        Set by an emulator that can tell: the boot-error-dialog case. Passive
        signal only: the broker surfaces it and takes no action of its own.
        """
        self.extraction_phase: Optional[str] = None
        """Set while a slow pre-launch extraction is running, else None.

        e.g. "extracting_archive", "extracting_pkg". Passive signal only, like
        boot_failed: the broker surfaces it, RomM decides what to show.
        """
        self.state_screenshot: Optional[bytes] = None
        """The frame captured just before the working slot's state was written, as PNG bytes.

        Set by the broker on every confirmed save and replaced, or cleared,
        by the next one, so it never outlives the state it was taken with.
        """

    def _spawn(
        self,
        cmd: list[str],
        env: dict[str, str],
        stdin_pipe: bool = False,
        stdin_tty: bool = False,
    ) -> None:
        """Start the app in its own process group with output captured.

        A launch banner and then the child's stdout and stderr are appended to
        `log_path`; if the log cannot be opened the output is discarded. The pid
        is recorded through `_record_pid` once the process is up.

        Args:
            cmd: The argv to run.
            env: The environment to run it in, normally `base_launch_env()`.
            stdin_pipe: Keep the child's stdin as a pipe so emulators with a
                stdin control protocol (shadPS4 IPC) can be driven headlessly.
            stdin_tty: Give the child a pseudo-terminal for stdin, for apps
                that print an error to stdout and exit when stdin is a
                terminal but raise a blocking dialog when it is not (Xenia).

        Raises:
            ValueError: When both `stdin_pipe` and `stdin_tty` are set.
            OSError: When the process started but its pid could not be
                recorded. The process is killed first: an emulator no record
                names outlives the next broker restart with nothing able to
                find it, so the launch fails rather than leaving one behind.
        """
        if stdin_pipe and stdin_tty:
            raise ValueError("stdin_pipe and stdin_tty are mutually exclusive")
        try:
            log_fh = open(self.log_path, "ab", buffering=0)
            log_fh.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch ({' '.join(cmd)}) ===\n".encode()
            )
        except OSError as exc:
            log.warning(
                "could not open %s for %s launch output, discarding it: %s",
                self.log_path,
                self.name,
                exc,
            )
            log_fh = None
        tty_fds: tuple[int, ...] = ()
        try:
            stdin: Optional[int] = subprocess.PIPE if stdin_pipe else None
            if stdin_tty:
                tty_fds = os.openpty()
                stdin = tty_fds[1]
            self._proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=stdin,
                stdout=log_fh if log_fh else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
                # The child keeps the pty master open itself. Closing the last
                # copy of a master hangs its terminal up, and isatty() on a
                # hung-up terminal is false, so the broker dropping its copy
                # below would otherwise undo the terminal it just handed over.
                pass_fds=tty_fds[:1],
                start_new_session=True,
            )
        finally:
            if log_fh:
                log_fh.close()
            for fd in tty_fds:
                os.close(fd)
        try:
            _record_pid(
                self.name, self._proc.pid, cmd, self.term_timeout, env.get(SESSION_TAG_ENV)
            )
        except OSError:
            # Emulator.stop rather than self.stop: a subclass stop drives a
            # control channel (IPC, a hotkey) the process has not come up far
            # enough to answer yet, and this only needs the signal path.
            Emulator.stop(self)
            raise

    def alive(self) -> bool:
        """Whether a spawned process exists and has not exited."""
        return self._proc is not None and self._proc.poll() is None

    def _forget(self) -> None:
        """Drop the handle on the emulator and the record of it on disk.

        Every path that ends with the process gone has to go through here.
        A graceful exit that only clears `_proc` leaves a record pointing at a
        pid nobody owns, and the next broker start would hunt it.
        """
        self._proc = None
        _clear_pid_record()

    def stop(self) -> None:
        """Terminate the running emulator, if any, and forget it.

        The process group gets SIGTERM, escalating to SIGKILL once
        `term_timeout` passes, and SIGKILL gets `kill_timeout` to land. A
        process that is already gone is a no-op.

        The handle and the pid record are only dropped once the process is
        confirmed gone. An emulator that outlived SIGKILL, or that refused the
        signal outright, keeps both: dropping the record while it still runs
        would leave it with nothing able to find it, which is the orphan
        `PID_FILE` exists to prevent, and every subclass `launch` opens with a
        `stop` that would then be its next chance to try again.

        This reaches the process and its group, which is every emulator here.
        An app that starts other apps and detaches them needs more than a
        signal to one group, and overrides this (see `Desktop.stop`).
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._forget()
            return
        log.info("stopping %s (pid %d)", self.name, proc.pid)
        gone = False
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=self.term_timeout)
                gone = True
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=self.kill_timeout)
                    gone = True
                except subprocess.TimeoutExpired:
                    log.error(
                        "%s (pid %d) did not exit after SIGKILL, keeping its pid record",
                        self.name,
                        proc.pid,
                    )
        except ProcessLookupError:
            log.debug("%s (pid %d) was already gone", self.name, proc.pid)
            gone = True
        except PermissionError as exc:
            log.error(
                "not allowed to signal %s (pid %d), keeping its pid record: %s",
                self.name,
                proc.pid,
                exc,
            )
        if gone:
            self._forget()

    def prepare_restore(self) -> None:
        """Hook run at activate, after `clear_working_slot` and before any extract.

        Called on every activate, whether or not there is an archive to
        restore: a session that starts with no archive is exactly the one where
        a stale save left in place would be picked up as the player's own.

        Default: nothing. Override to clear anything that would block or
        outrank the restore: a process holding a save file open, or an existing
        file the newer-file guard would wrongly keep over the archived one.
        """

    def launch(self, rom_path: Optional[Path], resume_slot: Optional[int]) -> None:
        """Start the emulator on `rom_path`, optionally resuming a state.

        Args:
            rom_path: The file to boot, as returned by `resolve_rom_file`, or
                None for an emulator that does not require a ROM.
            resume_slot: The slot to load once the game is up, or None for a
                fresh start.

        Raises:
            NotImplementedError: Always; every subclass overrides this.
        """
        raise NotImplementedError

    def save_state(self, slot: int) -> bool:
        """Save the running game to `slot`.

        Only called when `supports_states`.

        Args:
            slot: The slot RomM asked for; implementations may resolve it to
                `state_slot`.

        Returns:
            True once the state is confirmed written.

        Raises:
            NotImplementedError: When the emulator does not support states.
        """
        raise NotImplementedError

    def load_state(self, slot: int) -> bool:
        """Load `slot` into the running game.

        Only called when `supports_states`.

        Args:
            slot: The slot RomM asked for; implementations may resolve it to
                `state_slot`.

        Returns:
            True once the load was delivered to the emulator.

        Raises:
            NotImplementedError: When the emulator does not support states.
        """
        raise NotImplementedError

    def state_path(self) -> Optional[Path]:
        """The file the working slot holds right now, or None if it is empty.

        This is what the state-file GET serves, so it has to be the file the
        emulator just wrote, not the newest state in the directory: another
        slot or another game's state would otherwise be filed in RomM as this
        save.

        Returns:
            The state file's path, or None. The default reports an empty slot.
        """
        return None

    def note_state_handout(self) -> None:
        """Record that the state-file GET route is about to read the file `state_path` returned.

        That read moves the file's access time, which some implementations
        also watch to confirm a load actually happened. Called before the
        route reads the file's bytes, so the window it opens covers that read
        instead of starting after the access time has already moved; callers
        of `load_state` can then tell their own load's read from this one.
        The default does nothing, for implementations that confirm loads some
        other way.
        """
        return None

    def lock_for_state_write(self) -> bool:
        """Take whatever lock a pushed state file should hold before it overwrites the working slot.

        A push lands as a raw file replace with no confirmation step of its
        own, so nothing else stops it landing in the middle of another
        implementation's own confirmation window for that same file (a resume
        retry, an in-flight load) and being mistaken for whatever that other
        read or write was watching for. The default has no such window to
        protect and always succeeds.

        Returns:
            True once locked (or when there is nothing to lock); False when
            an implementation's lock could not be taken in time, in which case
            the caller must not write the file and must not call
            `unlock_state_write`.
        """
        return True

    def unlock_state_write(self) -> None:
        """Release whatever `lock_for_state_write` took. No-op by default.

        Only called after a `lock_for_state_write` call that returned True.
        """
        return None

    def _clear_subtree(
        self, subtree: str, keep: Optional[Callable[[Path], bool]] = None
    ) -> None:
        """Empty one of `save_subtrees` without removing the directory itself.

        The directory stays because an emulator that finds its save directory
        missing at launch writes its saves somewhere else entirely, or refuses
        to boot at all.

        Args:
            subtree: A path relative to `save_root`, as `save_subtrees` names it.
            keep: Called with each top-level entry; returning True leaves that
                entry in place. The default clears everything.
        """
        root = self.save_root
        target = root / subtree
        try:
            resolved = target.resolve()
            # A relative escape in a subtree name would otherwise point this
            # delete outside the save tree.
            if not resolved.is_relative_to(root.resolve()):
                log.error(
                    "%s: refusing to clear %s, it escapes the save root %s",
                    self.name,
                    target,
                    root,
                )
                return
            if not target.is_dir():
                return
            entries = list(target.iterdir())
        except OSError as exc:
            log.warning(
                "%s: could not scan %s for stale save data, an earlier session's "
                "saves may survive into this one: %s",
                self.name,
                target,
                exc,
            )
            return
        for entry in entries:
            if keep is not None and keep(entry):
                continue
            try:
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
            except OSError as exc:
                log.warning(
                    "%s: could not clear stale save data %s: %s", self.name, entry, exc
                )
                continue
            log.info("%s: cleared stale save data %s", self.name, entry)

    def _clear_save_subtrees(
        self,
        excluded: tuple[str, ...] = (),
        keep: Optional[Callable[[Path], bool]] = None,
    ) -> None:
        """Empty every save subtree this session is responsible for.

        Args:
            excluded: Subtrees the whole-card routes carry, which this session
                must leave alone; the card is laid down before activate runs,
                so clearing one here would delete what RomM just synced.
            keep: Passed through to `_clear_subtree` for each subtree.
        """
        for subtree in self.save_subtrees:
            if subtree in excluded:
                continue
            self._clear_subtree(subtree, keep)

    def clear_working_slot(self, excluded: tuple[str, ...] = ()) -> None:
        """Drop what an earlier session left in the save tree, before the restore.

        Every subclass that carries real save data MUST empty the save tree
        here (or in `prepare_restore`) and declare `clears_stale_saves`. This
        is not an optimisation and not optional. The restore only overwrites
        the members the incoming archive happens to carry, so anything the
        previous session wrote and this one's archive does not name survives
        untouched: on a pooled or shared container that is one player's saves
        left readable, and mixed into the dump, in the next player's session. A
        clear scoped to a state slot or to one title is not enough on its own
        unless nothing else under `save_subtrees` can hold another player's
        data.

        Deleting more than the container's own leftovers is the failure in the
        other direction, so the clear is scoped to `save_subtrees` and runs
        before the archive is extracted, never after. `_clear_save_subtrees`
        is the usual implementation; an emulator with app data, DLC or
        quarantined files sharing a subtree passes a `keep` predicate.

        The default only reports the gap: the base class cannot know which
        paths are safe to delete, so an emulator with save data that reaches
        this warns rather than silently starting a session on the last
        player's files.

        Args:
            excluded: Save subtrees the whole-card routes carry for this
                session, which the clear must leave alone.
        """
        if self.save_subtrees and not self.clears_stale_saves:
            log.warning(
                "%s holds save data in %s but clears none of it at activate: "
                "an earlier session's saves may survive into this one",
                self.name,
                ", ".join(self.save_subtrees),
            )

    def memory_card_path(self, platform: Optional[str] = None) -> Optional[Path]:
        """The directory holding the card the memory-card routes sync, or None.

        The broker names the card rather than reading the name out of the
        emulator's own config, because RomM lays a card down before the first
        launch has written that config. `platform` is the ROM's platform slug,
        for an emulator whose card exists on only one of several platforms it
        serves (GameCube vs Wii on Dolphin); most emulators ignore it.

        Returns:
            The card directory, or None for emulators without a memory card.
        """
        return None

    def archive_core(self) -> Optional[str]:
        """The core or backend actually running the game, or None.

        Only meaningful for a launcher that is one shell over many cores; it
        goes in the archive manifest so the parent can tell a RetroArch PSP
        archive from a standalone PPSSPP one.

        Returns:
            The core name, or None for emulators that are their own backend.
        """
        return None

    def save_file_kind(self, rel: str) -> str:
        """What an archive member holds, for the manifest the parent reads.

        Every emulator lays its save directories out differently, so this is
        what lets the parent sort an archive without a table of those layouts.
        The default classifies by subtree; emulators whose states and saves
        share a directory override it.

        Args:
            rel: The member path, relative to `save_root` and posix-separated.

        Returns:
            One of `state`, `state_screenshot`, `memcard` or `save`.
        """
        if self.memory_card_subtree and _under_subtree(rel, self.memory_card_subtree):
            return "memcard"
        if any(_under_subtree(rel, sub) for sub in self.state_subtrees):
            # The frame captured with a state is written beside it, and the
            # parent shows it rather than restoring it.
            return "state_screenshot" if rel.lower().endswith((".png", ".jpg")) else "state"
        return "save"

    def always_restore(self, rel: str) -> bool:
        """Whether an archive member outranks whatever sits on disk at activate.

        The restore skips a member the disk already holds a newer copy of, so
        it can never roll back progress made since the archive was taken. That
        reasoning only holds for files whose mtime records this player's own
        saving. An emulator with a file the container writes on behalf of
        whoever is using it (a signed-in profile package, an account store)
        overrides this: the freshest copy of one of those is the last player's,
        and passing over the incoming one silently runs the session under their
        identity.

        Args:
            rel: The member path, relative to `save_root` and posix-separated.

        Returns:
            True to restore the member unconditionally. The default exempts
            nothing, so every member stays under the guard.
        """
        return False

    import_identity: Optional[imports.SessionIdentity] = None
    """The game id this session runs as, set by activate's preflight."""

    def import_spec(self) -> imports.ImportSpec:
        """What this emulator accepts as a declared import, on `self.platform`.

        Returns:
            The spec. The default accepts nothing, so every import member is
            refused with `kind_not_accepted` before `place_import` is called.
        """
        return imports.ImportSpec()

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Place one member that passed hygiene and the kind gate.

        Must be pure: nothing is written, nothing is deleted, and the only
        reads allowed are `member.head()` and the rom through `ctx`.

        Args:
            member: The member.
            spec: This emulator's spec, as `import_spec` answered it.
            ctx: The launch context.

        Returns:
            Where the member lands, or why it cannot. The default refuses.
        """
        return imports.ImportRefusal("kind_not_accepted", member.name, "no imports")

    def validate_import_plan(
        self, plan: list[imports.Placement], ctx: imports.ImportCtx
    ) -> list[imports.ImportRefusal]:
        """Check the placements together, after the shared checks.

        Args:
            plan: Every placement.
            ctx: The launch context.

        Returns:
            Any refusals. The default has none.
        """
        return []

    def identity_source(self) -> Optional[imports.IdentitySource]:
        """Where this emulator's session identity comes from.

        Returns:
            The source, or None when the emulator checks no identity.
        """
        return None

    @property
    def restore_subtrees(self) -> tuple[str, ...]:
        """The subtrees an archive may restore into, read before the working slot is cleared.

        Returns:
            `save_subtrees` by default. An emulator whose `save_subtrees`
            depends on state the clear sets overrides this.
        """
        return self.save_subtrees

    def state_target(self, filename: str) -> Optional[Path]:
        """Where a pushed state called `filename` belongs.

        Validating the name against the emulator's own convention is what keeps
        a caller from dropping arbitrary files into the save tree. The slot in
        it is not part of that test: RomM holds the library, so a stored state
        carries whatever slot it was captured in and lands in this broker's own
        working slot regardless.

        Args:
            filename: The name the pushed state was stored under.

        Returns:
            The path to write it to, or None if the name is not one this
            emulator would write for the loaded game. The default accepts
            nothing.
        """
        return None

    def wait_for_state(self, deadline: float, poll: float = 0.5) -> bool:
        """Block until the working slot holds a state file, or `deadline` passes.

        A resume state can turn up after launch: the state-file routes only
        answer while a session is up, so RomM pushes its pick once activate has
        returned and the game is already booting. Waiting for it here is what
        keeps a deferred resume load from firing on a slot that is still empty
        and reporting a fresh start.

        Args:
            deadline: A `time.monotonic()` value to give up at.
            poll: Seconds between checks of `state_path`.

        Returns:
            True if the slot holds a state file by the time this returns.
        """
        while time.monotonic() < deadline:
            if self.state_path() is not None:
                return True
            time.sleep(poll)
        return self.state_path() is not None

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Save state (best effort) and stop.

        Default: nothing to save. A `slot` of None is an exit that writes no
        state. The game's own save data is still flushed and shipped: not
        writing a state is the whole of "exit without saving", and discarding
        an in-game save the player made at a save point would be losing real
        progress.

        Args:
            slot: The slot to save into before stopping, or None to skip the
                state save.

        Returns:
            A dict with `{"state_saved", "state_slot", "state_file",
            "sram_flushed"}`: whether a state was written, the effective
            slot, the written file's `{"path", "size", "mtime"}`, and whether
            the game's own save data was confirmed flushed before exit
            (None where an implementation has no such confirmation to give).
            The default reports all four as None.
        """
        self.stop()
        return {
            "state_saved": None,
            "state_slot": None,
            "state_file": None,
            "sram_flushed": None,
        }

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """File the emulator should boot for `path` (folder or file).

        Args:
            path: The ROM as RomM delivered it, either a single file or a folder
                holding the game's files.

        Returns:
            The file to hand to `launch`, or None if nothing bootable is there.

        Raises:
            NotImplementedError: Always; every subclass overrides this.
        """
        raise NotImplementedError

    def swap_disc(self, path: Path) -> bool:
        """Mount `path` in place of the running disc.

        Only called when `supports_disc_swap`.

        Args:
            path: The disc image to mount.

        Returns:
            True once the new disc is in the tray.

        Raises:
            NotImplementedError: When the emulator has no tray.
        """
        raise NotImplementedError
