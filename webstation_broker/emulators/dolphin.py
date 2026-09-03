"""Dolphin (GameCube/Wii) launcher: ROM resolution, hotkey save states, and boot-time resume.

Dolphin has no control socket, but it takes every setting it needs on the
command line: `-C` writes into the layered config, so nothing here has to
patch an INI, and `-s` loads a state file by path at boot, so a resume that
is already on disk never depends on a keystroke landing. What is left is the
mid-session save, which only the hotkey can reach; the broker runs as the
same user as the session, so xdotool talks to Xwayland directly.

Save states are not thumbnailed here. The frame comes off the streamed canvas
in the browser, which is the only capture that cannot stall the emulator.
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

A resolved disc image must sit under it; candidates resolving outside are discarded.
"""

USER_DIR = Path(os.environ.get("DOLPHIN_USER_DIR", "/config/.local/share/dolphin-emu"))
"""Dolphin's user directory, passed with `-u` (env `DOLPHIN_USER_DIR`)."""
STATE_DIR = USER_DIR / "StateSaves"
"""Directory Dolphin writes its `.sNN` save states into."""
CONFIG_DIR = USER_DIR / "Config"
"""Directory holding Dolphin's INI files, where the pad bindings are seeded."""
DOLPHIN_LOG_PATH = Path(os.environ.get("DOLPHIN_LOG_PATH", "/config/dolphin.log"))
"""Log file the broker tails for this emulator (env `DOLPHIN_LOG_PATH`, default `/config/dolphin.log`)."""

STATE_SLOT = int(os.environ.get("DOLPHIN_STATE_SLOT", "1"))
"""The one slot the broker works in (env `DOLPHIN_STATE_SLOT`, default 1).

RomM holds the library of states, so a requested slot resolves to this one
and the routes echo the effective slot.
"""

GC_SLOT_A_DEVICE = 8
"""`EXIDeviceType::MemoryCardFolder`, the GC slot A device pinned at launch.

It keeps saves as loose `.gci` files under `GC/<region>/Card A` rather than
one `.raw` card image.
"""

SAVE_KEY = f"shift+F{STATE_SLOT}"
"""xdotool key for saving the working slot; Dolphin's own default is `Shift+F<n>` for slot n."""
LOAD_KEY = f"F{STATE_SLOT}"
"""xdotool key for loading the working slot; Dolphin's own default is `F<n>` for slot n."""

STATE_WAIT = float(os.environ.get("DOLPHIN_STATE_WAIT", "20.0"))
"""Seconds a save state has to land on disk after the save hotkey (env `DOLPHIN_STATE_WAIT`, default 20)."""
LOAD_WAIT = float(os.environ.get("DOLPHIN_LOAD_WAIT", "20.0"))
"""Seconds Dolphin has to read the state back after the load hotkey (env `DOLPHIN_LOAD_WAIT`, default 20)."""
LOAD_SETTLE = float(os.environ.get("DOLPHIN_LOAD_SETTLE", "2.0"))
"""Seconds a load gets to deserialize after the state file has been read (env `DOLPHIN_LOAD_SETTLE`).

Nothing observable marks the end of the deserialize, only the read that starts
it, so this is the one bounded guess in the load path. Defaults to 2 seconds.
"""
_ATIME_BACKDATE = 1.0
"""Seconds a state's access time is set behind its mtime, so the next read of it stands out."""
RESUME_LOAD_WAIT = float(os.environ.get("DOLPHIN_RESUME_LOAD_WAIT", "90.0"))
"""Seconds a deferred resume waits for a state file to arrive (env `DOLPHIN_RESUME_LOAD_WAIT`, default 90)."""
RESUME_LOAD_SETTLE = float(os.environ.get("DOLPHIN_RESUME_LOAD_SETTLE", "5.0"))
"""How long the window has to be up before a hotkey is worth sending (env `DOLPHIN_RESUME_LOAD_SETTLE`).

Dolphin maps its window before the core is running, and a load that early is
dropped. Defaults to 5 seconds.
"""

