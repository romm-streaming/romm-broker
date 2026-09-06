"""Xenia Edge (Xbox 360) launcher: ROM resolution and launch/kill lifecycle.

Xenia has no save states and no external control API (no IPC, no socket, no
stdin protocol; the only listener is a gdbstub behind --debug, which drives
the JIT, not the session). Persistence is the game's own save data, which
the guest commits to plain host files under the content tree:

    <storage_root>/content/<XUID>/<TITLE_ID>/00000001/<save name>/...

Each save package carries an STFS header in a sibling tree,
`<TITLE_ID>/Headers/00000001/<save name>`, written when the save is created
and left alone by later writes. The header is what names the save in the
guest's own load menu, so a title's save directory and its header tree only
mean anything together; `Xenia.save_and_exit` restamps whole title
directories for that reason.

Guest writes are write-through: every guest NtWriteFile is an immediate host
pwrite() with no user-space buffering, so save files are complete on disk
the moment the game writes them and survive a hard kill. Xenia installs no
SIGTERM/SIGINT handlers (only SIGILL/SIGSEGV/SIGBUS for the JIT), so SIGTERM
kills it with default disposition. That skips xenia's own exit path, but that
path only saves config and flushes the log, nothing save-related, so the base
SIGTERM->SIGKILL stop is the shutdown story. The exit must always be
broker-driven anyway: a game exiting to dashboard leaves xenia sitting on a
modal dialog instead of exiting, and a guest main-thread exit leaves the
process idling with the window open.

The profile tree (content/<XUID>/FFFE07D1/...) ships inside the content
subtree: Xbox 360 save paths embed the profile XUID, so profile and saves
must travel together for a restore into a fresh container to line up. It is
also the one part of the tree the restore overwrites unconditionally, since
it is the one part the stale-save clear cannot take out; see
`Xenia.always_restore`.

A profile has to exist before the broker launches anything. Xenia Edge
checks its account list as soon as the emulator is initialised and, finding
none, raises a native "No Profiles Found" dialog that is not gated on
--headless and that nobody in the stream can dismiss; without a profile a
game cannot save either. Creating one on the desktop signs it in and persists
the XUID into xenia-edge.config.toml under the storage root
(logged_profile_slot_0_xuid), so every later launch against the same
--storage_root signs in silently. docs/standalone_emulators.md carries the
user-facing version of this.

`--headless` does not mean windowless: the game still renders in the normal
window selkies captures. It auto-answers guest system dialogs (storage
select, sign-in, message boxes) that nobody in the stream could dismiss.
One title per process: loading a second game requires a process restart.

Bootable forms: an XISO (.iso), a bare executable (.xex), an extracted dump
(folder with default.xex at its root), or an XBLA / Games on Demand title
folder, whose STFS package the resolver digs out of the content-type layout
the console uses (see _find_container).
"""

import logging
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

XENIA_BIN = os.environ.get("XENIA_BIN", "/opt/xenia/AppRun")
# Passed as --storage_root so saves land where the broker expects instead of
# ~/Xenia; content, cache, config and the signed-in profile all live under it.
# The desktop launcher has to point at the same directory, or the profile
# created there is not the one a broker launch boots with.
DATA_DIR = Path(os.environ.get("XENIA_DATA_DIR", "/config/xenia"))
XENIA_LOG_PATH = Path(os.environ.get("XENIA_LOG_PATH", "/config/xenia.log"))

# XISO dumps and extracted executables; a folder holding several candidates
# picks by this order. STFS containers (XBLA titles and Games on Demand
# installs) have no extension and are found by layout instead, see
# _find_container.
ROM_EXTENSIONS = (".iso", ".xex")
_ROM_SEARCH_GLOBS = ("*", "*/*")

