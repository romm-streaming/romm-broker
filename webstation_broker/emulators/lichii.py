"""Lichii (Nintendo Switch and Switch 2) launcher: ROM resolution, config patching, SIGTERM shutdown.

Lichii is a fork of Eden, and this module copies `eden` rather than sharing
code with it, so a change here cannot move Eden. The two keep separate
config, data and save trees (`lichii/` beside `eden/`) and separate archives.
Lichii is experimental: the image pins one build, and the behaviour below was
read from that build's source (tag build-341).

Lichii has no save states and no external control API. Persistence is the
game's own save data, which the emulated game commits directly to host files
under the virtual NAND (`nand/user/save/...`). Save paths are keyed by the
Switch profile UUID, so the profile store (`nand/system/save/8000000000000010`)
ships with the saves; that way a save archive restored into a fresh
container brings its matching profile along and the paths line up. Exit
restamps both so the delta dump takes each save unit whole rather than the
few files the game happened to rewrite (see `Lichii.save_and_exit`).

Declared imports (`import_spec`, `place_import`, `validate_import_plan`)
take a NAND export as it sat: a save unit at `nand/user/save/<space>/<user>/<title>`
and, for an account save, the profile store beside it. See
docs/content/docs/api/imports.mdx for the shapes.

Shutdown: Lichii's Qt frontend routes SIGTERM through the event loop into a
normal window close (graceful emulation teardown). SIGINT is `_exit(1)` in
Lichii, so the broker never sends it. The close path pops a confirmation
dialog unless the `confirmStop` UI setting is Ask_Never, so that is patched
before every launch.

First launch: Lichii offers to import a legacy profile (Eden, Citron, Sudachi,
Suyu or yuzu) whenever one of their data directories exists beside its own and
it has no import decision on record. In the webstation Eden's always does once
Eden has run, and the prompt is a modal no one in a streamed session can
answer. So every launch first records "start fresh" the way Lichii's own
button does (`_mark_import_fresh`). Eden's data is never imported.
"""

import functools
import logging
import os
import re
import shutil
import time
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Union

from .. import imports
from .base import Emulator, base_launch_env, xdg_config_dir, xdg_data_dir

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Library root a resolved ROM must live under (env `ROM_ROOT`, default `/romm`)."""

CONFIG_DIR = xdg_config_dir("lichii")
"""Lichii's config directory, holding the qt-config.ini the broker patches.

Not configurable, and deliberately: nothing on Lichii's command line names it, so
an override would move only the copy the broker writes and leave Lichii reading
its own. `launch` exports the root this resolved to instead.
"""
DATA_DIR = xdg_data_dir("lichii")
"""Lichii's data directory holding the virtual NAND, which is also the save root.

Not configurable, for the same reason as `CONFIG_DIR`, and with more at stake:
an override here would point the save dump and restore at a tree Lichii does not
write to, which loses saves without reporting anything.
"""
INI_PATH = CONFIG_DIR / "qt-config.ini"
"""The qt-config.ini patched before every launch."""
IMPORT_STATE_DIR = DATA_DIR.parent / "lichii.migration"
"""Where Lichii records its legacy profile import decision, beside its data directory.

Lichii names it `<data dir>.migration`. A `complete` or `fresh` file there means
the decision is made; `pending` or `journal` means an import was interrupted.
"""
LICHII_LOG_PATH = Path(os.environ.get("LICHII_LOG_PATH", "/config/lichii.log"))
"""The emulator log file (env `LICHII_LOG_PATH`, default `/config/lichii.log`)."""

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
_SAVE_SUBTREE = "nand/user/save"
"""The user save tree relative to `DATA_DIR`; the first of `Lichii.save_subtrees`."""
_PROFILE_SUBTREE = "nand/system/save/8000000000000010"
"""The profile store relative to `DATA_DIR`; the second of `Lichii.save_subtrees`."""
_ZERO_USER = "0" * 32
"""The user id of a device save, which belongs to no profile."""
_RAW_SAVE_LIMIT = 64 * 1024 * 1024
"""Largest `.bin` taken for a save; a bigger one is a raw hardware dump, not a save."""
_SAVE_EXPECTED = "nand/user/save/<space>/<user>/<title id>/<tail>"
"""The shape of a save file, for refusals."""
_PROFILE_EXPECTED = "nand/system/save/8000000000000010/<tail>"
"""The shape of a profile store file, for refusals."""
_EXPECTED = f"{_SAVE_EXPECTED}, with {_PROFILE_EXPECTED} for an account save"
"""The shapes an import member is asked to take, for refusals."""
_SAVE_WRAPPER = ("nand", "user", "save")
"""The verbatim NAND folders above a save space."""
_PROFILE_WRAPPER = ("nand", "system", "save", "8000000000000010")
"""The verbatim NAND folders down to the profile store."""
_HEX16_LEVEL = re.compile(r"[0-9A-Fa-f]{16}", re.ASCII)
"""A save space id or a title id, as a folder name."""
_USER_LEVEL = re.compile(r"[0-9A-Fa-f]{32}", re.ASCII)
"""A user id, as a folder name."""
_LOOSE_TITLE_RE = re.compile(r"(?:0[xX])?[0-9A-Fa-f]{16}", re.ASCII)
"""A title id at the head of a path that says nothing of the space or profile it sat in."""
_SD_ROOTS = ("Nintendo", "sdmc")
"""Top folders of an SD card's own layout, which Lichii does not read."""
_HOMEBREW_ROOTS = (("JKSV",), ("Checkpoint",), ("switch", "JKSV"), ("switch", "Checkpoint"))
"""Where the homebrew save managers put a backup; none of them records the space or profile."""

