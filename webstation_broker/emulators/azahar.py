"""Azahar (Nintendo 3DS) launcher: qt-config.ini patching and SIGTERM shutdown.

Azahar has no control API reachable from outside the process. Its only
network surface is a UDP RPC server that reads and writes emulated memory,
with nothing for state or shutdown, and its command line takes a ROM path and
little else. So there is no mid-session save state here: persistence is the
game's own save data, written to host files under the emulated SD card and
NAND. Azahar installs no SIGTERM handler, so the stop is a hard kill and the
dump takes whatever the game had already committed to disk.

The 3DS data root is XDG-derived (`$XDG_DATA_HOME/azahar-emu`), with config
alongside it under `$XDG_CONFIG_HOME`. Azahar also has a portable mode that
triggers on a `user/` directory in the *working* directory, which is why the
launch never chdir's anywhere it might find one.

Save data paths are stable across containers: the console and SD card ids
Azahar files saves under are hardcoded to all-zeros rather than generated per
install, so a save dumped in one session restores into the next.

Declared imports (`import_spec`, `place_import`) rewrite a donor's console and
SD card ids to those two constants. See docs/content/docs/api/imports.mdx for
the shapes.
"""

import configparser
import logging
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional, Union

from .. import imports
from .base import Emulator, base_launch_env, xdg_config_dir, xdg_data_dir

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Library root a resolved ROM must live under (env `ROM_ROOT`, default `/romm`)."""


# Azahar's Linux layout: one `azahar-emu` directory per XDG root.
USER_DIR = xdg_data_dir("azahar-emu")
"""Azahar's data root, which is also the save root.

Not configurable, and deliberately: nothing on Azahar's command line names
this directory, so an override here would move only the half of the pair the
broker dumps and restores. The emulator would keep writing saves where XDG
puts them, and a session would restore into a tree nothing reads. `launch`
exports the root this resolved to instead, which is an agreement the two
cannot fall out of.
"""
CONFIG_DIR = xdg_config_dir("azahar-emu")
"""Azahar's config directory, holding the qt-config.ini patched before each launch.

Not configurable, for the same reason as `USER_DIR`: the patch would land in
a file Azahar never opens, leaving it parked on its first-run setup.
"""
CONFIG_PATH = CONFIG_DIR / "qt-config.ini"
"""The qt-config.ini patched before every launch."""
AZAHAR_LOG_PATH = Path(os.environ.get("AZAHAR_LOG_PATH", "/config/azahar.log"))
"""The emulator log file (env `AZAHAR_LOG_PATH`, default `/config/azahar.log`)."""

SYSTEM_ID = "0" * 32
"""The console id Azahar files saves under.

Fixed all-zeros rather than generated per install, so these paths are the
same in every container and a dumped save restores where the next session
looks.
"""
SDCARD_ID = "0" * 32
"""The SD card id Azahar files saves under, fixed all-zeros like `SYSTEM_ID`."""

SDMC_DIR = USER_DIR / "sdmc" / "Nintendo 3DS" / SYSTEM_ID / SDCARD_ID
"""The emulated SD card's per-console directory."""
NAND_DATA_DIR = USER_DIR / "nand" / "data" / SYSTEM_ID
"""The emulated NAND's per-console data directory."""

_SAVE_GROUP_ROOTS = (
    SDMC_DIR / "title",
    SDMC_DIR / "extdata",
    NAND_DATA_DIR / "extdata",
    NAND_DATA_DIR / "sysdata",
)
"""Roots holding save trees, each keyed `<titleHigh>/<titleLow>`.

SD title saves are where a game's own saves land; extdata carries the larger
side data some games keep (photos, downloaded content), and sysdata the
system's own.
"""

ROM_EXTENSIONS = (
    ".3ds", ".cci", ".zcci", ".cxi", ".zcxi", ".app",
    ".3dsx", ".z3dsx", ".elf", ".axf", ".bin",
)
"""Formats Azahar boots directly, best first; a folder holding several candidates picks by this order.

No .cia: that is an installable package rather than something the emulator
boots.
"""
_ROM_SEARCH_GLOBS = ("*", "*/*")
"""Glob patterns a ROM folder is searched with; a library folder wrapping a game adds a level."""
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)
"""Matches update and DLC names: they sit beside base games in library folders, and the base game boots."""

