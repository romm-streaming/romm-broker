"""PPSSPP (Sony PSP) launcher: ROM resolution, ini patching, and hotkey save states.

PPSSPP has no control socket. Everything the broker needs pinned lives in two
inis, both patched before every launch. ppsspp.ini gets FirstRun and
CheckForNewVersion so a freshly seeded /config never opens the setup wizard or
an update toast behind a session nobody can click into, and StateSlot so every
save/load hotkey lands on the broker's one working slot without ever cycling
it. controls.ini gets the bracket keys added to Save State and Load State:
F1-F12 never reach PPSSPP through this container's streaming/input stack
(confirmed empirically, including with a real keyboard through the user's own
desktop session), so the brackets go in on every launch rather than being
trusted to survive from a one-off manual bind through PPSSPP's own remap UI.
They are merged into the action's existing comma-separated mapping list, not
written over it, so whatever else the player bound to those actions still
works. Both files are written with a leading UTF-8 BOM; the patcher has to strip
it on read and put it back on write; a naive line scan would loosen the BOM
onto the first `[section]` line and never match it.

Save states have no boot-time load flag, unlike Dolphin's `-s`, so a resume
always goes through the deferred hotkey load below rather than loading before
the window exists to fail into. Unlike Dolphin, PPSSPP writes a screenshot
alongside every state, so the thumbnail here comes from that file rather than
the streamed canvas.
"""

import logging
import os
import re
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from threading import Thread
from typing import Any, Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Root of the RomM library mount (env `ROM_ROOT`, default `/romm`).

A resolved ROM must sit under it; candidates resolving outside are discarded.
"""

CONFIG_DIR = Path(os.environ.get("PPSSPP_CONFIG_DIR", "/config/.config/ppsspp"))
"""PPSSPP's config root (env `PPSSPP_CONFIG_DIR`, default `/config/.config/ppsspp`)."""
PSP_DIR = CONFIG_DIR / "PSP"
"""The emulated memory stick root, holding save data, states and the system inis."""
SYSTEM_DIR = PSP_DIR / "SYSTEM"
"""Directory holding ppsspp.ini and controls.ini."""
INI_PATH = SYSTEM_DIR / "ppsspp.ini"
"""The main ini the broker patches before every launch."""
CONTROLS_INI_PATH = SYSTEM_DIR / "controls.ini"
"""The control mapping ini the broker patches before every launch."""
STATE_DIR = PSP_DIR / "PPSSPP_STATE"
"""Directory PPSSPP writes its `.ppst` save states and their `.jpg` screenshots into."""
PPSSPP_LOG_PATH = Path(os.environ.get("PPSSPP_LOG_PATH", "/config/ppsspp.log"))
"""Log file the broker tails for this emulator (env `PPSSPP_LOG_PATH`, default `/config/ppsspp.log`)."""

STATE_SLOT = int(os.environ.get("PPSSPP_STATE_SLOT", "1"))
"""The one slot the broker works in (env `PPSSPP_STATE_SLOT`, default 1).

RomM holds the library of states, so a requested slot resolves to this one
and the routes echo the effective slot.
"""

SAVE_KEY = "bracketleft"
"""xdotool key for saving the working slot.

F1-F12 never reach PPSSPP through this container's streaming/input stack
(confirmed empirically: bound and unbound alike, nothing was ever
delivered), so the working hotkeys are the bracket keys instead. The
device-1 code is NKCODE (device 1 is the keyboard; device 10 entries
elsewhere in this file are the gamepad's own, unrelated numbering) per
PPSSPP's `Qt/NKCodeFromQt.h`, which maps `Qt::Key_BracketLeft/Right` to
`NKCODE_LEFT_BRACKET/RIGHT_BRACKET` (71/72, `Common/Input/KeyCodes.h`), not
the 132/134 an earlier remap-UI capture recorded, which traced back to the
same broken injection path this binding works around.
"""
LOAD_KEY = "bracketright"
"""xdotool key for loading the working slot; see `SAVE_KEY` for why it is a bracket key."""