VIDEO_BACKEND = os.environ.get("DOLPHIN_VIDEO_BACKEND", "OGL")
"""Video backend passed with `-v` (env `DOLPHIN_VIDEO_BACKEND`, default `OGL`).

OGL over Vulkan by default: RADV on the integrated AMD parts these containers
run on has been the less reliable of the two.
"""

ROM_EXTENSIONS = (".rvz", ".wia", ".gcz", ".iso", ".gcm", ".ciso", ".wbfs", ".wad", ".dol", ".elf")
"""Discs Dolphin boots, best first.

A folder holding several candidates picks the compressed image over the raw
one beside it.
"""
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)

_STATE_NAME_RE = re.compile(r"^(?P<game>[^/]+)\.s\d{2}$")
"""Matches `<game id>.s01`, the name Dolphin builds for a save state."""

_GC_PLATFORM = "ngc"
"""RomM platform slug for GameCube, the only Dolphin platform with a physical memory card."""

_GAME_ID_LEN = 6
"""Length of the game id a disc header opens with and Dolphin stamps into a state."""
_GAME_ID_RE = re.compile(r"^[A-Za-z0-9]{6}$")
"""A game id the broker will compare on: six alphanumerics, e.g. `GXCE01`."""
_ID_OFFSETS = {".iso": 0, ".gcm": 0, ".wbfs": 0x200}
"""Disc formats that keep the game id in the clear, and the offset it sits at.

A raw GameCube or Wii image opens with the disc header, and a WBFS file keeps
a copy of that header at 0x200. The compressed formats (.rvz, .wia, .gcz,
.ciso) hold it behind their own container, so a ROM in one of those has no id
to check a state against.
"""


def _game_id_at(path: Path, offset: int) -> Optional[str]:
    """Read a game id out of `path` at `offset`.

    Args:
        path: The disc image or state file to read.
        offset: Byte offset the id starts at.

    Returns:
        The id, or None when it cannot be read or is not six alphanumerics.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read(_GAME_ID_LEN)
    except OSError as exc:
        log.warning("could not read a game id out of %s: %s", path, exc)
        return None
    try:
        game_id = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return game_id if _GAME_ID_RE.match(game_id) else None


def _rom_game_id(rom_path: Path) -> Optional[str]:
    """The game id of the disc about to boot, for a format that stores one in the clear.

    Args:
        rom_path: The image being booted.

    Returns:
        The six-character id, or None for a format that keeps it compressed.
    """
    offset = _ID_OFFSETS.get(rom_path.suffix.lower())
    if offset is None:
        return None
    return _game_id_at(rom_path, offset)


_XDOTOOL = os.environ.get("XDOTOOL_BIN", "xdotool")
_WINDOW_CLASS = "dolphin-emu"
"""WM class shared by Dolphin's render window, its main window and its dialogs.

Only the render window titles itself with the running game, in a
`Dolphin <ver> | JIT64 | OpenGL | <game>` line, and only it takes hotkeys.
"""
_RENDER_TITLE_MARK = " | "
"""Substring that singles the render window's title out from the main window and dialogs."""