_HEX8_RE = re.compile(r"^[0-9a-fA-F]{8}$")
"""Matches a title id half: title dirs under a save group root are `<titleHigh>/<titleLow>`, both 8 hex."""
_SD_ROOT = f"sdmc/Nintendo 3DS/{SYSTEM_ID}/{SDCARD_ID}"
"""The emulated SD card's per-console directory, relative to `USER_DIR`."""
_NAND_EXTDATA = f"nand/data/{SYSTEM_ID}/extdata"
"""The emulated NAND's extdata tree, relative to `USER_DIR`."""
_PROTECTED = ("nand/data/*/sysdata/*",)
"""The system's own data, which an import never writes.

`sysdata` is one of the dumped and restored save subtrees, so nothing but
this keeps a member out of it if the hook were to place one there. The hook
refuses such a member first, with a reason of its own; the glob is the
backstop for a destination it would miss.
"""
_EXPECTED = (
    "sdmc/Nintendo 3DS/<id0>/<id1>/{title,extdata}/<high>/<low>/<tail>, "
    "or nand/data/<id0>/extdata/<high>/<low>/<tail>"
)
"""The shapes an import member is asked to take, for refusals."""
_SD_WRAPPERS = (
    ("saves", "Azahar", "Azahar", "sdmc", "Nintendo 3DS"),
    ("saves", "Azahar", "Azahar", "Nintendo 3DS"),
    ("saves", "Azahar", "Azahar"),
    ("sdmc", "Nintendo 3DS"),
    ("Nintendo 3DS",),
    (),
)
"""Folders an archive may wrap an SD card's ids in, most specific first with `()` last.

`sdmc/` and `Nintendo 3DS/` nest in the order Azahar's own layout does, and
`sdmc` only ever leads to `Nintendo 3DS`. The `saves/Azahar/Azahar/` forms are
where the RetroArch/libretro build of this same core actually writes:
`sort_savefiles_enable` redirects RetroArch's save dir to `saves/Azahar/`
(sorted by the core's `library_name`), and the core then nests its own
`Azahar/` folder under whatever directory it is handed, so a bare
`saves/Azahar/` with no second `Azahar/` is never what the core itself
produces and is left unrecognised rather than guessed at.
"""
_NAND_WRAPPERS = (
    ("saves", "Azahar", "Azahar", "nand", "data"),
    ("nand", "data"),
)
"""Folders an archive may wrap the NAND data ids in. There is no bare form: the NAND path is kept."""
_ID_LEVEL = re.compile(r"[0-9A-Fa-f]{32}", re.ASCII)
"""A console or SD card id, as a folder name."""
_HALF_LEVEL = re.compile(r"[0-9A-Fa-f]{8}", re.ASCII)
"""One half of a title or extdata id, as a folder name."""
_SD_GROUP_LEVEL = re.compile(r"title|extdata", re.ASCII)
"""The SD card's save trees; the rest of an SD card is installed titles and system files."""
_NAND_GROUP_LEVEL = re.compile(r"extdata|sysdata", re.ASCII)
"""The NAND's data trees; `sysdata` is refused."""
_MANAGER_ROOTS = (("3ds", "JKSM"), ("3ds", "Checkpoint"), ("JKSM",), ("Checkpoint",))
"""Where the homebrew save managers put a backup; their layouts are not verified against Azahar's."""
_HARDWARE_MARKERS = ("dbs", "backups")
"""Folders beside an SD card's title tree that only a real console's SD card has."""
_HARDWARE_MEMO = "azahar-hardware-sd"
"""The `ImportCtx.memo` key `_hardware_sd_ids` keeps its answer under."""

