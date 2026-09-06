"""Eden (Nintendo Switch) launcher: ROM resolution, qt-config.ini patching, and SIGTERM shutdown.

Eden has no save states and no external control API. Persistence is the
game's own save data, which the emulated game commits directly to host files
under the virtual NAND (`nand/user/save/...`). Save paths are keyed by the
Switch profile UUID, so the profile store (`nand/system/save/8000000000000010`)
ships with the saves; that way a save archive restored into a fresh
container brings its matching profile along and the paths line up. Exit
restamps both so the delta dump takes each save unit whole rather than the
few files the game happened to rewrite (see `Eden.save_and_exit`).

Shutdown: Eden's Qt frontend routes SIGTERM through the event loop into a
normal window close (graceful emulation teardown). SIGINT is `_exit(1)` in
Eden, so the broker never sends it. The close path pops a confirmation
dialog unless the `confirmStop` UI setting is Ask_Never, so that is patched
before every launch.
"""

import logging
import os
import re
import shutil
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Library root a resolved ROM must live under (env `ROM_ROOT`, default `/romm`)."""

CONFIG_DIR = Path(os.environ.get("EDEN_CONFIG_DIR", "/config/.config/eden"))
"""Eden's config directory (env `EDEN_CONFIG_DIR`, default `/config/.config/eden`)."""
DATA_DIR = Path(os.environ.get("EDEN_DATA_DIR", "/config/.local/share/eden"))
"""Eden's data directory holding the virtual NAND (env `EDEN_DATA_DIR`)."""
INI_PATH = CONFIG_DIR / "qt-config.ini"
"""The qt-config.ini patched before every launch."""
EDEN_LOG_PATH = Path(os.environ.get("EDEN_LOG_PATH", "/config/eden.log"))
"""The emulator log file (env `EDEN_LOG_PATH`, default `/config/eden.log`)."""

SAVE_DIR = DATA_DIR / "nand" / "user" / "save"
"""The virtual NAND's user save tree, laid out `<save space id>/<user id>/<title id>`.

Account saves key the middle level by profile UUID; device saves use an
all-zero one, and cache and bcat storage use their own names there. Every
kind ends in a title id directory, which is the unit a save is dumped as.
"""
PROFILE_STORE_DIR = DATA_DIR / "nand" / "system" / "save" / "8000000000000010"
"""The system save holding the Switch profile list."""

_SAVE_UNIT_DEPTH = 3
"""Directory levels between `SAVE_DIR` and a title id directory."""
_TITLE_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
"""Matches a title id: the leaf of a save unit path, 16 hex digits."""

ROM_EXTENSIONS = (".xci", ".nsp", ".nca", ".nro")
"""Formats Eden's loader boots directly, best first; a folder holding several picks by this order."""
_ROM_SEARCH_GLOBS = ("*", "*/*")
"""Glob patterns a ROM folder is searched with, one level of wrapper folder deep."""
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)
"""Matches update and DLC names: they sit beside base games in library folders, and the base game boots."""