# An XBLA or GoD title as the console lays it down, and as most dumps keep it:
#
#     [Content/<XUID>/]<TITLE_ID>/<CONTENT_TYPE>/<hash>
#
# <hash> is the STFS header package Xenia boots from; a GoD install keeps its
# payload beside it in <hash>.data/. Only these two content types are games
# (000D0000 arcade title, 00007000 installed game); DLC (00000002) and title
# updates (000B0000) in a sibling folder are picked up by Xenia from the
# content tree, not booted. The globs cover the title folder handed over bare
# (<TITLE_ID> as the root), with the Content/<XUID> prefix, and with one more
# wrapper such as the folder RomM names after the game.
_CONTAINER_TYPE_DIRS = ("000D0000", "00007000")
_CONTAINER_GLOBS = tuple(
    f"{'*/' * depth}{type_dir}/*"
    for depth in (0, 1, 2, 3)
    for type_dir in _CONTAINER_TYPE_DIRS
)
_STFS_MAGICS = (b"CON ", b"LIVE", b"PIRS")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)

# The save tree's own shape, under <storage_root>/content. Anything that is
# not an <XUID>/<TITLE_ID> pair is not save data: the storage root also holds
# config, cache and shader dumps, and the restamp walk must not reach them.
CONTENT_SUBTREE = "content"
_XUID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
_TITLE_ID_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_PROFILE_TITLE_ID = "FFFE07D1"
"""Title id of the profile package Xbox 360 save paths are keyed by."""
_SAVE_CONTENT_TYPE = "00000001"
"""Content type the guest writes saved games under.

The other types under the same title directory are installed content: DLC
(00000002) and title updates (000B0000). They are the game, not a player's
data, so the stale-save clear must not reach them.
"""
_HEADERS_DIR = "Headers"
"""Sibling tree holding a saved game's STFS header, keyed by content type too."""