_CONFIG_PATCHES: dict[str, dict[str, str]] = {
    "UI": {
        "confirmClose": "false",
        "check_for_update_on_start": "false",
        "enable_discord_presence": "false",
    },
}
"""qt-config.ini keys forced before every launch, by ini section.

Only the ones that would otherwise put something in front of the game: two
that phone home and prompt, and the close confirmation that would leave a
modal behind in a session nobody can click.
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
            log.debug("skipping rom candidate %s: %s", p, exc)
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


def _patch_config() -> None:
    """Force broker-required qt-config.ini values before every launch.

    Patched key-wise, and with raw parsing, so everything else in the file
    survives: Azahar rewrites this whole file on exit, and it holds the
    player's own settings alongside QSettings-encoded keys that would not
    round-trip through interpolation.

    Raises:
        OSError: When the file cannot be written or replaced.
        configparser.Error: When the patched values cannot be serialized.
        UnicodeError: When the merged contents cannot be re-encoded as UTF-8.

    All three are fatal to a launch rather than a warning to launch past:
    an unpatched config leaves `confirmClose` on, so the close the broker
    drives ends at a confirmation modal in a session nobody can click, and
    the update check and Discord presence put their own dialogs in front of
    the game.
    """
    try:
        parser = configparser.RawConfigParser()
        # Keys are case-sensitive here; the default would fold them and write
        # back a second, lowercased copy of every setting Azahar wrote.
        parser.optionxform = str
        if CONFIG_PATH.exists():
            try:
                parser.read(CONFIG_PATH, encoding="utf-8")
            except (configparser.Error, UnicodeDecodeError) as exc:
                log.warning("qt-config.ini unreadable (%s), reseeding it", exc)
                parser = configparser.RawConfigParser()
                parser.optionxform = str
        for section, entries in _CONFIG_PATCHES.items():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in entries.items():
                parser.set(section, key, value)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            # QSettings writes `key=value`; keep that shape so a player
            # reading the file sees one style throughout.
            parser.write(fh, space_around_delimiters=False)
        tmp.replace(CONFIG_PATH)
        log.debug("azahar: patched qt-config.ini at %s", CONFIG_PATH)
    except (OSError, configparser.Error, UnicodeError):
        log.exception("azahar: qt-config.ini patch failed at %s, refusing to launch", CONFIG_PATH)
        raise


def _refuse(member: imports.ImportMember, reason: str, detail: str) -> imports.ImportRefusal:
    """Refuse a member with the Azahar shapes in the message.

    Args:
        member: The member.
        reason: The refusal code.
        detail: What is wrong with this member.

    Returns:
        The refusal.
    """
    return imports.ImportRefusal(reason, member.name, _EXPECTED, detail=detail)


def _hardware_sd_ids(ctx: imports.ImportCtx) -> frozenset[tuple[str, str]]:
    """Find the SD cards in the archive that came off a real console.

    A hardware SD card has ids that are not all zeros and, beside its title
    tree, `dbs` and `backups` folders. Its saves are encrypted, so Azahar
    could not read them however they were placed.

    Args:
        ctx: The launch context; its `memo` holds the answer for the other members.

    Returns:
        The lower-cased `(id0, id1)` pair of every such card.
    """
    cached = ctx.memo.get(_HARDWARE_MEMO)
    if isinstance(cached, frozenset):
        return cached
    found: set[tuple[str, str]] = set()
    for other in ctx.members:
        if other.kind != "save":
            continue
        card = imports.match_anchored(other.parts, wrappers=_SD_WRAPPERS, levels=(_ID_LEVEL, _ID_LEVEL))
        if card is None or card.tail[0] not in _HARDWARE_MARKERS:
            continue
        ids = (card.ids[0].lower(), card.ids[1].lower())
        if ids != (SYSTEM_ID, SDCARD_ID):
            found.add(ids)
    answer = frozenset(found)
    ctx.memo[_HARDWARE_MEMO] = answer
    return answer


def _finish(
    member: imports.ImportMember, subtree: str, high: str, low: str, tail: tuple[str, ...]
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place a member under a title or extdata folder of a save tree.

    Args:
        member: The member.
        subtree: The save tree, relative to `USER_DIR`.
        high: The folder's high half, lower-cased.
        low: The folder's low half, lower-cased.
        tail: The components below the folder.

    Returns:
        The placement, or a refusal.
    """
    dest = imports.build_dest(subtree, (high, low), tail, member=member, expected=_EXPECTED)
    if isinstance(dest, imports.ImportRefusal):
        return dest
    return imports.Placement(member, dest)