ROM_EXTENSIONS = (".xci", ".nsp", ".nca", ".nro")
"""Formats Lichii's loader boots directly, best first; a folder holding several picks by this order."""
_ROM_SEARCH_GLOBS = ("*", "*/*")
"""Glob patterns a ROM folder is searched with, one level of wrapper folder deep."""
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)
"""Matches update and DLC names: they sit beside base games in library folders, and the base game boots."""

_INI_PATCHES: dict[tuple[str, str], str] = {
    ("UI", "confirmStop\\default"): "confirmStop\\default = false",
    ("UI", "confirmStop"): "confirmStop = 2",
    ("UI", "gui_hide_backend_warning\\default"): "gui_hide_backend_warning\\default = false",
    ("UI", "gui_hide_backend_warning"): "gui_hide_backend_warning = true",
}
"""qt-config.ini lines forced before every launch, keyed `(section, key)`.

Lichii serializes enum settings as their underlying integer;
ConfirmStop::Ask_Never is 2. The `\\default` flag must be false or the
stored value is ignored in favor of the built-in default (Ask_Always),
which pops a confirmation dialog on close and would hang a headless
SIGTERM shutdown.

`gui_hide_backend_warning` silences the "Wayland Detected! Use X11 /
Continue with Wayland" modal `MainWindow::OnCheckGraphicsBackend` opens on a
fresh profile. Left up, it blocks the boot (the game never starts) and holds
off SIGTERM until the SIGKILL escalation, the same hang as `confirmStop`.
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
        except (OSError, ValueError) as exc:
            log.debug("lichii: skipping candidate %s: %s", p, exc)
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

    A missing file is seeded with just the patched section and Lichii fills in
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
            # First run: write a minimal config, Lichii fills in the rest.
            INI_PATH.parent.mkdir(parents=True, exist_ok=True)
            INI_PATH.write_text(
                "[UI]\n" + "\n".join(_INI_PATCHES.values()) + "\n"
            )
            log.debug("lichii: seeded a new %s", INI_PATH)
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
        log.debug("lichii: patched %s", INI_PATH)
    except (OSError, UnicodeDecodeError):
        log.exception("lichii: qt-config.ini patch failed at %s, refusing to launch", INI_PATH)
        raise


class ImportPendingError(RuntimeError):
    """Lichii has an interrupted profile import on record, so the broker will not launch it."""


def _mark_import_fresh() -> None:
    """Record "start fresh" for Lichii's legacy profile import, unless a decision exists.

    Lichii checks `IMPORT_STATE_DIR` at startup: `complete` or `fresh` means no
    prompt; `pending` or `journal` means an import stopped part way and must be
    recovered; with none of them it prompts as soon as any legacy emulator's
    data directory exists, Eden's included. Writing an empty `fresh` file is
    what Lichii's own "Start fresh" button does.

    An interrupted import is refused rather than cleared: its journal is what
    Lichii recovers the half-moved files from, and it is not the broker's to
    discard. Lichii would prompt for it anyway, with no one to answer.

    Raises:
        ImportPendingError: When `pending` or `journal` exists.
        OSError: When the state directory cannot be read or the marker written.
            Fatal to the launch like a failed `_patch_ini`: without the marker
            Lichii opens a modal that holds the session on a dialog.
    """
    try:
        if (IMPORT_STATE_DIR / "pending").exists() or (IMPORT_STATE_DIR / "journal").exists():
            log.error(
                "lichii: an interrupted profile import is on record in %s, refusing to launch; "
                "recover or discard it from the Lichii GUI in desktop mode",
                IMPORT_STATE_DIR,
            )
            raise ImportPendingError(f"interrupted Lichii profile import in {IMPORT_STATE_DIR}")
        if (IMPORT_STATE_DIR / "complete").exists() or (IMPORT_STATE_DIR / "fresh").exists():
            return
        IMPORT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        (IMPORT_STATE_DIR / "fresh").touch()
        log.info("lichii: recorded a fresh start in %s, no legacy profile import", IMPORT_STATE_DIR)
    except OSError:
        log.exception(
            "lichii: could not record the import decision in %s, refusing to launch", IMPORT_STATE_DIR
        )
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
        log.warning("lichii: could not walk %s, its saves may be dropped from the dump: %s", root, exc)
        return
    for p in files:
        try:
            os.utime(p, (when, when))
        except OSError as exc:
            log.warning("lichii: could not restamp %s, it may be dropped from the dump: %s", p, exc)


def _empty_save_tree(root: Path) -> int:
    """Remove everything `root` holds, or `root` itself when it is a file.

    The directory itself stays: Lichii derives the rest of the NAND layout from
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
            log.debug("lichii: cleared stale save data %s", root)
            return 1
        if not root.is_dir():
            return 0
        entries = list(root.iterdir())
    except OSError as exc:
        log.warning("lichii: could not clear stale save data %s: %s", root, exc)
        return 0
    cleared = 0
    for entry in entries:
        try:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                shutil.rmtree(entry)
        except OSError as exc:
            log.warning("lichii: could not clear stale save data %s: %s", entry, exc)
            continue
        cleared += 1
        log.debug("lichii: cleared stale save data %s", entry)
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
    archive starts from the profile Lichii seeds on first boot.

    The clear stops at the two save subtrees: installed titles, ROMs and
    config live elsewhere under the data root and are not save data.
    """
    cleared = 0
    for root in (SAVE_DIR, PROFILE_STORE_DIR):
        cleared += _empty_save_tree(root)
    if cleared:
        log.info("lichii: cleared %d stale save entries before the restore", cleared)


_refuse = functools.partial(imports.refuse, expected=_EXPECTED)
"""Refuse a member, naming this emulator's accepted shapes (see `imports.refuse`)."""