def _disc_number(rel: Path) -> int:
    """Return the disc number a relative ROM path names, or 1 when it names none.

    Args:
        rel: Candidate path relative to the ROM folder being searched.

    Returns:
        The number following a `disc`, `disk` or `cd` marker in the path, never below 1.
    """
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Optional[Path]:
    """Pick the best bootable disc image out of a set of candidate paths.

    Hidden files, unsupported extensions, non-files and anything resolving
    outside `ROM_ROOT` are dropped. The rest rank by disc number, then by
    position in `ROM_EXTENSIONS`, then by depth and name, so disc 1 in the
    best format wins.

    Args:
        candidates: Paths found under the ROM folder.
        base: The ROM folder the candidates are relative to.

    Returns:
        The resolved path of the winning image, or None when nothing qualifies.
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
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


_GCPAD_TEMPLATE = """[GCPad{n}]
Device = SDL/{i}/Microsoft X-Box 360 pad
Buttons/A = `Button E`
Buttons/B = `Button S`
Buttons/X = `Button N`
Buttons/Y = `Button W`
Buttons/Z = `Shoulder L`
Buttons/Start = Start
Main Stick/Up = `Left Y+`
Main Stick/Down = `Left Y-`
Main Stick/Left = `Left X-`
Main Stick/Right = `Left X+`
Main Stick/Calibration = 100.00 141.42 100.00 141.42 100.00 141.42 100.00 141.42
C-Stick/Up = `Right Y+`
C-Stick/Down = `Right Y-`
C-Stick/Left = `Right X-`
C-Stick/Right = `Right X+`
C-Stick/Calibration = 100.00 141.42 100.00 141.42 100.00 141.42 100.00 141.42
Triggers/L = `Trigger L`
Triggers/R = `Trigger R`
D-Pad/Up = `Pad N`
D-Pad/Down = `Pad S`
D-Pad/Left = `Pad W`
D-Pad/Right = `Pad E`
Triggers/L-Analog = `Thumb L`
Triggers/R-Analog = `Thumb R`
"""
"""GCPadNew.ini section template, formatted per pad with `n` (1-based) and `i` (0-based SDL index).