def _place_sd(
    member: imports.ImportMember,
    card: imports.AnchoredMatch,
    session: imports.SessionIdentity,
    ctx: imports.ImportCtx,
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place a file of an SD card's title or extdata tree under Azahar's ids.

    A title folder is keyed by the game's own id and held strictly to the
    session's; extdata has an id of its own, which names no title.

    Args:
        member: The member.
        card: The member's path, cut after the SD card's two ids.
        session: The session's identity.
        ctx: The launch context.

    Returns:
        The placement, or a refusal.
    """
    if (card.ids[0].lower(), card.ids[1].lower()) in _hardware_sd_ids(ctx):
        return _refuse(
            member, "source_incompatible", "an encrypted SD card from a console; Azahar cannot read it"
        )
    group, rest = card.tail[0], card.tail[1:]
    if not _SD_GROUP_LEVEL.fullmatch(group):
        return _refuse(member, "unrecognised_layout", "not in the SD card's title or extdata tree")
    if len(rest) < 3 or not (_HALF_LEVEL.fullmatch(rest[0]) and _HALF_LEVEL.fullmatch(rest[1])):
        return _refuse(member, "unrecognised_layout", "not a file in a title's folder")
    high, low = rest[0].lower(), rest[1].lower()
    if group == "title":
        refusal = imports.check_member_identity(
            member,
            imports.NORMALISERS["hex16"](high + low),
            session,
            family="hex16",
            policy="strict",
            expected=_EXPECTED,
        )
        if refusal is not None:
            return refusal
    return _finish(member, f"{_SD_ROOT}/{group}", high, low, rest[2:])


def _place_nand(
    member: imports.ImportMember, data: imports.AnchoredMatch
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place a file of the NAND's extdata tree, and refuse the system's own.

    Args:
        member: The member.
        data: The member's path, cut after the NAND console id and the group folder.

    Returns:
        The placement, or a refusal.
    """
    if data.ids[1] == "sysdata":
        return _refuse(member, "protected_destination", "the system's own data, which Azahar writes itself")
    rest = data.tail
    if len(rest) < 3 or not (_HALF_LEVEL.fullmatch(rest[0]) and _HALF_LEVEL.fullmatch(rest[1])):
        return _refuse(member, "unrecognised_layout", "not a file of an extdata folder")
    return _finish(member, _NAND_EXTDATA, rest[0].lower(), rest[1].lower(), rest[2:])


def _unmatched(member: imports.ImportMember, ctx: imports.ImportCtx) -> imports.ImportRefusal:
    """Refuse a member that is not under an SD card's or the NAND's ids, saying which kind of not it is.

    A loose file, or a bare `data/` folder, names no title. RomM's
    `save_target` is the title's `high/low` pair, which locates a title
    folder and not the file a loose member should be, so nothing is placed
    from it. The refusal still says `destination_unresolvable` when RomM sent
    a `title_id` or a `save_target`, because then the game is known and only
    the path is missing; with neither sent, the layout is what is wrong.

    Args:
        member: The member.
        ctx: The launch context, for RomM's rom.

    Returns:
        The refusal.
    """
    parts = member.parts
    if len(parts) == 1 or parts[0] == "data":
        rom = ctx.rom
        if rom is not None and (rom.title_id or rom.save_target):
            return _refuse(
                member,
                "destination_unresolvable",
                "the title folder is not in the path; send the SD card's Nintendo 3DS folder as it sat",
            )
        return _refuse(member, "unrecognised_layout", "a loose file; a 3DS save is a folder under a title")
    if parts[0] in ("title", "extdata"):
        return _refuse(
            member,
            "unrecognised_layout",
            "no console or SD card folder above it; send the Nintendo 3DS folder as it sat",
        )
    return _refuse(member, "unrecognised_layout", "not a file of Azahar's SD card or NAND save tree")


def _place_save(
    member: imports.ImportMember, session: imports.SessionIdentity, ctx: imports.ImportCtx
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place one file of a 3DS save export under the ids Azahar uses.

    Args:
        member: The member.
        session: The session's identity.
        ctx: The launch context.

    Returns:
        The placement, or a refusal.
    """
    parts = member.parts
    if any(parts[: len(root)] == root for root in _MANAGER_ROOTS):
        return _refuse(
            member,
            "shape_unverified",
            "a save manager's backup folder; its layout is not verified, send the emulator's own folder",
        )
    card = imports.match_anchored(parts, wrappers=_SD_WRAPPERS, levels=(_ID_LEVEL, _ID_LEVEL))
    if card is not None:
        return _place_sd(member, card, session, ctx)
    data = imports.match_anchored(parts, wrappers=_NAND_WRAPPERS, levels=(_ID_LEVEL, _NAND_GROUP_LEVEL))
    if data is not None:
        return _place_nand(member, data)
    return _unmatched(member, ctx)


class Azahar(Emulator):
    """Nintendo 3DS via Azahar, driven by command line flags and config file patching.

    Azahar has no control API reachable from outside the process, so the
    broker pins qt-config.ini before every launch (no close confirmation, no
    update check, no Discord presence) and boots windowed with `-w`. A patch
    that fails aborts the launch: those keys are the only thing keeping a
    modal nobody can click out of the session.
    Fullscreen Azahar stops rendering when the display resizes under it,
    which happens whenever the player resizes their browser. Azahar installs
    no SIGTERM handler, so the stop is a hard kill and the dump takes
    whatever the game had already committed to disk.

    There are no reachable save states: persistence is the game's own save
    data under the emulated SD card and NAND, and the archive is scoped to
    those save trees. At exit every file in the title save dirs written
    during the session gets its mtime refreshed so the delta dump ships
    those saves whole while other titles' saves stay filtered out. A resume
    slot is logged and ignored.

    Attributes:
        name: Provider key, `azahar`.
        display_name: Human-readable name.
        save_root: The data root the save subtrees hang off.
        save_subtrees: The SD title and extdata trees plus the NAND extdata and sysdata trees.
        rom_extensions: Bootable formats, best first.
        log_path: The emulator log file.
        term_timeout: SIGTERM grace before SIGKILL (env `AZAHAR_STOP_WAIT`, default 5).
        clears_stale_saves: On; activate empties every declared save subtree.
    """

    name = "azahar"
    display_name = "Azahar"
    clears_stale_saves = True
    save_root = USER_DIR
    save_subtrees = (
        f"sdmc/Nintendo 3DS/{SYSTEM_ID}/{SDCARD_ID}/title",
        f"sdmc/Nintendo 3DS/{SYSTEM_ID}/{SDCARD_ID}/extdata",
        f"nand/data/{SYSTEM_ID}/extdata",
        f"nand/data/{SYSTEM_ID}/sysdata",
    )
    """The save trees under the data root.

    Scoped to the save trees: the rest of the data root is config, cache,
    shaders and system titles, none of which belong in a save archive.
    """
    rom_extensions = ROM_EXTENSIONS
    log_path = AZAHAR_LOG_PATH
    term_timeout = float(os.environ.get("AZAHAR_STOP_WAIT", "5"))
    """SIGTERM grace before SIGKILL (env `AZAHAR_STOP_WAIT`, default 5).

    No SIGTERM handler: the default action ends the process at once, and
    whatever the game committed is already on disk. The grace window only
    covers process-group teardown.
    """

    def __init__(self) -> None:
        """Initialise the process handle and the session baseline."""
        super().__init__()
        self._session_start = float("inf")
        """Unix time `launch` started the emulator at; infinity until it does.

        Infinity rather than zero so an instance whose launch never completed
        matches no file at all. A baseline of zero is newer than nothing, so
        every title in the container would read as touched this session and
        `save_and_exit` would restamp and ship all of them.
        """

    def clear_working_slot(self, excluded: tuple[str, ...] = ()) -> None:
        """Empty the SD and NAND save trees before the archive restore.

        Azahar files a save under the title id alone, with nothing in the path
        naming the player, so the previous session's saves sit exactly where
        this one's belong. The restore only writes the members the incoming
        archive carries: a title the last player saved and this one's archive
        does not name would stay readable, and the exit restamp ships a title
        whole once anything under it is written, so it would leave again in
        this player's dump.

        Args:
            excluded: Subtrees carried by the whole-card routes. Azahar has no
                memory card, so this is always empty.
        """
        self._clear_save_subtrees(excluded)

    def prepare_restore(self) -> None:
        """Stop a running Azahar so the archive can be extracted under it."""
        self.stop()

    def import_spec(self) -> imports.ImportSpec:
        """Declare what Azahar takes: SD and NAND save trees, and nothing else.

        Azahar has no states and no cards, so the save kind is the only one.

        Returns:
            The spec.
        """
        shapes = (
            "sdmc/Nintendo 3DS/<id0>/<id1>/{title,extdata}/<high>/<low>/<tail>",
            "nand/data/<id0>/extdata/<high>/<low>/<tail>",
        )
        return imports.ImportSpec(kinds=(imports.KindSpec("save", shapes),), protected=_PROTECTED)

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Place one declared save file under the ids Azahar uses.

        Args:
            member: The member, already past the kind gate.
            spec: This emulator's spec.
            ctx: The launch context.

        Returns:
            The placement, or a refusal.
        """
        return _place_save(member, imports.identity_for(self, ctx), ctx)

    def identity_source(self) -> Optional[imports.IdentitySource]:
        """Take the session's game from RomM's save target, or its title id when there is none.

        A 3DS `save_target` is the title's `high/low` pair and `title_id` is
        that same pair written as one sixteen-digit id, so the two normalise
        alike and either names the folder a title save belongs in. Azahar
        boots a file, not a path that names an id, so there is no rom reader.

        Returns:
            A `hex16` source that reads `save_target`, then `title_id`.
        """
        return imports.IdentitySource("hex16", use_save_target=True, fall_back_to_title_id=True)

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The file Azahar should boot for `path`.

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
                log.warning("azahar: search of %s for %s failed: %s", path, pattern, exc)
        picked = _pick_rom_file(candidates, path)
        if picked is not None:
            log.debug("azahar: resolved rom file: %s", picked)
        return picked

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Patch qt-config.ini and boot the game windowed.

        Args:
            rom_path: The file to boot.
            resume_slot: Ignored with a log line; there are no reachable save states.

        Raises:
            OSError: When qt-config.ini could not be patched, so nothing is
                spawned; see `_patch_config`.
            configparser.Error: Likewise, when the patched values cannot be serialized.
            UnicodeError: Likewise, when the merged contents cannot be encoded.
        """
        self.stop()
        _patch_config()
        if resume_slot is not None:
            log.info(
                "azahar has no reachable save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        self._session_start = time.time()
        binary = os.environ.get("AZAHAR_BIN", "/opt/azahar/AppRun")
        # Windowed, not fullscreen: a fullscreen Azahar stops rendering the
        # moment the display resizes under it, and the display resizes
        # whenever the player resizes their browser window.
        cmd = [binary, "-w", str(rom_path)]
        # The command line names neither directory, so the emulator resolves
        # both itself. Export the roots the broker resolved so it cannot land
        # anywhere else: the patched config and the dumped saves are only the
        # ones this launch uses if the two agree.
        env = base_launch_env()
        env["XDG_CONFIG_HOME"] = str(CONFIG_DIR.parent)
        env["XDG_DATA_HOME"] = str(USER_DIR.parent)
        log.info("launching azahar (rom=%s, config=%s, user=%s)", rom_path, CONFIG_DIR, USER_DIR)
        self._spawn(cmd, env)

    def _modified_title_saves(self) -> list[Path]:
        """Title save dirs holding a file written while the session ran.

        Returns:
            The `<titleHigh>/<titleLow>` directories under every save group
            root touched since launch.
        """
        selected: list[Path] = []
        for root in _SAVE_GROUP_ROOTS:
            if not root.is_dir():
                continue
            try:
                for high in sorted(root.iterdir()):
                    if not high.is_dir() or not _HEX8_RE.match(high.name):
                        continue
                    for title in sorted(high.iterdir()):
                        if not title.is_dir() or not _HEX8_RE.match(title.name):
                            continue
                        try:
                            if any(
                                p.is_file() and p.stat().st_mtime >= self._session_start
                                for p in title.rglob("*")
                            ):
                                selected.append(title)
                        except OSError as exc:
                            log.warning(
                                "azahar: could not scan the title save dir at %s, "
                                "its saves may be dropped from the dump: %s",
                                title,
                                exc,
                            )
            except OSError as exc:
                log.warning(
                    "azahar: could not list the save tree at %s, the dump may be incomplete: %s",
                    root,
                    exc,
                )
        return selected

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Stop Azahar and mark this session's title saves for the dump.

        Args:
            slot: Ignored, including None; there are no save states to write,
                and the save-data restamp is what an exit does either way.

        Returns:
            `state_saved`, `state_slot` and `state_file`, all None.
        """
        self.stop()
        # The dump ships files newer than the session baseline. A 3DS save is
        # a directory tree the game rewrites only partially, so refresh every
        # mtime in this session's title save dirs: they ship whole, other
        # titles' saves stay filtered out.
        now = time.time()
        for d in self._modified_title_saves():
            try:
                for p in d.rglob("*"):
                    if p.is_file():
                        try:
                            os.utime(p, (now, now))
                        except OSError as exc:
                            log.warning("could not restamp %s, may be dropped from the dump: %s", p, exc)
            except OSError as exc:
                log.warning("could not walk %s, save dump may be incomplete: %s", d, exc)
        return {"state_saved": None, "state_slot": None, "state_file": None}