def _unmatched(member: imports.ImportMember) -> imports.ImportRefusal:
    """Refuse a member that is not a NAND export, saying which kind of not it is.

    Args:
        member: The member.

    Returns:
        `source_incompatible` for a raw dump, `destination_unresolvable` for a
        save that lacks the space, profile or title the destination is keyed
        by, and `unrecognised_layout` for anything else.
    """
    parts = member.parts
    if parts[-1].lower().endswith(".bin") and member.size > _RAW_SAVE_LIMIT:
        return _refuse(member, "source_incompatible", "a raw dump, not a save; export the NAND save folder")
    if _LOOSE_TITLE_RE.fullmatch(parts[0]) or any(parts[: len(root)] == root for root in _HOMEBREW_ROOTS):
        return _refuse(
            member,
            "destination_unresolvable",
            "the save space and profile are not in the path; send the NAND export",
        )
    return _refuse(member, "unrecognised_layout", "not a file of a NAND save export")


def _place_save(
    member: imports.ImportMember, session: imports.SessionIdentity
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place a file of a NAND export, or of the profile store, where it sat.

    A save unit is keyed by its own title id, so it is held strictly to the
    session's game. The profile store names no title. Ids are upper-cased, the
    way Lichii writes them.

    Args:
        member: The member.
        session: The session's identity.

    Returns:
        The placement, or a refusal.
    """
    parts = member.parts
    if parts[0] == "bis":
        return _refuse(member, "source_incompatible", "a BIS partition dump from a console")
    if parts[0] in _SD_ROOTS:
        return _refuse(member, "source_incompatible", "an SD card's own layout")
    found = imports.match_anchored(
        parts, wrappers=(_SAVE_WRAPPER,), levels=(_HEX16_LEVEL, _USER_LEVEL, _HEX16_LEVEL)
    )
    if found is not None:
        refusal = imports.check_member_identity(
            member,
            imports.NORMALISERS["hex16"](found.ids[2]),
            session,
            family="hex16",
            policy="strict",
            expected=_EXPECTED,
        )
        if refusal is not None:
            return refusal
        dest = imports.build_dest(
            _SAVE_SUBTREE,
            tuple(part.upper() for part in found.ids),
            found.tail,
            member=member,
            expected=_EXPECTED,
        )
    else:
        store = imports.match_anchored(parts, wrappers=(_PROFILE_WRAPPER,), levels=())
        if store is None:
            return _unmatched(member)
        dest = imports.build_dest(_PROFILE_SUBTREE, (), store.tail, member=member, expected=_EXPECTED)
    if isinstance(dest, imports.ImportRefusal):
        return dest
    return imports.Placement(member, dest)


def _is_profile_dest(parts: tuple[str, ...]) -> bool:
    """Whether a destination is a file of the profile store.

    Args:
        parts: The destination's components, relative to `DATA_DIR`.

    Returns:
        True when it sits under `nand/system/save/8000000000000010`.
    """
    return parts[: len(_PROFILE_WRAPPER)] == _PROFILE_WRAPPER


def _is_account_dest(parts: tuple[str, ...]) -> bool:
    """Whether a destination is a file of a save that belongs to a profile.

    Args:
        parts: The destination's components, relative to `DATA_DIR`.

    Returns:
        True when it sits under `nand/user/save/<space>/<user>` with a user id
        that is not the all-zero device one.
    """
    return parts[: len(_SAVE_WRAPPER)] == _SAVE_WRAPPER and len(parts) > 4 and parts[4] != _ZERO_USER


class Lichii(Emulator):
    """Nintendo Switch and Switch 2 via Lichii, driven by command line flags and a graceful SIGTERM.

    Lichii has no external control API, so the broker patches qt-config.ini
    before every launch (close confirmation off), records a fresh start for
    the legacy profile import, and boots fullscreen with `-f -g`. Either
    step failing aborts the launch: the close confirmation is what the whole
    shutdown path rests on, and the import prompt would hold the session on a
    modal. SIGTERM goes through Lichii's Qt
    event loop into a normal window close, so the stop is a graceful emulation
    teardown; SIGINT is never used because Lichii maps it to `_exit(1)`.

    There are no save states: persistence is the game's own save data under
    the virtual NAND, and the archive carries it together with the profile
    store, since Switch save paths embed the profile UUID. At exit every file
    of a title whose save was written this session gets its mtime refreshed,
    the profile store with it, so the delta dump ships those saves whole while
    other titles' saves stay filtered out. A resume slot is logged and ignored.

    Attributes:
        name: Provider key, `lichii`.
        display_name: Human-readable name.
        rom_cacheable: On; with the ROM cache enabled, launch boots a local copy of the ROM.
        save_root: The data directory the save subtrees hang off.
        save_subtrees: Game saves plus the profile store.
        clears_stale_saves: On; activate empties both save subtrees.
        rom_extensions: Bootable formats, best first.
        log_path: The emulator log file.
        term_timeout: SIGTERM grace before SIGKILL (env `LICHII_STOP_WAIT`, default 15).
    """

    name = "lichii"
    rom_cacheable = True
    display_name = "Lichii"
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
    log_path = LICHII_LOG_PATH
    term_timeout = float(os.environ.get("LICHII_STOP_WAIT", "15"))
    """SIGTERM grace before SIGKILL (env `LICHII_STOP_WAIT`, default 15).

    A running game takes longer than the base 5 s to tear down gracefully;
    give SIGTERM room before escalating to SIGKILL.
    """

    def __init__(self) -> None:
        """Initialise the process handle and the session baseline."""
        super().__init__()
        self._session_start = float("inf")
        """Unix time `launch` started Lichii at; infinity until it does.

        Infinity rather than zero so an instance that never launched matches
        no file at all. Zero is newer than nothing, so every title in the
        container would read as written this session and `save_and_exit`
        would restamp and ship all of them.
        """

    def clear_working_slot(self, excluded: tuple[str, ...] = ()) -> None:
        """Drop the previous session's saves and profile before the restore.

        Lichii has no save states and no fixed slot, so the whole clear is the
        save data (`_clear_stale_save_data`). It has to happen here rather than
        at exit: a session that crashes or is killed never reaches
        `save_and_exit`, and the exit restamp ships a save unit whole, so a
        leftover file in one would leave in the next player's archive.

        Args:
            excluded: Subtrees carried by the whole-card routes. Lichii names no
                memory card subtree, so this is always empty.
        """
        _clear_stale_save_data()

    def import_spec(self) -> imports.ImportSpec:
        """Declare what Lichii takes: NAND save exports and the profile store they resolve through.

        Lichii has no states and no cards, so the save kind is the only one.

        Returns:
            The spec.
        """
        shapes = (_SAVE_EXPECTED, _PROFILE_EXPECTED)
        return imports.ImportSpec(kinds=(imports.KindSpec("save", shapes),))

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Place one declared save file, or one of the profile store's.

        Args:
            member: The member, already past the kind gate.
            spec: This emulator's spec.
            ctx: The launch context.

        Returns:
            The placement, or a refusal.
        """
        return _place_save(member, imports.identity_for(self, ctx))

    def validate_import_plan(
        self, plan: list[imports.Placement], ctx: imports.ImportCtx
    ) -> list[imports.ImportRefusal]:
        """Hold an account save to its profile store, and the profile store to one per archive.

        Save paths embed the profile's UUID, so an account save without the
        profile store that names that UUID would be readable by nothing. The
        store has to come in the same import: the archive's own is another
        player's, and is cleared with the rest before the restore. Two profile
        stores cannot both be Lichii's, so an imported one beside the archive's
        is refused rather than merged.

        A file that clashes with another member is left to the shared
        one-member-per-destination check, which has already refused it.

        Args:
            plan: The placements that passed every per-member check.
            ctx: The launch context; its `archive_paths` are the archive's ordinary members.

        Returns:
            One refusal per account save that lacks its profile store, or per
            imported profile file that meets the archive's, or none.
        """
        stores = [p for p in plan if _is_profile_dest(p.dest.parts)]
        if not stores:
            held = [p for p in plan if _is_account_dest(p.dest.parts)]
            reason = "incomplete_unit"
            detail = "an account save needs the profile store it was exported with"
        elif any(_is_profile_dest(PurePosixPath(rel).parts) for rel in ctx.archive_paths):
            held = stores
            reason = "destination_conflict"
            detail = "the archive already carries a profile store"
        else:
            return []
        if not held:
            return []
        skip = imports.destination_conflicts(plan, ctx.archive_paths, False)
        return [
            imports.ImportRefusal(reason, p.member.name, _PROFILE_EXPECTED, detail=detail)
            for p in held
            if p.member.name not in skip
        ]

    def identity_source(self) -> Optional[imports.IdentitySource]:
        """Take the session's game from RomM's title id, 16 hex digits.

        Lichii boots a file, not a path that names an id, so there is no rom reader.

        Returns:
            A `hex16` source with no reader.
        """
        return imports.IdentitySource("hex16")

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The file Lichii should boot for `path`.

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
                log.warning("lichii: search of %s for %s failed: %s", path, pattern, exc)
        return _pick_rom_file(candidates, path)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Patch qt-config.ini, settle the profile import, and boot the game fullscreen.

        Args:
            rom_path: The file to boot.
            resume_slot: Ignored with a log line; Lichii has no save states.

        Raises:
            OSError: When qt-config.ini could not be patched or the import
                decision recorded, so nothing is spawned; see `_patch_ini` and
                `_mark_import_fresh`.
            UnicodeDecodeError: When the existing qt-config.ini is not decodable text.
            ImportPendingError: When an interrupted profile import is on record.
        """
        self.stop()
        _patch_ini()
        _mark_import_fresh()
        if resume_slot is not None:
            log.info(
                "lichii has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        self._session_start = time.time()
        binary = os.environ.get("LICHII_BIN", "lichii")
        # The command line names neither directory, so Lichii resolves both
        # itself. Export the roots the broker resolved so it cannot land
        # anywhere else: the patched qt-config.ini and the dumped saves are
        # only the ones this launch uses if the two agree.
        env = base_launch_env()
        env["XDG_CONFIG_HOME"] = str(CONFIG_DIR.parent)
        env["XDG_DATA_HOME"] = str(DATA_DIR.parent)
        log.info("launching lichii (rom=%s, config=%s, data=%s)", rom_path, CONFIG_DIR, DATA_DIR)
        self._spawn([binary, "-f", "-g", str(rom_path)], env)

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
                        "lichii: could not list %s, saves under it may be dropped from the dump: %s",
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
                    "lichii: could not walk %s, its saves may be dropped from the dump: %s",
                    title,
                    exc,
                )
        return selected

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Stop Lichii and mark this session's saves and profile for the dump.

        The dump ships files newer than the session baseline. A Switch save is
        a directory the game rewrites only partially, and the profile store
        the save paths are keyed by is not rewritten by a game session at all,
        so both are restamped: a restored archive then carries the whole save
        unit and the profile its path resolves through, instead of the
        handful of files the game happened to write.

        Args:
            slot: Ignored; Lichii has no save states.

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
        log.info("lichii: exit restamped %d save dir(s) for the dump", len(touched))
        return {"state_saved": None, "state_slot": None, "state_file": None}