STATE_WAIT = float(os.environ.get("PPSSPP_STATE_WAIT", "20.0"))
"""Seconds a save state has to land on disk after the save hotkey (env `PPSSPP_STATE_WAIT`, default 20)."""
STATE_STABLE = float(os.environ.get("PPSSPP_STATE_STABLE", "1.5"))
"""Seconds a state's size and mtime must both hold still before the write counts as finished.

From env `PPSSPP_STATE_STABLE`, default 1.5. Long enough that a stalled write
is not mistaken for a finished one, short enough to stay well inside
`STATE_WAIT`.
"""
STATE_SHOT_WAIT = float(os.environ.get("PPSSPP_STATE_SHOT_WAIT", "5.0"))
"""Seconds the screenshot beside a finished state gets to land (env `PPSSPP_STATE_SHOT_WAIT`, default 5).

Its own short window rather than the rest of `STATE_WAIT`: the state itself is
already confirmed by the time this is waited on, so a screenshot PPSSPP never
writes costs a few seconds instead of the whole save budget.
"""
STATE_SHOT_STABLE = 0.5
"""Seconds the screenshot's size must hold still before the write counts as finished.

Shorter than `STATE_STABLE`: a jpeg thumbnail is orders of magnitude smaller
than the state it belongs to.
"""

RESUME_LOAD_WAIT = float(os.environ.get("PPSSPP_RESUME_LOAD_WAIT", "90.0"))
"""Seconds a deferred resume has to find both a state file and a game window (env `PPSSPP_RESUME_LOAD_WAIT`).

Defaults to 90. One budget covers both: a resume state is usually already on
disk from the save archive, so nearly all of it goes on the boot.
"""
RESUME_LOAD_SETTLE = float(os.environ.get("PPSSPP_RESUME_LOAD_SETTLE", "12.0"))
"""Seconds to wait after the game window is up before the load hotkey goes in.

From env `PPSSPP_RESUME_LOAD_SETTLE`, default 12. PPSSPP titles its window
with the game as soon as the disc is mounted, but keeps loading and
registering HLE module state well past that point, and a load that lands
before the event table is complete leaves the core reporting an unregistered
event and a black screen. This is not the wait for the window itself (that is
polled for): it is the margin between a window saying a game is running and a
core that can actually be loaded into.
"""

ROM_EXTENSIONS = (".chd", ".cso", ".pbp", ".iso", ".elf", ".prx")
"""All formats PPSSPP boots directly, best first.

Best means most likely to be the verified, compressed copy; a folder holding
several candidates picks by this order. PSP has no multi-disc titles, so
there is no disc number to rank on.
"""
_ROM_SEARCH_GLOBS = ("*", "*/*")

_STAGING_SUFFIX = ".tmp"
"""Suffix PPSSPP saves a state under before renaming it over the real name once the save succeeds."""

_STATE_NAME_RE = re.compile(r"^(?P<prefix>[^/]+)_(?P<slot>\d+)\.ppst$")
"""Matches `<game id>_<version>_<slot>.ppst`, the name PPSSPP builds for a save state.

The screenshot beside it shares the same stem with a `.jpg` extension.
"""

_XDOTOOL = os.environ.get("XDOTOOL_BIN", "xdotool")
_WINDOW_CLASS = "PPSSPPQt"
_GAME_TITLE_MARK = " - "
"""Substring a PPSSPP window title gains once a game is running.

Before a game is running the title is just `PPSSPP <ver>`; once one is
loaded PPSSPP appends ` - <id> : <name>`, which is what a hotkey needs.
"""

_INI_SECTION = "General"
_INI_PATCHES: dict[tuple[str, str], str] = {
    (_INI_SECTION, "FirstRun"): "False",
    (_INI_SECTION, "CheckForNewVersion"): "False",
    (_INI_SECTION, "StateSlot"): str(STATE_SLOT),
}
"""Settings forced into ppsspp.ini: no first-run wizard, no update toast, and the working slot pinned."""

_CONTROLS_SECTION = "ControlMapping"
_CONTROLS_PATCHES: dict[tuple[str, str], str] = {
    (_CONTROLS_SECTION, "Save State"): "1-71",
    (_CONTROLS_SECTION, "Load State"): "1-72",
}
"""Bindings added to controls.ini: device-1 (keyboard) NKCODE for the bracket keys.

See the `SAVE_KEY` note for where 71/72 come from. These are merged into
whatever the action already carries rather than replacing it, so a pad button
or a second key the player mapped to Save State keeps working.
"""


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Optional[Path]:
    """Pick the best bootable ROM out of a set of candidate paths.

    Hidden files, unsupported extensions, non-files and anything resolving
    outside `ROM_ROOT` are dropped. The rest rank by position in
    `ROM_EXTENSIONS`, then by depth and name.

    Args:
        candidates: Paths found under the ROM folder.
        base: The ROM folder the candidates are relative to.

    Returns:
        The resolved path of the winning ROM, or None when nothing qualifies.
    """
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
        ranked.append((ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real))
    if not ranked:
        return None
    return min(ranked)[-1]