def _pick_rom_file(candidates: list[Path], base: Path) -> Optional[Path]:
    """Pick the best bootable disc image or executable among `candidates`.

    Hidden files, non-files, unbootable extensions and anything resolving
    outside `ROM_ROOT` are skipped. Ranking prefers the lowest disc number,
    then the `ROM_EXTENSIONS` order, then the shallowest path, then the
    lowercased name.

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
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


def _disc_number(rel: Path) -> int:
    """The disc number named in `rel`.

    Args:
        rel: A candidate's path relative to the folder it was found in.

    Returns:
        The number the disc marker carries, or 1 when the path names none, so
        a single-disc dump ranks alongside a disc 1.
    """
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _is_stfs_package(path: Path) -> bool:
    """Whether `path` opens with an STFS container magic.

    Args:
        path: The file to sniff.

    Returns:
        True when the first four bytes are one of `_STFS_MAGICS`. A file that
        cannot be read is not bootable either, so it reads as False.
    """
    try:
        with path.open("rb") as fh:
            return fh.read(4) in _STFS_MAGICS
    except OSError:
        return False


def _find_container(base: Path) -> Optional[Path]:
    """The STFS header package under a title folder, or None.

    The magic check is what separates the package from its .data payload
    directory and from anything else a dump might have dropped next to it.
    Shallowest match wins, then the arcade type over the installed-game type,
    so a folder holding both lands on one answer every time.
    """
    ranked = []
    for pattern in _CONTAINER_GLOBS:
        try:
            matches = list(base.glob(pattern))
        except OSError as exc:
            # One unreadable subdirectory must not discard the candidates the
            # other patterns already found.
            log.warning("xenia: container search of %s for %s failed: %s", base, pattern, exc)
            continue
        for p in matches:
            if p.name.startswith("."):
                continue
            try:
                if not p.is_file():
                    continue
                real = p.resolve()
                rel = p.relative_to(base)
            except (OSError, ValueError):
                continue
            if not real.is_relative_to(ROM_ROOT) or not _is_stfs_package(real):
                continue
            ranked.append(
                (len(rel.parts), _CONTAINER_TYPE_DIRS.index(p.parent.name), p.name.lower(), real)
            )
    if not ranked:
        return None
    return min(ranked)[3]


def _title_save_dirs(title: Path) -> list[Path]:
    """The saved-game trees inside one `content/<XUID>/<TITLE_ID>` directory.

    A title directory holds one directory per content type, only one of which
    is the player's own data: `00000001` for the saves themselves, plus the
    matching `Headers/00000001` sidecars that name them in the guest's load
    menu. Everything else there (DLC, title updates) is installed content.

    Args:
        title: The content title directory to scan.

    Returns:
        The saved-game and header directories, empty when the title holds
        none or cannot be listed.
    """
    found: list[Path] = []
    try:
        for entry in sorted(title.iterdir()):
            if entry.name == _SAVE_CONTENT_TYPE:
                found.append(entry)
            elif entry.name.lower() == _HEADERS_DIR.lower():
                header = entry / _SAVE_CONTENT_TYPE
                if header.exists() or header.is_symlink():
                    found.append(header)
    except OSError as exc:
        log.warning(
            "xenia: could not list %s, an earlier session's saves may survive into this one: %s",
            title,
            exc,
        )
    return found


class Xenia(Emulator):
    """Emulator adapter for Xenia Edge (Xbox 360).

    Activate drops the previous session's saved games out of the content tree
    and hands the restore an unconditional overwrite of the profile package,
    which the clear has to leave standing. Launch resolves a bootable file out
    of a library entry and records a session baseline. Exit stops the process
    and refreshes the mtime of every
    file under the content title directories written this session, so the
    delta dump ships those titles whole while other titles' content stays
    filtered out. There are no save states, so a resume slot is logged and
    ignored.

    Attributes:
        name: Provider key, `xenia`.
        display_name: Human-readable name.
        save_root: The storage root the save subtrees hang off.
        save_subtrees: `content`, the save and profile tree.
        rom_extensions: Bootable formats, best first.
        log_path: The emulator log file.
        clears_stale_saves: On; activate empties every title's saved-game tree.
    """

    name = "xenia"
    display_name = "Xenia"
    save_root = DATA_DIR
    # The whole content tree: saves (<XUID>/<TITLE_ID>/00000001), their
    # header sidecars, and the profile store the save paths are keyed by.
    # The delta dump only ships files modified since launch, so installed
    # DLC sitting in here does not bloat the archive.
    save_subtrees = (CONTENT_SUBTREE,)
    rom_extensions = ROM_EXTENSIONS
    log_path = XENIA_LOG_PATH
    clears_stale_saves = True
    """On: `clear_working_slot` drops every title's saved games and headers.

    The clear stops at the saved-game content type. The rest of the content
    tree is the container's own furniture, not a player's data: DLC and title
    updates are the installed game, and the profile package the save paths are
    keyed by has to outlive the session that used it, since Xenia Edge refuses
    to start without one behind a dialog nobody in the stream can dismiss.
    """
    # SIGTERM is already a hard kill (no handler installed) and save writes
    # are write-through, so the base 5 s before SIGKILL is only a formality.

    def __init__(self) -> None:
        """Initialise the process handle and the session baseline."""
        super().__init__()
        self._session_start = float("inf")
        """Unix time `launch` started Xenia at; infinity until it does.

        Infinity rather than zero so an instance that never launched matches
        no file at all. Zero is newer than nothing, so every title in the
        container would read as written this session and `save_and_exit`
        would restamp and ship all of them.
        """

    def _stale_save_dirs(self) -> list[Path]:
        """Saved-game trees left in the content tree by an earlier session.

        Scoped the way `_session_title_dirs` scopes the dump: only
        `<XUID>/<TITLE_ID>` pairs count as save data, and the profile package
        is skipped. Every XUID and every title id is swept, not just the
        incoming one, because a directory named for another account or another
        game holds the last player's saves just the same, and the exit restamp
        ships a touched title whole.

        Returns:
            The directories to remove, empty when there is no content tree,
            nothing stale in it, or it cannot be listed.
        """
        targets: list[Path] = []
        content = self.save_root / CONTENT_SUBTREE
        if not content.is_dir():
            return targets
        try:
            xuid_dirs = sorted(content.iterdir())
        except OSError as exc:
            log.warning(
                "xenia: could not list the content tree at %s to clear stale saves, "
                "an earlier session's saves may survive into this one: %s",
                content,
                exc,
            )
            return targets
        for xuid in xuid_dirs:
            if not xuid.is_dir() or not _XUID_RE.match(xuid.name):
                continue
            try:
                title_dirs = sorted(xuid.iterdir())
            except OSError as exc:
                log.warning(
                    "xenia: could not list %s, an earlier session's saves may survive "
                    "into this one: %s",
                    xuid,
                    exc,
                )
                continue
            for title in title_dirs:
                if not title.is_dir() or not _TITLE_ID_RE.match(title.name):
                    continue
                if title.name.upper() == _PROFILE_TITLE_ID:
                    continue
                targets.extend(_title_save_dirs(title))
        return targets

    def clear_working_slot(self) -> None:
        """Drop the previous session's saved games before the archive restore.

        The restore only writes the members this player's archive names, so a
        save the last player left behind would otherwise sit in the content
        tree readable by this one, and leave again in this player's own dump:
        the exit restamp ships a title whole once anything under it is written.

        Only the saved-game content type and its header sidecars go. Installed
        DLC and title updates share the title directory, the profile package
        the save paths are keyed by has to survive for the next launch to sign
        in at all, and config, cache and shader dumps sit beside the content
        tree; none of that is a player's data.
        """
        cleared = 0
        for entry in self._stale_save_dirs():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError as exc:
                log.warning("xenia: could not clear stale save data %s: %s", entry, exc)
            else:
                cleared += 1
                log.debug("xenia: cleared stale save data %s", entry)
        if cleared:
            log.info("xenia: cleared %d stale save dir(s) before the restore", cleared)

    def always_restore(self, rel: str) -> bool:
        """Whether `rel` is a profile member, restored over whatever is on disk.

        The clear leaves the profile package in place, so at restore time it is
        still the last player's, carrying the mtime the exit restamp gave it
        (the profile rides along with any title that saved, so it is routinely
        the freshest thing in the tree). The incoming archive's profile is
        older by construction, since it was taken in this player's previous
        session, and the newer-file guard would pass it over: the session would
        run, and then dump, under the last player's identity, propagating it to
        every player after them.

        The guard does not describe this file. It exists so a restore cannot
        roll back progress made since the archive was taken, and a profile
        package holds no progress: nothing between the last dump and this
        restore wrote it, since the clear and the restore both run before the
        emulator boots.

        Args:
            rel: The member path, relative to `save_root` and posix-separated.

        Returns:
            True for a file under `content/<XUID>/FFFE07D1`.
        """
        parts = PurePosixPath(rel).parts
        return (
            len(parts) > 3
            and parts[0] == CONTENT_SUBTREE
            and _XUID_RE.match(parts[1]) is not None
            and parts[2].upper() == _PROFILE_TITLE_ID
        )

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a library entry to the file Xenia should boot.

        A file is returned as given. A directory is resolved in order: an
        extracted dump's default.xex, then the best disc image or bare
        executable candidate, then an XBLA/Games on Demand content package.

        Args:
            path: The ROM library entry, either a bootable file or a
                directory to search.

        Returns:
            The path to boot, or None if nothing bootable was found or a
            default.xex symlink escapes ROM_ROOT.
        """
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        # An extracted dump boots via its executable.
        default_xex = path / "default.xex"
        try:
            if default_xex.is_file():
                if default_xex.resolve().is_relative_to(ROM_ROOT):
                    return default_xex
                return None  # symlink escapes ROM_ROOT
            if default_xex.exists() or default_xex.is_symlink():
                # present but not a regular file: dangling symlink, or a
                # symlink to a directory/device/fifo. is_file() misses these,
                # and falling through to the candidate search would silently
                # boot something else instead of the dump's own executable.
                return None
        except OSError:
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError as exc:
                # One unreadable subdirectory must not discard the candidates
                # the other patterns already found.
                log.warning("xenia: search of %s for %s failed: %s", path, pattern, exc)
        rom = _pick_rom_file(candidates, path)
        if rom is not None:
            return rom
        # No disc or executable: an XBLA or GoD container, if the layout
        # holds one.
        return _find_container(path)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Stop any running instance and launch Xenia against a ROM.

        Xenia has no save states, so resume_slot is accepted for interface
        parity with other emulators but only logged, never acted on; the
        game always resumes from its own save data.

        Args:
            rom_path: The resolved ROM file to boot.
            resume_slot: Ignored save-state slot, kept for interface parity.
        """
        self.stop()
        if resume_slot is not None:
            log.info(
                "xenia has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._session_start = time.time()
        log.info("launching xenia (rom=%s)", rom_path)
        self._spawn(
            [
                XENIA_BIN,
                "--fullscreen",
                "--headless",
                f"--storage_root={DATA_DIR}",
                "--discord=false",
                str(rom_path),
            ],
            base_launch_env(),
        )

    def _session_title_dirs(self) -> list[Path]:
        """Content title dirs to ship whole, given what this session wrote.

        A 360 save is a directory tree under
        `content/<XUID>/<TITLE_ID>/<CONTENT_TYPE>/<name>` whose STFS header
        lives in a sibling `Headers` tree the guest only writes when the save
        is created. Scoping the dump to individually touched files would leave
        that header behind and restore a save the guest cannot name, so the
        title directory is the smallest unit that stays consistent.

        The profile under `<XUID>/FFFE07D1` joins any XUID that wrote
        something, touched or not: save paths embed the XUID, so saves
        restored without their profile land under an account the target
        container has never signed in.

        A listing or walk that fails is logged and skipped rather than raised:
        the storage root can vanish under the walk, and the exit path calling
        this still has a report to hand back.

        Returns:
            The `content/<XUID>/<TITLE_ID>` directories to restamp, empty when
            no launch set a baseline or nothing was written.
        """
        selected: list[Path] = []
        content = self.save_root / CONTENT_SUBTREE
        if not content.is_dir():
            return selected
        try:
            xuid_dirs = sorted(content.iterdir())
        except OSError as exc:
            log.warning(
                "xenia: could not list the content tree at %s, the dump may be incomplete: %s",
                content,
                exc,
            )
            return selected
        for xuid in xuid_dirs:
            if not xuid.is_dir() or not _XUID_RE.match(xuid.name):
                continue
            try:
                title_dirs = sorted(xuid.iterdir())
            except OSError as exc:
                log.warning(
                    "xenia: could not list %s, its saves may be dropped from the dump: %s",
                    xuid,
                    exc,
                )
                continue
            touched: list[Path] = []
            profile: Optional[Path] = None
            for title in title_dirs:
                if not title.is_dir() or not _TITLE_ID_RE.match(title.name):
                    continue
                if title.name.upper() == _PROFILE_TITLE_ID:
                    profile = title
                    continue
                try:
                    written = any(
                        p.is_file() and p.stat().st_mtime >= self._session_start
                        for p in title.rglob("*")
                    )
                except OSError as exc:
                    log.warning(
                        "xenia: could not walk %s, its saves may be dropped from the dump: %s",
                        title,
                        exc,
                    )
                    continue
                if written:
                    touched.append(title)
            if touched:
                selected.extend(touched)
                if profile is not None:
                    selected.append(profile)
        return selected

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Stop Xenia and mark this session's content for the dump.

        Args:
            slot: Ignored; Xenia has no save states.

        Returns:
            `state_saved`, `state_slot` and `state_file`, all None.
        """
        self.stop()
        # The dump ships files newer than the session baseline, and a 360 save
        # is a tree the guest rewrites only in part. Refreshing every mtime
        # under this session's title dirs ships them whole, headers and
        # profile included, while other titles' content stays filtered out.
        now = time.time()
        title_dirs = self._session_title_dirs()
        for d in title_dirs:
            try:
                for p in d.rglob("*"):
                    if p.is_file():
                        try:
                            os.utime(p, (now, now))
                        except OSError as exc:
                            log.warning(
                                "xenia: could not restamp %s, it may be dropped from the dump: %s",
                                p,
                                exc,
                            )
            except OSError as exc:
                log.warning(
                    "xenia: could not walk %s, the save dump may be incomplete: %s", d, exc
                )
        log.info("xenia exit: restamped %d content dir(s) for the save dump", len(title_dirs))
        return {"state_saved": None, "state_slot": None, "state_file": None}