Selkies presents the browser gamepad as an SDL device, and Dolphin ships no
default binding for one, so an unconfigured container has no usable pad.
"""


def _seed_gcpad() -> None:
    """Write the pad bindings for four pads once, if the file is not already there.

    Seeded rather than patched so a player's own remapping, which Dolphin
    writes back to this same file, survives every later launch. A write
    failure is logged, not raised.
    """
    path = CONFIG_DIR / "GCPadNew.ini"
    if path.exists():
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(_GCPAD_TEMPLATE.format(n=i + 1, i=i) for i in range(4))
        )
        log.info("seeded %s", path)
    except OSError as exc:
        log.warning("could not seed the pad bindings at %s: %s", path, exc)


def _state_for_slot(slot: int) -> Optional[Path]:
    """Find the most recently written state in `slot`.

    Args:
        slot: The slot number, matched as a two-digit `.sNN` suffix.

    Returns:
        The newest state in `slot` by mtime, or None if it holds nothing.
    """
    if not STATE_DIR.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in STATE_DIR.glob(f"*.s{slot:02d}"):
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            pass
    if not candidates:
        return None
    return max(candidates)[1]


def _resume_state(rom_path: Path) -> Optional[Path]:
    """The working slot's state, when it belongs to the ROM being booted.

    Dolphin loads a `-s` state whatever game it was taken from, and a state
    opens with the game id it belongs to, so the two can be compared before it
    is handed over. A ROM whose format hides its own id is taken on trust:
    refusing every compressed image would cost far more resumes than the
    mismatch it guards against.

    Args:
        rom_path: The image about to boot.

    Returns:
        The state to resume from, or None when the slot is empty or holds another game's state.
    """
    state = _state_for_slot(STATE_SLOT)
    if state is None:
        return None
    rom_id = _rom_game_id(rom_path)
    if rom_id is None:
        return state
    state_id = _game_id_at(state, 0)
    if state_id == rom_id:
        return state
    log.warning(
        "resume: state %s belongs to game id %s, not %s from %s, refusing it",
        state.name,
        state_id,
        rom_id,
        rom_path.name,
    )
    return None


def _snapshot() -> dict[Path, tuple[int, float]]:
    """Snapshot every state in the broker's working slot.

    Returns:
        A dict of state path to `(size, mtime)`, empty when the directory is missing. Files that
        vanish mid-scan are skipped.
    """
    if not STATE_DIR.is_dir():
        return {}
    snap: dict[Path, tuple[int, float]] = {}
    for p in STATE_DIR.glob(f"*.s{STATE_SLOT:02d}"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return snap


def _holds_open(pid: Optional[int], path: Path) -> bool:
    """Tell whether `pid` still has `path` open.

    A file the writer has not closed yet is a write still in flight, however
    long its size happens to sit still: Dolphin compresses a state in chunks
    and a busy or stalled host can leave it the same size for a full poll
    window mid-write.

    Args:
        pid: The emulator process, or None when the broker holds no handle on it.
        path: The state file being watched.

    Returns:
        True only when the descriptor is confirmed open. No pid, a `/proc` that
        cannot be read, and a process already gone all read as False, so the
        size test stays the answer where this one cannot contribute.
    """
    if pid is None:
        return False
    try:
        target = os.path.realpath(path)
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                if os.path.realpath(fd) == target:
                    return True
            except OSError:
                continue
    except OSError as exc:
        log.debug("could not read the open files of dolphin pid %s for %s: %s", pid, path, exc)
    return False


def _wait_for_state_write(
    before: dict[Path, tuple[int, float]], deadline: float, pid: Optional[int] = None
) -> bool:
    """Poll the working slot until a write completes or the deadline passes.

    A write counts as complete once the file is non-empty, Dolphin has closed
    it, and its size has been stable for 0.5 s. The hotkey is fire-and-forget,
    so the file itself is the only confirmation there is, and size alone is not
    enough of one: an emulator that stalls mid-write holds a steady size while
    the state on disk is still truncated. A target that disappears mid-write is
    dropped and the scan starts over.

    Args:
        before: Snapshot from `_snapshot` taken before the hotkey was sent.
        deadline: `time.monotonic` value to give up at.
        pid: The running dolphin process, whose open descriptors say whether the
            write has finished; None falls back to the size test alone.

    Returns:
        True once a new or modified state has settled, False on timeout.
    """
    STABLE_SECS = 0.5
    POLL_SECS = 0.1
    target: Optional[Path] = None
    last_size: Optional[int] = None
    stable_since: Optional[float] = None
    while time.monotonic() < deadline:
        after = _snapshot()
        if target is None:
            for p, (size, mtime) in after.items():
                prev = before.get(p)
                if prev is None or prev[1] != mtime:
                    target = p
                    last_size = size
                    stable_since = time.monotonic()
                    break
        else:
            cur = after.get(target)
            if cur is None:
                target = None
            else:
                if cur[0] != last_size:
                    last_size = cur[0]
                    stable_since = time.monotonic()
                elif (
                    time.monotonic() - stable_since >= STABLE_SECS
                    and last_size > 0
                    and not _holds_open(pid, target)
                ):
                    log.info("save state write complete: %s (%d bytes)", target.name, last_size)
                    return True
        time.sleep(POLL_SECS)
    if target is not None:
        log.warning(
            "save state write never settled before the deadline: %s (%s bytes, pid %s)",
            target.name,
            last_size,
            pid,
        )
    return False


def _backdate_atime(path: Path) -> Optional[float]:
    """Stamp `path`'s access time behind its own mtime and return what was written.

    Under `relatime`, the mount default, the kernel refreshes an access time
    only while it still sits at or behind the file's mtime. A state loaded
    twice in one session would already carry an atime past its mtime by the
    second load, and that read would leave no trace at all, so the stamp goes
    back behind the mtime before every load.

    Args:
        path: The state file about to be loaded.

    Returns:
        The access time stamped on, or None when it could not be read or set.
    """
    try:
        st = path.stat()
        marker = st.st_mtime - _ATIME_BACKDATE
        os.utime(path, (marker, st.st_mtime))
    except OSError as exc:
        log.warning("could not backdate the access time of the state %s: %s", path, exc)
        return None
    return marker


def _atime_tracked(dir_path: Path) -> bool:
    """Tell whether reading a file in `dir_path` moves its access time.

    A `noatime` mount records nothing, and on one of those the read a load
    makes is invisible: without this the broker would report every load as
    failed. Measured on a scratch file, because the probe's own read is the
    thing being measured and making it against the state would spend the
    backdated marker the load itself needs.

    Args:
        dir_path: The directory the state file lives in.

    Returns:
        True only when the probe's read demonstrably moved the access time.
    """
    probe = dir_path / f".atime-probe.{os.getpid()}"
    try:
        probe.write_bytes(b"probe")
        st = probe.stat()
        marker = st.st_mtime - _ATIME_BACKDATE
        os.utime(probe, (marker, st.st_mtime))
        with probe.open("rb") as fh:
            fh.read(1)
        moved = probe.stat().st_atime > marker
    except OSError as exc:
        log.warning("could not probe access-time tracking in %s: %s", dir_path, exc)
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove the access-time probe %s: %s", probe, exc)
    return moved


def _wait_for_state_read(path: Path, marker: float, deadline: float) -> bool:
    """Poll until `path`'s access time moves past `marker`, or the deadline passes.

    Args:
        path: The state file whose access time was backdated to `marker`.
        marker: The access time stamped on before the load hotkey was sent.
        deadline: A `time.monotonic()` value to give up at.

    Returns:
        True once the access time has moved past `marker`, False on timeout or
        if the file goes away first.
    """
    POLL_SECS = 0.1
    while time.monotonic() < deadline:
        try:
            if path.stat().st_atime > marker:
                return True
        except OSError as exc:
            log.warning("state %s went away while waiting for the load to read it: %s", path, exc)
            return False
        time.sleep(POLL_SECS)
    return False


def _restamp_slot(filename: str, slot: int) -> Optional[str]:
    """Rename a state for `slot`, keeping the game id that ties it to its disc.

    Dolphin resolves a state by the game id in the name, so that is what has to
    survive the trip; the slot a stored capture happens to carry is rewritten
    into this broker's one working slot.

    Args:
        filename: The basename a stored state arrived with.
        slot: The slot to stamp into the name.

    Returns:
        The same state named for `slot`, or None if `filename` is not a state name.
    """
    match = _STATE_NAME_RE.match(filename)
    if match is None:
        return None
    return f"{match.group('game')}.s{slot:02d}"


class Dolphin(Emulator):
    """GameCube and Wii sessions on dolphin-emu.

    The broker launches `dolphin-emu -b` with every setting on the command
    line: `-u` for the user directory, `-v` for the video backend, and a run
    of `-C` overrides for fullscreen, no stop confirmation, no panic dialogs,
    analytics consent already answered, and slot A pinned to a GCI folder
    card. Qt is forced onto xcb so the window lives on Xwayland, where
    xdotool can reach it. A resume whose state is already on disk, and whose
    game id matches the disc booting, loads at boot with `-s`, which is both
    more reliable than the hotkey and invisible to the player; a resume whose
    state RomM pushes after activate returns is delivered by a deferred thread
    over the load hotkey instead. Saving is hotkey only: this launch's own
    render window is activated, `SAVE_KEY` is sent through XTEST, and the state
    directory is polled until the file settles, since the hotkey gives no
    acknowledgement. A hotkey load is confirmed the other way round, off the
    access time of the state Dolphin has to read to deserialize it.

    Save data rides the save archive: `GC` holds the memory cards as loose
    `.gci` files, `Wii` the NAND, so nothing here needs the whole-card
    routes. A state is named for the game id, so pushed names are restamped
    into the broker's slot, the working slot is cleared before a boot, and
    Dolphin's undo-load buffer is dropped on exit so the archive does not
    carry a second full-size copy of a state RomM already stores.

    Attributes:
        name: RomM platform key, `dolphin`.
        display_name: Human-readable name shown in the UI.
        save_root: Dolphin's user directory, which the save subtrees hang off.
        save_subtrees: `StateSaves`, `GC` and `Wii`, the directories the save archive carries.
        rom_extensions: Bootable disc formats, best first.
        supports_states: True, states are saved over the hotkey and loaded at boot or by hotkey.
        state_slot: The one slot the broker works in, echoed back as the effective slot.
        state_dir: Where Dolphin writes `.sNN` files.
        log_path: The Dolphin log the broker exposes.
        memory_card_subtree: `GC` on a GameCube session, None on any other.
    """

    name = "dolphin"
    display_name = "Dolphin"
    save_root = USER_DIR
    save_subtrees = ("StateSaves", "GC", "Wii")
    """Directories the save archive carries.

    GC holds the memory cards, Wii the NAND. GC's card also rides the
    whole-card routes when a GameCube session opts in
    (`memory_card_subtree` below); Wii has no physical card, so both its NAND
    and any GC tree beside it only ever move through the save archive.
    """
    state_subtrees = ("StateSaves",)
    rom_extensions = ROM_EXTENSIONS
    supports_states = True
    state_slot = STATE_SLOT
    state_dir = STATE_DIR
    log_path = DOLPHIN_LOG_PATH

    def __init__(self) -> None:
        """Set up the process state and the launch sequence counter that fences deferred loads."""
        super().__init__()
        self._launch_seq = 0

    @property
    def memory_card_subtree(self) -> Optional[str]:
        """The subtree the whole-card routes carry: `GC` for GameCube, None otherwise.

        Gated on the session's platform because `memory_card_path` is: a Wii
        session has no card for those routes to carry, so naming a subtree
        here anyway would take GC out of both the archive restore and the dump
        with nothing else willing to move it.
        """
        return "GC" if self.platform == _GC_PLATFORM else None

    def memory_card_path(self, platform: Optional[str] = None) -> Optional[Path]:
        """The whole GC/ tree, or None for Wii (NAND, no physical card).

        Returns the tree rather than one region's Card A folder: Dolphin
        buckets a GCI folder card by the disc's region (GC/<region>/Card A),
        and a library can mix regions, so syncing has to carry all of them
        rather than guessing one.
        """
        return USER_DIR / "GC" if platform == _GC_PLATFORM else None

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a RomM path to the disc image to boot.

        A file is taken as is. A directory is searched one level deep for the
        best candidate by `_pick_rom_file`.

        Args:
            path: The ROM file or folder RomM handed over.

        Returns:
            The image to pass to dolphin-emu, or None when there is nothing bootable.
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
            except OSError:
                return None
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

    def _render_window(self) -> Optional[str]:
        """Find the window this launch's Dolphin renders the game into.

        Picked by title rather than by taking the first match, because the main
        window and any dialog carry the same class, and a hotkey sent at either
        of those does nothing. Then confirmed by pid: a window left behind by
        the previous emulator process carries the same class and the same kind
        of title, and would swallow the save hotkey for a session that has
        already moved on.

        Returns:
            The X window id as xdotool prints it, or None when this process has no render
            window up.
        """
        proc = self._proc
        if proc is None:
            log.warning("no dolphin process to find a render window for")
            return None
        out = self._xdotool("search", "--class", _WINDOW_CLASS)
        if out is None:
            return None
        for win_id in out.split():
            name = self._xdotool("getwindowname", win_id)
            if not name or _RENDER_TITLE_MARK not in name:
                continue
            pid = self._xdotool("getwindowpid", win_id)
            if pid is not None and pid.strip() == str(proc.pid):
                return win_id
        log.warning("no dolphin render window found for pid %s", proc.pid)
        return None

    def _send_key(self, key: str) -> bool:
        """Focus the render window and send `key` through XTEST.

        Activating first is what makes this survive the player clicking back
        into the page: XTEST delivers to whatever holds focus, so a key sent at
        an unfocused Dolphin goes to the desktop instead.

        Args:
            key: The key name in xdotool's syntax, for example `shift+F1`.

        Returns:
            True when the window was found, activated and the key sent, False otherwise.
        """
        win_id = self._render_window()
        if win_id is None:
            return False
        if self._xdotool("windowactivate", "--sync", win_id) is None:
            return False
        return self._xdotool("key", "--clearmodifiers", key) is not None

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Stop any running instance, seed the pad bindings, and start dolphin-emu.

        The binary comes from env `DOLPHIN_BIN` (default `dolphin-emu`). With
        `resume_slot` set and the working slot holding a state for this disc,
        the state is loaded at boot with `-s`; with the slot empty or holding
        another game's state, a deferred thread waits for RomM's push and
        loads it over the hotkey.

        Args:
            rom_path: The disc image to boot.
            resume_slot: Slot to resume from, or None to boot clean.
        """
        self.stop()
        _seed_gcpad()
        self._launch_seq += 1
        seq = self._launch_seq

        binary = os.environ.get("DOLPHIN_BIN", "dolphin-emu")
        env = base_launch_env()
        # Qt would pick Wayland, where the save hotkey could never be injected.
        env["QT_QPA_PLATFORM"] = "xcb"

        cmd = [
            binary,
            "-b",
            "-u", str(USER_DIR),
            "-v", VIDEO_BACKEND,
            "-C", "Dolphin.Display.Fullscreen=True",
            "-C", "Dolphin.Interface.ConfirmStop=False",
            # A modal panic dialog in a container nobody can click is a hang.
            "-C", "Dolphin.Interface.UsePanicHandlers=False",
            # First run otherwise opens a consent dialog that takes focus off
            # the render window, which is where the save hotkey has to land.
            "-C", "Dolphin.Analytics.PermissionAsked=True",
            "-C", "Dolphin.Analytics.Enabled=False",
            # GCI folder, which is also Dolphin's own default. Pinned because
            # the card sync has to know which layout it is reading, and a
            # default that moved would silently change where saves live.
            "-C", f"Dolphin.Core.SlotA={GC_SLOT_A_DEVICE}",
        ]

        # A state already on disk (restored from the save archive) loads at boot,
        # which is both more reliable than the hotkey and invisible to the player.
        resume_path = _resume_state(rom_path) if resume_slot is not None else None
        if resume_path is not None:
            cmd += ["-s", str(resume_path)]

        cmd += ["-e", str(rom_path)]
        log.info("launching dolphin (rom=%s, resume_slot=%s)", rom_path, resume_slot)
        self._spawn(cmd, env)

        # RomM pushes its resume pick after activate returns, so a slot that was
        # empty at launch can still fill. That one has to go in over the hotkey.
        if resume_slot is not None and resume_path is None:
            Thread(
                target=self._deferred_load_state, args=(seq, rom_path), daemon=True
            ).start()

    def _deferred_load_state(self, seq: int, rom_path: Path) -> None:
        """Wait for a pushed state to arrive, then load it over the hotkey.

        Gives the file `RESUME_LOAD_WAIT` to appear, then `RESUME_LOAD_SETTLE`
        for the window to be ready. The state has to belong to the ROM that
        booted, the same test the `-s` resume makes, since this path also runs
        for a launch whose slot held another game's state. Abandons itself
        whenever `seq` no longer matches the current launch, so a superseded
        launch never gets a stray load.

        Args:
            seq: The launch sequence number this load belongs to.
            rom_path: The image that booted, whose game id the state must carry.
        """
        deadline = time.monotonic() + RESUME_LOAD_WAIT
        if not self.wait_for_state(deadline):
            log.warning("resume: no state file ever arrived")
            return
        if self._launch_seq != seq:
            log.info("resume: launch superseded, load abandoned")
            return
        time.sleep(RESUME_LOAD_SETTLE)
        if self._launch_seq != seq:
            return
        if _resume_state(rom_path) is None:
            log.warning(
                "resume: the working slot holds no state for %s, no load sent", rom_path.name
            )
            return
        ok = self.load_state(STATE_SLOT)
        log.info("resume: deferred load %s", "delivered" if ok else "failed")

    def save_state(self, slot: int) -> bool:
        """Save a state into the broker's slot over the hotkey and wait for it to land.

        `slot` is what RomM asked for and is ignored: this saves into
        `STATE_SLOT` and the caller reads the effective slot back off
        `state_slot`.

        Args:
            slot: The slot RomM requested; not used.

        Returns:
            True once the state file has been written and settled within `STATE_WAIT`, False if
            the hotkey could not be sent or the write never completed.
        """
        before = _snapshot()
        if not self._send_key(SAVE_KEY):
            return False
        pid = self._proc.pid if self._proc is not None else None
        return _wait_for_state_write(before, time.monotonic() + STATE_WAIT, pid)

    def load_state(self, slot: int) -> bool:
        """Load the broker's slot over the hotkey and confirm Dolphin read the state back.

        The hotkey is silent on an empty slot, so an absent file has to be
        caught here or the caller reads a no-op as success. A hotkey that was
        sent is no proof either: Dolphin drops one that lands before the core
        is running and says nothing about it, so the state's access time is
        backdated first and the load only counts once a read has moved it.
        The deserialize that read starts is unobservable, and `LOAD_SETTLE`
        covers it rather than handing the caller a session mid-restore.

        Args:
            slot: The slot RomM requested; the broker's `STATE_SLOT` is what gets loaded.

        Returns:
            True once the state has been read back and settled, False when the slot is empty,
            the hotkey could not be sent, or nothing read the state within `LOAD_WAIT`.
        """
        state = self.state_path()
        if state is None:
            log.warning("load state: slot %d holds no state file", STATE_SLOT)
            return False
        marker = _backdate_atime(state) if _atime_tracked(STATE_DIR) else None
        if not self._send_key(LOAD_KEY):
            log.warning("load state: could not send the load hotkey for %s", state.name)
            return False
        if marker is None:
            log.warning(
                "load state: %s cannot be confirmed, nothing tracks access times under %s",
                state.name,
                STATE_DIR,
            )
            time.sleep(LOAD_SETTLE)
            return True
        if not _wait_for_state_read(state, marker, time.monotonic() + LOAD_WAIT):
            log.warning(
                "load state: dolphin never read %s back within %.1fs", state.name, LOAD_WAIT
            )
            return False
        time.sleep(LOAD_SETTLE)
        log.info("load state: %s read back and settled", state.name)
        return True

    def state_path(self) -> Optional[Path]:
        """Return the newest state file in the broker's slot, or None when it holds nothing."""
        return _state_for_slot(STATE_SLOT)

    def clear_working_slot(self) -> None:
        """Delete every state in the broker's slot before a new session boots.

        A state is named for the game it was taken from, and the game id only
        comes off the running disc, so a leftover cannot be told apart from the
        state of the game about to boot. Anything still here belongs to a
        session that has already exited and whose states RomM holds.
        """
        if not STATE_DIR.is_dir():
            return
        for stale in STATE_DIR.glob(f"*.s{STATE_SLOT:02d}"):
            try:
                stale.unlink()
                log.info("cleared stale state %s", stale.name)
            except OSError as exc:
                log.warning("could not clear stale state %s: %s", stale.name, exc)

    def state_target(self, filename: str) -> Optional[Path]:
        """Map a pushed state's filename to where it may be written.

        With the slot already holding a state, a pushed name has to match it;
        otherwise the game id is taken on trust, bounded to a `<game>.s<slot>`
        basename in the state dir.

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

    def _drop_undo_buffer(self) -> None:
        """Delete the undo-load-state buffer before the save archive is built.

        Dolphin rewrites this on every state load, so it lands in the dump as a
        second full-size copy of a state RomM is already storing, for the sake
        of an undo hotkey a streaming session has no way to press. A failure
        to remove it is logged, not raised.
        """
        undo = STATE_DIR / "lastState.sav"
        try:
            undo.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not drop the undo state buffer: %s", exc)

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Save a state if asked, stop the emulator, and drop the undo buffer.

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
        self._drop_undo_buffer()
        return {
            "state_saved": saved,
            "state_slot": STATE_SLOT if slot is not None else None,
            "state_file": state_file,
        }

    def stop(self) -> None:
        """Invalidate any in-flight deferred state load before the kill."""
        self._launch_seq += 1
        super().stop()
