"""Flycast (Sega Dreamcast) launcher.

Handles ROM resolution, save state via a graceful close request, and boot-time resume.

Flycast has no control socket and, unlike DuckStation, installs no
SIGTERM/SIGINT handler at all: a bare SIGTERM is a hard kill. Its only
graceful-shutdown path is SDL_QUIT (a window close request), which
`dc_exit()` turns into `unloadGame()`, and that is also the only place
`Dreamcast.AutoSaveState` gets written.

Getting a real SDL_QUIT out of it takes more than an xdotool window-close
command: `windowclose` is a raw XDestroyWindow, which never reaches SDL's own
event loop, and `windowquit`'s `_NET_CLOSE_WINDOW` is sent to the root window
for a window manager to pick up, but this container's compositor (labwc, on
wlroots) has no handler for that message at all, only for its own internal
Close action. That action is what actually sends a real WM_DELETE_WINDOW to
the window (wlroots' `wlr_xwayland_surface_close()`), and the only way to
reach it externally is the same keybind a player would use: Alt+F4, which
labwc's default config binds to Close. `stop()` therefore activates the
window and sends that keypress, exactly the hotkey-injection shape
dolphin.py/ppsspp.py already use for their own hotkeys, before falling back
to the base SIGTERM->SIGKILL, the same graceful-trigger-then-escalate shape
shadPS4 uses for its IPC STOP command.

Everything the broker needs (auto-load, auto-save, slot, fullscreen) rides
`-config section:key=value` transient overrides on the command line rather
than a patched ini, so nothing here touches emu.cfg. The section for the
typed Dreamcast.* options is "config" (that's the whole key, dot included;
see core/cfg/option.cpp), not "Dreamcast" as the key's own name might
suggest.

Save state is a single deterministic file, `<rom-basename>.state`, in the
flat XDG data dir ($XDG_DATA_HOME/flycast or ~/.local/share/flycast, same
resolution flycast itself does). With slot pinned to 0 the broker can compute
that path itself instead of guessing at the newest or most-recently-changed
file in the directory.

That name is flycast's, not the broker's to pick, and it carries the rom's
basename alone: two discs sharing a basename in different folders (a region
or revision pair) write the same state file. So a confirmed write also
records a `<rom-basename>.state.rom` marker naming the rom it came from, the
same ownership trick duckstation.py uses on its flat savestates directory,
and a resume is refused when the marker names a different rom. A state with
no marker still loads, since an unidentified state is all the broker had
before.

VMU saves are loose files in the same flat data dir (vmu_save_A1.bin etc.,
shared across games by default), beside the arcade nvmem and eeprom files.
The whole directory ships as one save subtree rather than named files, since
nothing here otherwise separates VMU saves from savestates from a first-run
onboarding artifact. clear_working_slot() sweeps all of it at activate: none
of it is namespaced by title or by player, and it runs before the rom is even
known (activate resolves the rom, then clears the slot, then launches), so
anything left behind is handed to the next player as their own and swept into
their exit archive. The BIOS pair, emu.cfg and flycast's own subdirectories
stay; they belong to the container, not to a session.
"""

import logging
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

FLYCAST_BIN = os.environ.get("FLYCAST_BIN", "/opt/flycast/AppRun")