def _merge_binding(existing: str, required: str) -> str:
    """Add one mapping to an action's binding list without dropping the rest.

    PPSSPP keeps every binding for an action on a single comma-separated
    line, so rewriting that line whole is what silently unmaps the pad button
    or second key a player put on the same action.

    Args:
        existing: The value side of the action's line, as the ini holds it.
        required: The `device-keycode` mapping the broker needs bound.

    Returns:
        The action's mappings with `required` appended, or unchanged when it is already there.
    """
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    if required not in parts:
        parts.append(required)
    return ",".join(parts)


def _patch_ini_file(
    path: Path,
    default_section: str,
    patches: dict[tuple[str, str], str],
    merge_existing: bool = False,
) -> None:
    """Force a set of settings into one of PPSSPP's inis before every launch.

    Written and read with a leading UTF-8 BOM, matching PPSSPP's own files: a
    naive scan without `utf-8-sig` would loosen the BOM onto the first
    `[section]` line and never match it. A missing file is seeded with just
    the forced settings and PPSSPP fills in the rest. Existing keys are
    rewritten in place, missing ones are added under their section (created
    when absent), and the result is written through a temp file. Any failure
    is logged rather than raised so the launch still goes ahead.

    Args:
        path: The ini file to patch.
        default_section: Section header to seed when the file does not exist yet.
        patches: `(section, key)` to the value the broker needs the key to carry.
        merge_existing: Treat an existing value as a comma-separated list and
            add the broker's value to it instead of replacing the line. Set
            for controls.ini, where the line is the player's whole mapping for
            that action, not a setting the broker owns.
    """
    try:
        if not path.exists():
            # First run: write just the forced settings, PPSSPP fills in the rest.
            path.parent.mkdir(parents=True, exist_ok=True)
            seeded = "\n".join(f"{key} = {val}" for (_sec, key), val in patches.items())
            path.write_text("﻿" + f"[{default_section}]\n" + seeded + "\n", encoding="utf-8")
            return
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        section = ""
        applied: set[tuple[str, str]] = set()
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                new_lines.append(line)
                continue
            matched = False
            for (sec, key), val in patches.items():
                if section != sec:
                    continue
                if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
                    value = val
                    if merge_existing:
                        value = _merge_binding(stripped.split("=", 1)[1], val)
                    new_lines.append(f"{key} = {value}")
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        missing = [(s, k, v) for (s, k), v in patches.items() if (s, k) not in applied]
        if missing:
            present = {
                ln.strip()[1:-1]
                for ln in new_lines
                if ln.strip().startswith("[") and ln.strip().endswith("]")
            }
            for sec, key, val in missing:
                if sec in present:
                    out: list[str] = []
                    inserted = False
                    for ln in new_lines:
                        out.append(ln)
                        if not inserted and ln.strip() == f"[{sec}]":
                            out.append(f"{key} = {val}")
                            inserted = True
                    new_lines = out
                else:
                    new_lines.extend(["", f"[{sec}]", f"{key} = {val}"])
                    present.add(sec)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("﻿" + "\n".join(new_lines) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.exception("%s patch failed, broker settings NOT applied", path.name)


def _patch_config() -> None:
    """Patch both ppsspp.ini and controls.ini with the broker's forced settings."""
    _patch_ini_file(INI_PATH, _INI_SECTION, _INI_PATCHES)
    _patch_ini_file(CONTROLS_INI_PATH, _CONTROLS_SECTION, _CONTROLS_PATCHES, merge_existing=True)


def _state_for_slot(slot: int) -> Optional[Path]:
    """Find the most recently written state in `slot`.

    Args:
        slot: The slot number, matched as the `_<slot>.ppst` suffix.

    Returns:
        The newest state in `slot` by mtime, or None if it holds nothing.
    """
    if not STATE_DIR.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in STATE_DIR.glob(f"*_{slot}.ppst"):
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            pass
    if not candidates:
        return None
    return max(candidates)[1]


def _snapshot() -> dict[Path, tuple[int, float]]:
    """Snapshot every state in the broker's working slot.

    Returns:
        A dict of state path to `(size, mtime)`, empty when the directory is missing. Files that
        vanish mid-scan are skipped.
    """
    if not STATE_DIR.is_dir():
        return {}
    snap: dict[Path, tuple[int, float]] = {}
    for p in STATE_DIR.glob(f"*_{STATE_SLOT}.ppst"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return snap


def _staging_in_flight(state: Path) -> bool:
    """Whether PPSSPP still has the staging file for `state` on disk.

    Args:
        state: The state file the save is expected to land on.

    Returns:
        True while a `_STAGING_SUFFIX` sibling exists, False when it is gone
        or cannot be looked at.
    """
    try:
        return state.with_name(state.name + _STAGING_SUFFIX).exists()
    except OSError as exc:
        log.warning("could not look for a staging file beside %s: %s", state.name, exc)
        return False


def _wait_for_state_write(before: dict[Path, tuple[int, float]], deadline: float) -> bool:
    """Poll the working slot until a write completes or the deadline passes.

    The hotkey is fire-and-forget, so the file itself is the only
    acknowledgement there is. A write only counts as complete once the state
    is non-empty, PPSSPP's staging file for it is gone (it saves to that name
    and renames it over the real one once the save succeeds), and neither
    size nor mtime has moved for `STATE_STABLE`. Size alone over a short
    window is not enough: an emulator that stalls mid-write passes that test,
    and the broker would ship a truncated state to RomM as the player's
    progress. A target that disappears mid-write is dropped and the scan
    starts over.

    Args:
        before: Snapshot from `_snapshot` taken before the hotkey was sent.
        deadline: `time.monotonic` value to give up at.

    Returns:
        True once a new or modified state has settled, False on timeout.
    """
    POLL_SECS = 0.1
    target: Optional[Path] = None
    last: Optional[tuple[int, float]] = None
    stable_since = 0.0
    while time.monotonic() < deadline:
        after = _snapshot()
        if target is None:
            for p, stamp in after.items():
                if before.get(p) != stamp:
                    target = p
                    last = stamp
                    stable_since = time.monotonic()
                    break
        else:
            cur = after.get(target)
            if cur is None:
                log.warning("save state %s vanished mid-write, waiting for another", target.name)
                target = None
                last = None
            elif cur != last:
                last = cur
                stable_since = time.monotonic()
            elif cur[0] == 0 or _staging_in_flight(target):
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= STATE_STABLE:
                log.info("save state write complete: %s (%d bytes)", target.name, cur[0])
                return True
        time.sleep(POLL_SECS)
    if target is not None:
        log.warning("save state %s never finished writing before the deadline", target.name)
    else:
        log.warning("no save state was written before the deadline")
    return False


def _wait_for_screenshot(state: Path, deadline: float) -> None:
    """Wait for the screenshot PPSSPP writes beside `state` to finish landing.

    Unlike the state, the screenshot is written in place with no staging file
    to rename, so nothing but its size holding still marks the end of that
    write, and the state-screenshot route serves it straight off disk the
    moment a save reports done. A screenshot that never settles is only
    logged: the state is already confirmed good, and losing a preview should
    not read as losing the save.

    Args:
        state: The state file the save settled on.
        deadline: `time.monotonic` value to give up at.
    """
    POLL_SECS = 0.1
    shot = state.with_suffix(".jpg")
    last: Optional[int] = None
    stable_since = 0.0
    while time.monotonic() < deadline:
        try:
            size = shot.stat().st_size
        except FileNotFoundError:
            size = 0
        except OSError as exc:
            log.warning("could not stat the screenshot beside %s: %s", state.name, exc)
            return
        if size == 0:
            last = None
        elif size != last:
            last = size
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= STATE_SHOT_STABLE:
            log.debug("save state screenshot complete: %s (%d bytes)", shot.name, size)
            return
        time.sleep(POLL_SECS)
    log.warning("save state screenshot %s never settled; the state itself is good", shot.name)


def _restamp_slot(filename: str, slot: int) -> Optional[str]:
    """Rename a state for `slot`, keeping the game id and version that tie it to its game.

    PPSSPP resolves a state by the game id (and version) in the name, so that
    is what has to survive the trip; the slot a stored capture happens to
    carry is rewritten into this broker's one working slot.

    Args:
        filename: The basename a stored state arrived with.
        slot: The slot to stamp into the name.

    Returns:
        The same state named for `slot`, or None if `filename` is not a state name.
    """
    match = _STATE_NAME_RE.match(filename)
    if match is None:
        return None
    return f"{match.group('prefix')}_{slot}.ppst"


class Ppsspp(Emulator):
    """Sony PSP sessions on PPSSPPQt.

    The broker launches `PPSSPPQt --fullscreen -- <rom>` after patching both
    of PPSSPP's inis: ppsspp.ini so a fresh container never shows the setup
    wizard or an update toast and so the working state slot is pinned, and
    controls.ini so Save State and Load State are bound to the bracket keys,
    the only hotkeys that reach PPSSPP through this container's input stack.
    Both files carry a UTF-8 BOM that the patcher strips and restores. Save
    and load are hotkey only: the game window is activated, the key is sent
    through XTEST, and for a save the state directory is polled until the
    file settles, since the hotkey gives no acknowledgement. There is no
    boot-time state-load flag, so a resume always goes through a deferred
    thread that waits for the state file, polls for the game window, and only
    then sends the load hotkey.

    Save data (`SAVEDATA`) and states (`PPSSPP_STATE`) both ride the save
    archive. A state is named for the game id and version, so pushed names
    are restamped into the broker's slot and the working slot is cleared
    before a boot. PPSSPP writes a `.jpg` screenshot beside every state, so
    the thumbnail comes from that file rather than the streamed canvas, and
    clearing a state drops its screenshot too.

    Attributes:
        name: RomM platform key, `ppsspp`.
        display_name: Human-readable name shown in the UI.
        save_root: The emulated memory stick root, which the save subtrees hang off.
        save_subtrees: `SAVEDATA` and `PPSSPP_STATE`, the directories the save archive carries.
        rom_extensions: Bootable ROM formats, best first.
        supports_states: True, states are saved and loaded over the bracket hotkeys.
        state_slot: The one slot the broker works in, echoed back as the effective slot.
        state_dir: Where PPSSPP writes `.ppst` files.
        log_path: The PPSSPP log the broker exposes.
    """

    name = "ppsspp"
    display_name = "PPSSPP"
    save_root = PSP_DIR
    save_subtrees = ("SAVEDATA", "PPSSPP_STATE")
    state_subtrees = ("PPSSPP_STATE",)
    rom_extensions = ROM_EXTENSIONS
    supports_states = True
    state_slot = STATE_SLOT
    state_dir = STATE_DIR
    log_path = PPSSPP_LOG_PATH

    def __init__(self) -> None:
        """Set up the process state and the launch sequence counter that fences deferred loads."""
        super().__init__()
        self._launch_seq = 0

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a RomM path to the ROM to boot.

        A file is taken as is. A directory is searched one level deep for the
        best candidate by `_pick_rom_file`. A pattern that cannot be walked
        (an unreadable subdirectory, a broken mount) costs only what that
        pattern would have contributed: the candidates already found still
        rank, since reporting a title unbootable over one bad directory is
        worse than booting the best of what could be read.

        Args:
            path: The ROM file or folder RomM handed over.

        Returns:
            The ROM to pass to PPSSPPQt, or None when there is nothing bootable.
        """
        if path.is_file():
            # Defense in depth: api.py already validates path is under
            # ROM_ROOT before calling in, but this checks it independently
            # rather than trusting every future caller to do the same.
            try:
                if not path.resolve().is_relative_to(ROM_ROOT):
                    return None
            except OSError:
                return None
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
        """Run one xdotool command against the session display.

        Args:
            *args: Arguments passed to the xdotool binary.

        Returns:
            Its stdout, or None if it could not be run, timed out, or exited non-zero.
        """
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

    def _game_window(self, log_missing: bool = True) -> Optional[str]:
        """Find the window this launch is running the game in.

        Two things have to hold. The window must belong to the process this
        broker spawned, since a window left behind by the previous emulator
        process carries the same class and title shape and would swallow the
        save hotkey, and its title must carry `_GAME_TITLE_MARK`, which
        PPSSPP only appends once a game is loaded: before that the window is
        a menu a hotkey does nothing useful to.

        Args:
            log_missing: Whether a miss is worth a warning. Off while polling
                a booting emulator, where a miss is the expected answer until
                the game comes up.

        Returns:
            The X window id as xdotool prints it, or None when this launch has no game window up.
        """
        proc = self._proc
        if proc is None:
            return None
        out = self._xdotool("search", "--class", _WINDOW_CLASS)
        if out is None:
            return None
        for win_id in out.split():
            pid = self._xdotool("getwindowpid", win_id)
            if pid is None or pid.strip() != str(proc.pid):
                continue
            name = self._xdotool("getwindowname", win_id)
            if name and _GAME_TITLE_MARK in name:
                return win_id
        if log_missing:
            log.warning("no ppsspp game window found for pid %s", proc.pid)
        return None

    def _wait_for_game_window(self, deadline: float) -> bool:
        """Poll until this launch has a game window up, or `deadline` passes.

        Args:
            deadline: `time.monotonic` value to give up at.

        Returns:
            True once `_game_window` names a window, False on timeout.
        """
        POLL_SECS = 1.0
        while time.monotonic() < deadline:
            if self._game_window(log_missing=False) is not None:
                return True
            time.sleep(POLL_SECS)
        return self._game_window(log_missing=False) is not None

    def _send_key(self, key: str) -> bool:
        """Focus the game window and send `key` through XTEST.

        Activating first is what makes this survive the player clicking back
        into the page: XTEST delivers to whatever holds focus, so a key sent
        at an unfocused PPSSPP goes to the desktop instead.

        Args:
            key: The key name in xdotool's syntax, for example `bracketleft`.

        Returns:
            True when the window was found, activated and the key sent, False otherwise.
        """
        win_id = self._game_window()
        if win_id is None:
            return False
        if self._xdotool("windowactivate", "--sync", win_id) is None:
            return False
        return self._xdotool("key", "--clearmodifiers", key) is not None

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Stop any running instance, patch the inis, and start PPSSPPQt.

        The binary comes from env `PPSSPP_BIN` (default `PPSSPPQt`). With
        `resume_slot` set, a deferred thread waits for the state file and
        loads it over the hotkey once the window is up.

        Args:
            rom_path: The ROM to boot.
            resume_slot: Slot to resume from, or None to boot clean.
        """
        self.stop()
        _patch_config()
        self._launch_seq += 1
        seq = self._launch_seq

        binary = os.environ.get("PPSSPP_BIN", "PPSSPPQt")
        env = base_launch_env()
        cmd = [binary, "--fullscreen", "--", str(rom_path)]
        log.info("launching ppsspp (rom=%s, resume_slot=%s)", rom_path, resume_slot)
        self._spawn(cmd, env)

        # PPSSPP has no boot-time state-load flag, so a resume always has to
        # go in over the hotkey once the window is up.
        if resume_slot is not None:
            Thread(target=self._deferred_load_state, args=(seq,), daemon=True).start()

    def _deferred_load_state(self, seq: int) -> None:
        """Wait for the resume state and a booted game, then load it over the hotkey.

        Both waits share `RESUME_LOAD_WAIT`. The window wait is the
        load-bearing one: a resume state restored from the save archive is
        already on disk when the thread starts, so waiting only on the file
        would fire the one hotkey this path ever sends seconds into a boot
        with no window to take it, losing the resume silently. Once the game
        window is up, `RESUME_LOAD_SETTLE` covers the rest of PPSSPP's own
        boot. Abandons itself whenever `seq` no longer matches the current
        launch, so a superseded launch never gets a stray load.

        Args:
            seq: The launch sequence number this load belongs to.
        """
        deadline = time.monotonic() + RESUME_LOAD_WAIT
        if not self.wait_for_state(deadline):
            log.warning("resume: no state file ever arrived")
            return
        if self._launch_seq != seq:
            log.info("resume: launch superseded, load abandoned")
            return
        if not self._wait_for_game_window(deadline):
            log.warning("resume: no game window came up in time, load abandoned")
            return
        if self._launch_seq != seq:
            log.info("resume: launch superseded, load abandoned")
            return
        time.sleep(RESUME_LOAD_SETTLE)
        if self._launch_seq != seq:
            return
        if self.load_state(STATE_SLOT):
            log.info("resume: deferred load delivered")
        else:
            log.warning("resume: deferred load failed, the session starts the game from scratch")

    def save_state(self, slot: int) -> bool:
        """Save a state into the broker's slot over the hotkey and wait for it to land.

        `slot` is what RomM asked for and is ignored: this saves into
        `STATE_SLOT` and the caller reads the effective slot back off
        `state_slot`.

        The screenshot PPSSPP writes beside the state is waited on too, since
        a save reported done is what sends RomM to fetch the thumbnail. A
        screenshot that never settles does not fail the save.

        Args:
            slot: The slot RomM requested; not used.

        Returns:
            True once the state file has been written and settled within `STATE_WAIT`, False if
            the hotkey could not be sent or the write never completed.
        """
        before = _snapshot()
        if not self._send_key(SAVE_KEY):
            return False
        if not _wait_for_state_write(before, time.monotonic() + STATE_WAIT):
            return False
        state = self.state_path()
        if state is not None:
            _wait_for_screenshot(state, time.monotonic() + STATE_SHOT_WAIT)
        return True

    def load_state(self, slot: int) -> bool:
        """Load the broker's slot over the hotkey.

        The hotkey is silent on an empty slot, so an absent file has to be
        caught here or the caller reads a no-op as success.

        Args:
            slot: The slot RomM requested; the broker's `STATE_SLOT` is what gets loaded.

        Returns:
            True when a state file exists and the hotkey was sent, False otherwise.
        """
        if self.state_path() is None:
            log.warning("load state: slot %d holds no state file", STATE_SLOT)
            return False
        return self._send_key(LOAD_KEY)

    def state_path(self) -> Optional[Path]:
        """Return the newest state file in the broker's slot, or None when it holds nothing."""
        return _state_for_slot(STATE_SLOT)

    def state_screenshot_path(self) -> Optional[Path]:
        """Return the `.jpg` PPSSPP wrote beside the current state, or None when there is none."""
        state = self.state_path()
        if state is None:
            return None
        shot = state.with_suffix(".jpg")
        return shot if shot.is_file() else None

    def clear_working_slot(self) -> None:
        """Delete everything the broker's slot holds before a new session boots.

        A state is named for the game it was taken from, and the game id only
        comes off the running disc, so a leftover cannot be told apart from
        the state of the game about to boot. Anything still here belongs to a
        session that has already exited and whose states RomM holds.

        The staging file goes with them. A session killed mid-save leaves one
        behind with no state to pair it against, and the save archive sweeps
        up whatever sits in the state tree, so it would ship to RomM as a
        state of its own. So would a screenshot whose state is already gone,
        which is why those are swept by name rather than only alongside the
        state they belong to.
        """
        if not STATE_DIR.is_dir():
            return
        patterns = (
            f"*_{STATE_SLOT}.ppst",
            f"*_{STATE_SLOT}.ppst{_STAGING_SUFFIX}",
            f"*_{STATE_SLOT}.jpg",
        )
        for pattern in patterns:
            for stale in STATE_DIR.glob(pattern):
                try:
                    stale.unlink()
                    log.info("cleared stale state file %s", stale.name)
                except OSError as exc:
                    log.warning("could not clear stale state file %s: %s", stale.name, exc)

    def state_target(self, filename: str) -> Optional[Path]:
        """Map a pushed state's filename to where it may be written.

        With the slot already holding a state, a pushed name has to match it;
        otherwise the game id is taken on trust, bounded to a
        `<game>_<slot>.ppst` basename in the state dir.

        Args:
            filename: The basename RomM is pushing.

        Returns:
            The path to write to, or None when the name is not a state name, carries a path
            component, or does not match the state already in the slot.
        """
        if "/" in filename or filename in ("", ".", ".."):
            return None
        restamped = _restamp_slot(filename, STATE_SLOT)
        if restamped is None:
            return None
        existing = self.state_path()
        if existing is not None:
            return existing if restamped == existing.name else None
        return STATE_DIR / restamped

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Save a state if asked, then stop the emulator.

        Args:
            slot: Slot RomM asked to save into (resolved to `STATE_SLOT`), or None to exit
                without saving a state.

        Returns:
            A dict with `state_saved` (bool), `state_slot` (the effective slot, or None when no
            save was requested) and `state_file` (a dict of `path`, `size` and `mtime` for the
            saved state, or None).
        """
        saved = False
        state_file: Optional[dict[str, Any]] = None
        if slot is not None and self.alive():
            saved = self.save_state(slot)
            if saved:
                p = self.state_path()
                if p is not None:
                    try:
                        st = p.stat()
                    except OSError as exc:
                        log.warning("could not stat saved state %s: %s", p, exc)
                        saved = False
                    else:
                        state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}
        self.stop()
        return {
            "state_saved": saved,
            "state_slot": STATE_SLOT if slot is not None else None,
            "state_file": state_file,
        }

    def stop(self) -> None:
        """Invalidate any in-flight deferred state load before the kill."""
        self._launch_seq += 1
        super().stop()