_INI_PATCHES: dict[tuple[str, str], str] = {
    ("UI", "confirmStop\\default"): "confirmStop\\default = false",
    ("UI", "confirmStop"): "confirmStop = 2",
}
"""qt-config.ini lines forced before every launch, keyed `(section, key)`.

Eden serializes enum settings as their underlying integer;
ConfirmStop::Ask_Never is 2. The `\\default` flag must be false or the
stored value is ignored in favor of the built-in default (Ask_Always),
which pops a confirmation dialog on close and would hang a headless
SIGTERM shutdown.
"""


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Optional[Path]:
    """Pick the best bootable file among `candidates`.

    Hidden files, non-files and anything resolving outside `ROM_ROOT` are
    skipped. Ranking prefers base games over updates and DLC, then the
    `ROM_EXTENSIONS` order, then the shallowest path, then the lowercased name.

    Args:
        candidates: Paths found under `base` by the search globs.
        base: The directory the candidates were searched from.

    Returns:
        The resolved path of the best candidate, or None when nothing qualifies.
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
        is_addon = 1 if _ADDON_RE.search(str(rel)) else 0
        ranked.append(
            (is_addon, ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


def _patch_ini() -> None:
    """Force broker-required qt-config.ini settings before every launch.

    A missing file is seeded with just the patched section and Eden fills in
    the rest. Otherwise the file is patched line-wise so every other setting
    survives untouched.

    Raises:
        OSError: When the file cannot be read, written or replaced.
        UnicodeDecodeError: When the existing file is not decodable text.

    Both are fatal to a launch rather than a warning to launch past: an
    unpatched config leaves `confirmStop` on Ask_Always, and the modal that
    then answers SIGTERM holds the shutdown until the SIGKILL escalation cuts
    a running game off mid-save.
    """
    try:
        if not INI_PATH.exists():
            # First run: write a minimal config, Eden fills in the rest.
            INI_PATH.parent.mkdir(parents=True, exist_ok=True)
            INI_PATH.write_text(
                "[UI]\n" + "\n".join(_INI_PATCHES.values()) + "\n"
            )
            return
        lines = INI_PATH.read_text().splitlines()
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
            for (sec, key), val in _INI_PATCHES.items():
                if section != sec:
                    continue
                if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
                    new_lines.append(val)
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        missing = [(s, k, v) for (s, k), v in _INI_PATCHES.items() if (s, k) not in applied]
        if missing:
            present = {
                ln.strip()[1:-1]
                for ln in new_lines
                if ln.strip().startswith("[") and ln.strip().endswith("]")
            }
            for sec, _key, val in missing:
                if sec in present:
                    out: list[str] = []
                    inserted = False
                    for ln in new_lines:
                        out.append(ln)
                        if not inserted and ln.strip() == f"[{sec}]":
                            out.append(val)
                            inserted = True
                    new_lines = out
                else:
                    new_lines.extend(["", f"[{sec}]", val])
                    present.add(sec)
        tmp = INI_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(INI_PATH)
    except (OSError, UnicodeDecodeError):
        log.exception("eden: qt-config.ini patch failed at %s, refusing to launch", INI_PATH)
        raise


def _restamp_tree(root: Path, when: float) -> None:
    """Set every file under `root` to `when` so the delta dump ships it whole.

    A file that cannot be walked or stamped is logged and stepped over: the
    exit path calling this still has a report to hand back, and the rest of
    the save unit still belongs in the archive.

    Args:
        root: A directory to walk, or a single file to stamp.
        when: The mtime and atime to write, as unix time.
    """
    try:
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = [p for p in root.rglob("*") if p.is_file()]
        else:
            return
    except OSError as exc:
        log.warning("eden: could not walk %s, its saves may be dropped from the dump: %s", root, exc)
        return
    for p in files:
        try:
            os.utime(p, (when, when))
        except OSError as exc:
            log.warning("eden: could not restamp %s, it may be dropped from the dump: %s", p, exc)


def _empty_save_tree(root: Path) -> int:
    """Remove everything `root` holds, or `root` itself when it is a file.

    The directory itself stays: Eden derives the rest of the NAND layout from
    it, and a launch is free to recreate whatever it needs underneath. A
    profile store laid down as a single file has no children to drop, so that
    one is unlinked whole.

    Anything that cannot be listed or removed is logged and stepped over, so a
    single unremovable leftover does not abort an activate that still has the
    rest of the tree to clear.

    Args:
        root: A save subtree to empty.

    Returns:
        The number of entries removed.
    """
    try:
        if root.is_symlink() or root.is_file():
            root.unlink()
            log.debug("eden: cleared stale save data %s", root)
            return 1
        if not root.is_dir():
            return 0
        entries = list(root.iterdir())
    except OSError as exc:
        log.warning("eden: could not clear stale save data %s: %s", root, exc)
        return 0
    cleared = 0
    for entry in entries:
        try:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                shutil.rmtree(entry)
        except OSError as exc:
            log.warning("eden: could not clear stale save data %s: %s", entry, exc)
            continue
        cleared += 1
        log.debug("eden: cleared stale save data %s", entry)
    return cleared


def _clear_stale_save_data() -> None:
    """Empty the NAND save trees before an archive restore.

    A restore only writes the members the incoming archive names, so an
    earlier session's save units otherwise survive into this one: readable by
    the next player, and swept into that player's exit dump the moment a game
    writes one file into a directory the restamp then ships whole.

    Every title goes, not just the incoming one: a save unit named for another
    title holds another player's data just the same, and `_session_save_dirs`
    selects by mtime alone. The profile store goes with them, since Switch save
    paths key on the profile UUID and a leftover profile would leave the next
    player's restored saves resolving through the last player's identity; the
    archive carries the matching profile store back, and a session with no
    archive starts from the profile Eden seeds on first boot.

    The clear stops at the two save subtrees: installed titles, ROMs and
    config live elsewhere under the data root and are not save data.
    """
    cleared = 0
    for root in (SAVE_DIR, PROFILE_STORE_DIR):
        cleared += _empty_save_tree(root)
    if cleared:
        log.info("eden: cleared %d stale save entries before the restore", cleared)


class Eden(Emulator):
    """Nintendo Switch via Eden, driven by command line flags and a graceful SIGTERM.

    Eden has no external control API, so the broker patches qt-config.ini
    before every launch (close confirmation off) and boots fullscreen with
    `-f -g`. A patch that fails aborts the launch: the close confirmation is
    what the whole shutdown path rests on. SIGTERM goes through Eden's Qt
    event loop into a normal window close, so the stop is a graceful emulation
    teardown; SIGINT is never used because Eden maps it to `_exit(1)`.

    There are no save states: persistence is the game's own save data under
    the virtual NAND, and the archive carries it together with the profile
    store, since Switch save paths embed the profile UUID. At exit every file
    of a title whose save was written this session gets its mtime refreshed,
    the profile store with it, so the delta dump ships those saves whole while
    other titles' saves stay filtered out. A resume slot is logged and ignored.

    Attributes:
        name: Provider key, `eden`.
        display_name: Human-readable name.
        save_root: The data directory the save subtrees hang off.
        save_subtrees: Game saves plus the profile store.
        clears_stale_saves: On; activate empties both save subtrees.
        rom_extensions: Bootable formats, best first.
        log_path: The emulator log file.
        term_timeout: SIGTERM grace before SIGKILL (env `EDEN_STOP_WAIT`, default 15).
    """

    name = "eden"
    display_name = "Eden"
    save_root = DATA_DIR
    save_subtrees = ("nand/user/save", "nand/system/save/8000000000000010")
    """Game saves plus the profile store.

    Switch save paths embed the profile UUID, so the two must travel
    together for restored saves to resolve.
    """
    clears_stale_saves = True
    """On: `clear_working_slot` empties the user save tree and the profile store.

    Both go whole. Nothing under them is the game itself (installed titles sit
    outside the save subtrees), and no leftover save unit can be told apart
    from the incoming player's own by anything the broker sees.
    """
    rom_extensions = ROM_EXTENSIONS
    log_path = EDEN_LOG_PATH
    term_timeout = float(os.environ.get("EDEN_STOP_WAIT", "15"))
    """SIGTERM grace before SIGKILL (env `EDEN_STOP_WAIT`, default 15).

    A running game takes longer than the base 5 s to tear down gracefully;
    give SIGTERM room before escalating to SIGKILL.
    """

    def __init__(self) -> None:
        """Initialise the process handle and the session baseline."""
        super().__init__()
        self._session_start = float("inf")
        """Unix time `launch` started Eden at; infinity until it does.

        Infinity rather than zero so an instance that never launched matches
        no file at all. Zero is newer than nothing, so every title in the
        container would read as written this session and `save_and_exit`
        would restamp and ship all of them.
        """

    def clear_working_slot(self) -> None:
        """Drop the previous session's saves and profile before the restore.

        Eden has no save states and no fixed slot, so the whole clear is the
        save data (`_clear_stale_save_data`). It has to happen here rather than
        at exit: a session that crashes or is killed never reaches
        `save_and_exit`, and the exit restamp ships a save unit whole, so a
        leftover file in one would leave in the next player's archive.
        """
        _clear_stale_save_data()

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The file Eden should boot for `path`.

        Args:
            path: A ROM file, or a folder searched up to two levels deep.

        Returns:
            The file itself, the best-ranked bootable file in the folder, or None.
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
                # One unreadable subdirectory must not discard what the other
                # patterns already found and report the title as unbootable.
                log.warning("eden: search of %s for %s failed: %s", path, pattern, exc)
        return _pick_rom_file(candidates, path)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Patch qt-config.ini and boot the game fullscreen.

        Args:
            rom_path: The file to boot.
            resume_slot: Ignored with a log line; Eden has no save states.

        Raises:
            OSError: When qt-config.ini could not be patched, so nothing is
                spawned; see `_patch_ini`.
            UnicodeDecodeError: Likewise, for an undecodable existing file.
        """
        self.stop()
        _patch_ini()
        if resume_slot is not None:
            log.info(
                "eden has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        self._session_start = time.time()
        binary = os.environ.get("EDEN_BIN", "eden")
        log.info("launching eden (rom=%s)", rom_path)
        self._spawn([binary, "-f", "-g", str(rom_path)], base_launch_env())

    def _session_save_dirs(self) -> list[Path]:
        """Title save directories holding a file written while the session ran.

        A level that fails to list is logged and skipped rather than raised:
        the NAND can vanish under the walk, and the exit path calling this
        still has a process to stop and a report to hand back.

        Only `<save space id>/<user id>/<title id>` leaves count. Anything
        whose leaf is not a title id is not a save unit, and handing an
        arbitrary directory to the restamp walk would ship whatever else the
        NAND keeps there.

        Returns:
            The title directories touched since launch, or nothing at all when
            no launch set a baseline.
        """
        level = [SAVE_DIR] if SAVE_DIR.is_dir() else []
        for _ in range(_SAVE_UNIT_DEPTH):
            children: list[Path] = []
            for parent in level:
                try:
                    children.extend(p for p in sorted(parent.iterdir()) if p.is_dir())
                except OSError as exc:
                    log.warning(
                        "eden: could not list %s, saves under it may be dropped from the dump: %s",
                        parent,
                        exc,
                    )
            level = children
        selected: list[Path] = []
        for title in level:
            if not _TITLE_ID_RE.match(title.name):
                continue
            try:
                if any(
                    p.is_file() and p.stat().st_mtime >= self._session_start
                    for p in title.rglob("*")
                ):
                    selected.append(title)
            except OSError as exc:
                log.warning(
                    "eden: could not walk %s, its saves may be dropped from the dump: %s",
                    title,
                    exc,
                )
        return selected

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Stop Eden and mark this session's saves and profile for the dump.

        The dump ships files newer than the session baseline. A Switch save is
        a directory the game rewrites only partially, and the profile store
        the save paths are keyed by is not rewritten by a game session at all,
        so both are restamped: a restored archive then carries the whole save
        unit and the profile its path resolves through, instead of the
        handful of files the game happened to write.

        Args:
            slot: Ignored; Eden has no save states.

        Returns:
            `state_saved`, `state_slot` and `state_file`, all None.
        """
        self.stop()
        now = time.time()
        touched = self._session_save_dirs()
        for d in touched:
            _restamp_tree(d, now)
        if touched:
            _restamp_tree(PROFILE_STORE_DIR, now)
        log.info("eden: exit restamped %d save dir(s) for the dump", len(touched))
        return {"state_saved": None, "state_slot": None, "state_file": None}