def _default_data_dir() -> str:
    """Return Flycast's own data dir: $XDG_DATA_HOME/flycast, or ~/.local/share/flycast if unset."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "flycast")
    return os.path.join(os.environ.get("HOME", "/config"), ".local/share/flycast")


DATA_DIR = Path(os.environ.get("FLYCAST_DATA_DIR", _default_data_dir()))
FLYCAST_LOG_PATH = Path(os.environ.get("FLYCAST_LOG_PATH", "/config/flycast.log"))

# Discs flycast boots, best first, so a folder holding several candidates
# picks the compressed image over the raw one beside it. .elf is homebrew;
# flycast's own CLI parser flips bios.UseReios on for it automatically.
ROM_EXTENSIONS = (".chd", ".gdi", ".cdi", ".cue", ".elf")
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)

_XDOTOOL = os.environ.get("XDOTOOL_BIN", "xdotool")
# SDL_CreateWindow("Flycast", ...) at boot; SDL_SetWindowTitle appends
# " - <game title>" once content loads. Matched by title rather than WM
# class since the AppImage's real argv[0] (and so its default X11 class)
# isn't something the broker can pin down from source alone.
_WINDOW_TITLE_RE = "^Flycast"


def _disc_number(rel: Path) -> int:
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _pick_rom_file(candidates: list[Path], base: Path) -> Optional[Path]:
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
            continue
        try:
            if not p.is_file():
                continue
            real = p.resolve()
            rel = p.relative_to(base)
        except (OSError, ValueError):
            continue
        if not real.is_relative_to(ROM_ROOT):
            continue
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


UNTRUSTED_SUFFIX = ".untrusted"
"""Suffix a resume state is renamed with when a force-killed exit leaves it unusable.

The file is only suspected of being torn, never known to be, so it is set
aside under this name rather than deleted. It stops matching `*.state`, so
neither `clear_working_slot` nor a resume can pick it up, and it rides the
save archive as ordinary save data (see `save_file_kind`), which is what
makes it recoverable at all: DATA_DIR itself does not outlive the container.
"""

OWNER_SUFFIX = ".rom"
"""Suffix of the marker file recording which rom a resume state belongs to.

The marker sits beside the state it names, holds the booted rom's resolved
path, and rides the save archive with it. It does not match `*.state`, so it
is never mistaken for a state.
"""

CONTAINER_FILES = frozenset({"dc_boot.bin", "dc_flash.bin", "emu.cfg"})
"""DATA_DIR entries that belong to the container rather than to a session.

The Dreamcast BIOS pair is uploaded once during setup and every session needs
it, and emu.cfg holds the graphics, audio and controller preferences the
desktop session set. Everything else in the flat data dir is one player's
progress, so `clear_working_slot` sweeps it and leaves these three.
"""


def _is_quarantined(name: str) -> bool:
    """Whether a DATA_DIR entry is a set-aside resume state or that state's marker."""
    return name.endswith(UNTRUSTED_SUFFIX) or name.endswith(UNTRUSTED_SUFFIX + OWNER_SUFFIX)


STATE_WAIT = float(os.environ.get("FLYCAST_STATE_WAIT", "30.0"))
"""Seconds a shutdown save gets to land on disk (env `FLYCAST_STATE_WAIT`, default 30)."""
STATE_STABLE = float(os.environ.get("FLYCAST_STATE_STABLE", "1.5"))
"""Seconds size and mtime must both hold still before a state counts as written.

From env `FLYCAST_STATE_STABLE`, default 1.5. Long enough that a stalled
write does not read as a finished one on a loaded host.
"""


def _state_path_for(rom_path: Path) -> Path:
    return DATA_DIR / f"{rom_path.stem}.state"


def _rom_identity(rom: Path) -> str:
    """The rom identity an owner marker records.

    Args:
        rom: The disc image or file the session booted.

    Returns:
        The resolved absolute path as text, so the same disc matches across
        sessions while two discs sharing a basename stay distinct.
    """
    try:
        return str(rom.resolve())
    except OSError as exc:
        log.warning("could not resolve %s for its state marker: %s", rom, exc)
        return str(rom)


def _owner_marker(state: Path) -> Path:
    """The owner marker path belonging to a resume state."""
    return state.with_name(state.name + OWNER_SUFFIX)


def _state_owner(state: Path) -> Optional[str]:
    """The rom identity recorded for a resume state.

    Args:
        state: The resume state to look up.

    Returns:
        The identity in its marker, or None when the state carries no
        readable marker.
    """
    marker = _owner_marker(state)
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("could not read the state marker %s: %s", marker, exc)
        return None
    return recorded or None


