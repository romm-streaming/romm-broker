"""Emulator interface and shared launch plumbing.

Defines the `Emulator` base class every launcher in this package subclasses, the
environment apps are launched into, and the on-disk pid record that lets a broker
process kill an emulator it never spawned.
"""

import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

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


def base_launch_env() -> dict[str, str]:
    """Build the environment apps are launched into.

    This is the broker's own environment, pointed at the running labwc session's
    displays (`BROKER_WAYLAND_DISPLAY` and `BROKER_DISPLAY`, defaulting to
    `wayland-0` and `:0`), with secret-shaped variables stripped out (see
    `_SENSITIVE_ENV_VARS`).

    Returns:
        A copy of the broker's environment with the display variables set and
        the emulator binary directories appended to `PATH`.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _SENSITIVE_ENV_VARS and not k.endswith(_SENSITIVE_ENV_SUFFIXES)
    }
    env["WAYLAND_DISPLAY"] = os.environ.get("BROKER_WAYLAND_DISPLAY", "wayland-0")
    env["DISPLAY"] = os.environ.get("BROKER_DISPLAY", ":0")
    # s6 services get a minimal PATH; emulator binaries live in /usr/games.
    path = env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    for extra in ("/usr/local/bin", "/usr/bin", "/usr/games", "/usr/local/games"):
        if extra not in path.split(":"):
            path = f"{path}:{extra}"
    env["PATH"] = path
    return env


def _record_pid(
    name: str, pid: int, cmd: list[str], term_timeout: Optional[float] = None
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

    Raises:
        OSError: When the record cannot be written. A session whose emulator
            has no record survives a broker restart unreapable, so the caller
            has to know rather than find out at the next launch.
    """
    tmp = PID_FILE.with_suffix(".tmp")
    record: dict[str, Any] = {"name": name, "pid": pid, "cmd": cmd}
    if term_timeout is not None:
        record["term_timeout"] = term_timeout
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
        pass
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
    except OSError:
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

    Returns:
        The record that was acted on, a dict with `{"name", "pid", "cmd"}` and
        optionally `term_timeout`, or None when there was no usable record.
    """
    try:
        record = json.loads(PID_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None
    except OSError as exc:
        log.warning("could not read emulator pid record: %s", exc)
        return None

    pid, cmd = record.get("pid"), record.get("cmd")
    # An empty cmd would match the empty cmdline every dead pid reports, so a
    # record that cannot identify its process is thrown away rather than acted
    # on: the pid may belong to something else entirely by now.
    if not isinstance(pid, int) or not cmd or _cmdline(pid) != cmd:
        _clear_pid_record()
        return None

    try:
        # Emulators are spawned with start_new_session, so the recorded pid is
        # its own session and group leader. A pid that is not one is not the
        # process that was recorded, whatever its cmdline says.
        pgid = os.getpgid(pid)
        if pgid != pid or _cmdline(pid) != cmd:
            _clear_pid_record()
            return None

        log.warning("reaping orphaned %s (pid %d) from an earlier broker process",
                    record.get("name", "emulator"), pid)
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + _record_term_timeout(record)
        while time.monotonic() < deadline and _cmdline(pid) == cmd:
            time.sleep(0.2)
        if _cmdline(pid) == cmd:
            os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as exc:
        log.warning("could not kill orphaned pid %d: %s", pid, exc)
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

    * `save_state`, `load_state`, `state_path`, `state_screenshot_path`,
      `state_target`, `clear_working_slot` and `wait_for_state` are the
      save-state hooks. The broker only calls the first two when
      `supports_states` is on; the defaults report an empty slot.
    * `swap_disc` is only called when `supports_disc_swap` is on.
    * `memory_card_path` pairs with `memory_card_subtree` for emulators whose
      whole memory card travels on its own routes.
    * `prepare_restore` runs before a save archive is extracted.
    * `save_and_exit` is the exit path; the default writes no state.

    The lifecycle as the broker drives it:

    1. Activate: `clear_working_slot` drops the previous session's leftover
       state, `prepare_restore` runs, and the incoming save archive is
       extracted into `save_root`, scoped to `save_subtrees`.
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
        """Start with no process handle, no boot failure, and no extraction running."""
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

    def _spawn(self, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False) -> None:
        """Start the app in its own process group with output captured.

        A launch banner and then the child's stdout and stderr are appended to
        `log_path`; if the log cannot be opened the output is discarded. The pid
        is recorded through `_record_pid` once the process is up.

        Args:
            cmd: The argv to run.
            env: The environment to run it in, normally `base_launch_env()`.
            stdin_pipe: Keep the child's stdin as a pipe so emulators with a
                stdin control protocol (shadPS4 IPC) can be driven headlessly.

        Raises:
            OSError: When the process started but its pid could not be
                recorded. The process is killed first: an emulator no record
                names outlives the next broker restart with nothing able to
                find it, so the launch fails rather than leaving one behind.
        """
        try:
            log_fh = open(self.log_path, "ab", buffering=0)
            log_fh.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch ({' '.join(cmd)}) ===\n".encode()
            )
        except OSError:
            log_fh = None
        try:
            self._proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.PIPE if stdin_pipe else None,
                stdout=log_fh if log_fh else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            if log_fh:
                log_fh.close()
        try:
            _record_pid(self.name, self._proc.pid, cmd, self.term_timeout)
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
        `term_timeout` passes. A process that is already gone is a no-op.

        The handle and the pid record are only dropped once the process is
        confirmed gone. An emulator that outlived SIGKILL, or that refused the
        signal outright, keeps both: dropping the record while it still runs
        would leave it with nothing able to find it, which is the orphan
        `PID_FILE` exists to prevent, and every subclass `launch` opens with a
        `stop` that would then be its next chance to try again.
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
                    proc.wait(timeout=10)
                    gone = True
                except subprocess.TimeoutExpired:
                    log.error(
                        "%s (pid %d) did not exit after SIGKILL, keeping its pid record",
                        self.name,
                        proc.pid,
                    )
        except ProcessLookupError:
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
        """Hook run before a save archive is extracted into `save_root`.

        Default: nothing. Override to clear anything that would block the
        restore: a process holding a save file open, or an existing file the
        newer-file guard would wrongly keep over the archived one.
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

    def state_screenshot_path(self) -> Optional[Path]:
        """The frame captured alongside the working slot's state, or None.

        Only for emulators that write the thumbnail as a separate file. The
        ones that embed it in the state itself return None and let RomM pull it
        out of the state it already fetched.

        Returns:
            The screenshot's path, or None. The default reports none.
        """
        return None

    def clear_working_slot(self) -> None:
        """Drop whatever the working slot holds from an earlier session.

        Called at activate, before the incoming save archive is restored, so
        only the container's own leftovers go. Emulators that name a state
        after the loaded content can tell a stale one apart on sight and leave
        this alone; the override exists for the ones that cannot.
        """

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
            A dict with `{"state_saved", "state_slot", "state_file"}`:
            whether a state was written, the effective slot, and the written
            file's `{"path", "size", "mtime"}`. The default reports all three
            as None.
        """
        self.stop()
        return {"state_saved": None, "state_slot": None, "state_file": None}

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