def _write_owner_marker(state: Path, rom: Optional[Path]) -> None:
    """Record which rom wrote a resume state, so a later resume can identify it.

    A failure is logged and stepped over: an unmarked state still ships in
    the archive and still resumes, so the cost is a resume that stays as
    ambiguous as it was before markers existed, not lost progress.

    Args:
        state: The resume state this exit confirmed.
        rom: The rom the session booted, or None when no launch on this
            object recorded one.
    """
    if rom is None:
        log.warning("no rom recorded for this session, leaving %s unmarked", state.name)
        return
    marker = _owner_marker(state)
    try:
        marker.write_text(_rom_identity(rom) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not mark %s as belonging to %s: %s", state.name, rom, exc)
    else:
        log.info("marked resume state %s as belonging to %s", state.name, rom)


def _state_belongs_to(state: Path, rom: Path) -> bool:
    """Whether a resume state may be loaded for the rom about to boot.

    Flycast names the state after the rom's basename alone, so a region or
    revision pair in two folders writes the same filename. The marker the
    last confirmed exit left is what tells them apart, and it is matched
    exactly. A state with no marker is still taken, which is what carries
    archives written before markers existed.

    Args:
        state: The resume state found for this rom's basename.
        rom: The rom about to boot.

    Returns:
        True when the state is unmarked or marked for this exact rom.
    """
    owner = _state_owner(state)
    if owner is None or owner == _rom_identity(rom):
        return True
    log.warning(
        "resume state %s is marked for %s, not %s, booting clean", state.name, owner, rom
    )
    return False


def _state_is_loadable(path: Path) -> bool:
    """Whether a resume state is worth pointing Dreamcast.AutoLoadState at.

    A zero-byte state is what an interrupted write or a truncated restore
    leaves behind, and flycast auto-loading one gives the player a dead boot
    instead of the fresh start it would otherwise have had.

    Args:
        path: The resume state to check.

    Returns:
        True when the file is a regular file holding any data at all.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not check the resume state at %s: %s", path, exc)
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0


def _wait_for_state_write(
    path: Path, before: Optional[tuple[int, float]], deadline: float
) -> Optional[tuple[int, float]]:
    """Poll a resume state until this exit's write settles, or the deadline passes.

    The process being gone is not proof the state is whole: flycast writes it
    from `unloadGame()` on its way out, and the broker zips DATA_DIR into the
    upload archive the moment `save_and_exit` returns, so a single stat taken
    the instant the pid dies can measure a half-flushed file and ship it to
    RomM as the player's progress. A write therefore only counts once the file
    differs from `before`, is non-empty, and has held both size and mtime still
    for `STATE_STABLE`. Size alone over one sample is not enough, and neither
    is a stalled writer's steady size over a short window.

    Args:
        path: The resume state this exit should have written.
        before: `(size, mtime)` the state carried before the exit, or None when
            there was no state then.
        deadline: `time.monotonic` value to give up at.

    Returns:
        The settled `(size, mtime)`, or None when nothing was written, nothing
        changed, or the write never settled in time.
    """
    POLL_SECS = 0.1
    last: Optional[tuple[int, float]] = None
    stable_since = 0.0
    reason = ""
    while True:
        try:
            st = path.stat()
        except OSError:
            cur = None
        else:
            cur = (st.st_size, st.st_mtime)
        if cur is None:
            reason = "no resume state written during shutdown"
        elif cur == before:
            reason = "resume state unchanged, exit may not have saved"
        else:
            reason = "resume state never finished writing before the deadline"
            if cur != last:
                last = cur
                stable_since = time.monotonic()
            elif cur[0] > 0 and time.monotonic() - stable_since >= STATE_STABLE:
                log.info("resume state write complete: %s (%d bytes)", path.name, cur[0])
                return cur
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECS)
    if last is None:
        log.warning("%s: %s", reason, path)
    else:
        log.warning("%s: %s (%d bytes)", reason, path, last[0])
    return None


class Flycast(Emulator):
    """Exit-only Flycast (Sega Dreamcast) launcher.

    Save state is triggered by a graceful window-close request (Alt+F4)
    rather than a control socket, since Flycast has no other clean shutdown
    path; see the module docstring for the full hardware/protocol rationale.
    """

    name = "flycast"
    display_name = "Flycast"
    save_root = DATA_DIR.parent
    # DATA_DIR itself: VMU saves and the savestate both sit loose at its
    # root, with nothing else here to split them into named subtrees.
    save_subtrees = (DATA_DIR.name,)
    rom_extensions = ROM_EXTENSIONS
    log_path = FLYCAST_LOG_PATH
    # The window-close request goes through the SDL event loop into
    # dc_exit(); give it room before escalating to a SIGTERM that flycast
    # has no handler for and which would skip AutoSaveState entirely.
    term_timeout = float(os.environ.get("FLYCAST_STOP_WAIT", "20"))

    def __init__(self) -> None:
        """Initialize the emulator with no ROM loaded yet."""
        super().__init__()
        self._rom_path: Optional[Path] = None

    def save_file_kind(self, rel: str) -> str:
        """Classify an archive member for the manifest.

        Flycast keeps the savestate loose beside the VMU images, so the
        subtree-based default cannot split them; the `.state` suffix can.

        Args:
            rel: The member path, relative to `save_root` and posix-separated.

        Returns:
            `state` for the savestate, `save` for everything else.
        """
        return "state" if rel.lower().endswith(".state") else "save"

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a ROM path to a single playable disc image or file.

        A pattern that cannot be walked (an unreadable subdirectory, a broken
        mount) costs only what that pattern would have contributed: the
        candidates already found still rank, since reporting a title
        unbootable over one bad directory is worse than booting the best of
        what could be read.

        Args:
            path: A direct file path, or a folder searched up to one level
                deep for the best candidate, ranked by disc number,
                extension preference, and path depth.

        Returns:
            The resolved file path, or None if no suitable candidate was
            found.
        """
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError as exc:
                log.warning("rom search %r under %s failed: %s", pattern, path, exc)
        return _pick_rom_file(candidates, path)

    def _xdotool(self, *args: str) -> Optional[str]:
        """Run xdotool, returning its stdout, or None if it failed."""
        try:
            result = subprocess.run(
                [_XDOTOOL, *args],
                env=base_launch_env(),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("xdotool %s failed: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            log.warning("xdotool %s: %s", " ".join(args), result.stderr.strip())
            return None
        return result.stdout

    def _window(self) -> Optional[str]:
        """The window id belonging to this launch's own process, or None.

        Matched by title then confirmed by pid rather than trusting the first
        title hit, since Alt+F4 acts on whatever id comes back and a stray
        match would close the wrong thing instead of just failing a hotkey.
        """
        proc = self._proc
        if proc is None:
            return None
        out = self._xdotool("search", "--name", _WINDOW_TITLE_RE)
        if out is None:
            return None
        for win_id in out.split():
            pid = self._xdotool("getwindowpid", win_id)
            if pid is not None and pid.strip() == str(proc.pid):
                return win_id
        log.warning("no flycast window found for pid %s", proc.pid)
        return None

    def _close_request(self, win_id: str) -> bool:
        """Activate the emulator's own window and send it Alt+F4.

        The keystroke goes through XTEST (no `key --window`) on purpose:
        Alt+F4 is a labwc keybind, and the compositor only acts on input that
        reaches the seat, so a synthetic event delivered straight to the
        client window would never fire its Close action and flycast would
        never see SDL_QUIT. XTEST lands on whatever holds focus, so focus is
        read back and the request is abandoned when a different window owns
        it, rather than closing something the player did not ask to close.

        Args:
            win_id: Window id already confirmed to belong to this launch.

        Returns:
            True when the keystroke was sent, False when the window could not
            be activated or a different window holds focus.
        """
        if self._xdotool("windowactivate", "--sync", win_id) is None:
            return False
        active = (self._xdotool("getactivewindow") or "").strip()
        if active and active != win_id:
            log.warning(
                "%s window %s did not take focus (active window is %s), skipping the close request",
                self.name,
                win_id,
                active,
            )
            return False
        if not active:
            log.debug("could not read the active window, trusting the synced activate of %s", win_id)
        return self._xdotool("key", "--clearmodifiers", "alt+F4") is not None

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Stop any running instance and launch Flycast for the given ROM.

        Args:
            rom_path: The resolved disc image or file to boot.
            resume_slot: If not None and a loadable resume state claimed by
                this rom exists, enable auto-load on boot; otherwise boot
                fresh.
        """
        self.stop()
        self._rom_path = rom_path
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        resume_path = _state_path_for(rom_path) if resume_slot is not None else None
        config_opts = [
            "config:Dreamcast.AutoSaveState=yes",
            "config:Dreamcast.SavestateSlot=0",
            "window:fullscreen=yes",
        ]
        if resume_path is not None:
            if not _state_is_loadable(resume_path):
                log.warning("resume requested but no resume state at %s", resume_path)
            elif _state_belongs_to(resume_path, rom_path):
                config_opts.append("config:Dreamcast.AutoLoadState=yes")

        log.info("launching flycast (rom=%s, resume_slot=%s)", rom_path, resume_slot)
        self._spawn(
            [FLYCAST_BIN, "-config", ",".join(config_opts), str(rom_path)],
            base_launch_env(),
        )

    def stop(self) -> None:
        """Request a graceful exit via Alt+F4, then escalate if it does not take.

        Activates the emulator's own window and sends Alt+F4 to trigger
        Flycast's only clean-shutdown path (SDL_QUIT). Falls back to the
        base class's SIGTERM/SIGKILL escalation if no window can be found,
        activated or focused, or if the process has not exited within
        term_timeout.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            win_id = self._window()
            if win_id is not None and self._close_request(win_id):
                try:
                    proc.wait(timeout=self.term_timeout)
                    self._forget()
                    log.info("%s exited gracefully after a close request", self.name)
                    return
                except subprocess.TimeoutExpired:
                    log.warning(
                        "%s did not exit after a close request, escalating to SIGTERM", self.name
                    )
        super().stop()

    def clear_working_slot(self) -> None:
        """Drop every session-owned file left in DATA_DIR before a restore.

        Resume states, their owner markers, VMU images, arcade nvmem and
        eeprom all sit loose in one flat directory, none of them namespaced
        by title or by player, and this runs before the rom is even known
        (activate resolves the rom, clears the slot, then launches). A
        leftover would either outrank its incoming archive member by mtime
        and get skipped by extract_save_archive's newer-file guard, or, with
        no member to replace it at all, be handed to the next player and
        swept into their exit archive as their own progress. Anything still
        here belongs to a session that already exited, so dropping it all is
        the same trade-off DuckStation's clear_working_slot makes.

        `CONTAINER_FILES` and subdirectories (flycast's controller mappings)
        are left: they are container setup, not one session's data. So are
        states set aside under `UNTRUSTED_SUFFIX`, which can be the only copy
        of that progress and which no resume can pick up anyway.
        """
        if not DATA_DIR.is_dir():
            return
        try:
            entries = list(DATA_DIR.iterdir())
        except OSError as exc:
            log.warning("could not list %s to clear the working slot: %s", DATA_DIR, exc)
            return
        for stale in entries:
            if stale.name in CONTAINER_FILES or _is_quarantined(stale.name):
                continue
            if stale.is_dir():
                log.debug("leaving the %s directory in place", stale.name)
                continue
            try:
                stale.unlink()
                log.info("cleared stale save data %s", stale.name)
            except OSError as exc:
                log.warning("could not clear stale save data %s: %s", stale.name, exc)

    def _set_aside_untrusted_state(self, path: Path) -> None:
        """Rename a resume state that a force-killed exit may have torn, marker and all.

        Size and mtime are all the broker has to judge a state by, and that
        is enough to refuse to resume from one but not enough to destroy the
        only copy of a player's progress, so the file is moved to a
        `UNTRUSTED_SUFFIX` sidecar instead of unlinked. A previous sidecar
        for the same rom is replaced, which bounds the leftovers at one per
        title. The owner marker travels with it: left behind it would claim
        the next state flycast writes under the same basename.

        Args:
            path: The resume state to move aside.
        """
        aside = path.with_name(path.name + UNTRUSTED_SUFFIX)
        try:
            path.replace(aside)
        except OSError as exc:
            log.warning("could not set aside untrusted resume state at %s: %s", path, exc)
            return
        log.warning("set aside untrusted resume state at %s as %s", path, aside.name)
        marker = _owner_marker(path)
        if not marker.exists():
            return
        try:
            marker.replace(_owner_marker(aside))
        except OSError as exc:
            log.warning("could not set aside the state marker %s: %s", marker, exc)

    def save_and_exit(self, slot: Optional[int]) -> dict:
        """Stop the emulator and report whether this exit actually saved state.

        A state the exit may have torn is not reported and not resumed from,
        but it is kept: `_set_aside_untrusted_state` renames it rather than
        deleting it. That quarantine does not depend on `slot`, since
        AutoSaveState is on for every launch and the caller dumps DATA_DIR by
        mtime whether or not a slot was asked for.

        Args:
            slot: The resume slot to report on. If None, a torn state is
                still caught and set aside, but a good one is not waited for
                or reported.

        Returns:
            A dict with "state_saved" (bool), "state_slot" (the given
            slot), and "state_file" (a dict of the trusted state file's
            path, size, and mtime, or None). A state file is only trusted
            when the process was not force-killed (no SIGTERM/SIGKILL
            escalation) and its size or mtime changed during this exit and
            then settled, per `_wait_for_state_write`. Returning is what
            releases the caller to zip DATA_DIR, so waiting for the write to
            settle here is what keeps a torn state out of the archive.
        """
        saved = False
        state_file = None
        was_alive = self.alive()
        rom_path = self._rom_path
        proc = self._proc
        p = _state_path_for(rom_path) if rom_path is not None else None
        before = None
        if p is not None:
            try:
                st = p.stat()
                before = (st.st_size, st.st_mtime)
            except OSError as exc:
                log.debug("no resume state at %s before exit: %s", p, exc)

        self.stop()

        # Flycast has no SIGTERM handler at all, so the base class's SIGTERM
        # escalation is already a hard kill here, not just its SIGKILL
        # follow-up: any negative returncode means a signal ended it before
        # dc_exit() could run, and a state file left over from an earlier
        # session for this same rom name must not be reported as this exit's
        # save.
        killed = proc is None or proc.returncode is None or proc.returncode < 0
        if was_alive:
            if p is None:
                if slot is not None:
                    log.warning("save_and_exit requested a slot but no rom is currently loaded")
            elif killed:
                # Independent of `slot`: AutoSaveState is on for every launch,
                # so the kill can have torn a state the caller's mtime-based
                # dump would sweep into this player's archive either way.
                log.warning("flycast had to be force-killed, resume state not trusted")
                try:
                    st = p.stat()
                except OSError as exc:
                    log.debug("no resume state at %s after a force-killed exit: %s", p, exc)
                else:
                    if before is None or before != (st.st_size, st.st_mtime):
                        self._set_aside_untrusted_state(p)
            elif slot is None:
                log.info("flycast exited with no slot requested, resume state left unreported")
            else:
                stamp = _wait_for_state_write(p, before, time.monotonic() + STATE_WAIT)
                if stamp is not None:
                    saved = True
                    state_file = {"path": str(p), "size": stamp[0], "mtime": stamp[1]}
                    _write_owner_marker(p, rom_path)
        return {"state_saved": saved, "state_slot": slot, "state_file": state_file}
