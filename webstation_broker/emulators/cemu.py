"""Cemu (Wii U) launcher: settings.xml patching, gamepad profile seeding, and SIGTERM shutdown.

Cemu has no control API and no save states. Persistence is the game's own
save data, written to host files under the virtual MLC
(`mlc01/usr/save/<titleHigh>/<titleLow>/user/<persistentId>`). Cemu installs
no signal handler, so SIGTERM is a hard kill; saves are already on disk.

A first launch with no settings.xml opens a modal Getting Started dialog, so
a minimal config is seeded before every launch. Cemu creates the default
account (0x80000001) itself on first boot, so save paths line up across
containers without the account store traveling.

Declared imports (`import_spec`, `place_import`) place a title's save tree
under that account: a donor console's persistent id is rewritten to
`DEFAULT_PERSISTENT_ID`. See docs/content/docs/api/imports.mdx for the shapes.

Cemu applies its built-in controller mapping only through the GUI, so the
broker seeds a Wii U GamePad profile for player 0 bound to the selkies
virtual pad. Cemu addresses SDL controllers by joystick GUID, and SDL >= 2.24
folds a CRC16 of the device name into that GUID, so the profile carries one
controller node per GUID variant; the one that matches binds.
"""

import functools
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple, Optional, Union

from .. import imports
from .base import Emulator, base_launch_env, xdg_config_dir, xdg_data_dir

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Library root a resolved ROM must live under (env `ROM_ROOT`, default `/romm`)."""


# Cemu's Linux layout: config under `$XDG_CONFIG_HOME/Cemu`, user data (the
# mlc) under `$XDG_DATA_HOME/Cemu`.
CONFIG_DIR = xdg_config_dir("Cemu")
"""Cemu's config directory, holding the settings.xml and pad profile the broker writes.

Not configurable, and deliberately: nothing on Cemu's command line names it,
so an override would move only the copy the broker patches and leave Cemu
reading its own, parked on the Getting Started modal. `launch` exports the
root this resolved to instead. To move save data, use `CEMU_MLC_DIR`, which
is stated on the command line and so is honoured by both halves.
"""
DATA_DIR = xdg_data_dir("Cemu")
"""Cemu's user data directory, the default home of the MLC. Not configurable; see `CONFIG_DIR`."""
SETTINGS_PATH = CONFIG_DIR / "settings.xml"
"""The settings.xml patched before every launch."""
PROFILE_PATH = CONFIG_DIR / "controllerProfiles" / "controller0.xml"
"""The player-0 controller profile the broker seeds."""
MLC_DIR = Path(os.environ.get("CEMU_MLC_DIR", str(DATA_DIR / "mlc01")))
"""The virtual MLC holding save data (env `CEMU_MLC_DIR`, default `DATA_DIR/mlc01`)."""
SAVE_DIR = MLC_DIR / "usr" / "save"
"""Where title save directories live inside the MLC."""
CEMU_LOG_PATH = Path(os.environ.get("CEMU_LOG_PATH", "/config/cemu.log"))
"""The emulator log file (env `CEMU_LOG_PATH`, default `/config/cemu.log`)."""

ROM_EXTENSIONS = (".wua", ".wux", ".wud", ".wuhb", ".iso", ".rpx", ".elf")
"""Formats Cemu boots directly, best first; a folder holding several candidates picks by this order."""
_ROM_SEARCH_GLOBS = ("*", "*/*", "*/*/*")
"""Glob patterns a ROM folder is searched with.

An extracted dump boots from `<game>/code/<title>.rpx`, two levels down; a
library folder wrapping one adds a third.
"""
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)
"""Matches update and DLC names: they sit beside base games in library folders, and the base game boots."""

_HEX8_RE = re.compile(r"^[0-9a-fA-F]{8}$")
"""Matches a title id half.

Title save dirs are `usr/save/<titleHigh>/<titleLow>`; `system` holds the
account store and play stats.
"""
DEFAULT_PERSISTENT_ID = "80000001"
"""The account id Cemu creates for itself on first boot, and the one an imported save is placed under.

A Wii U keeps each player's save under `user/<persistent id>`, and the id is
per console. Cemu here has one account, so a donor's id is rewritten to it.
"""
_TITLE_HIGH = "00050000"
"""The title id high half of an eShop or disc title, the only kind that keeps a player's save."""
_SAVE_WRAPPERS = (("mlc01", "usr", "save"), ("storage_mlc", "usr", "save"), ("usr", "save"), ("save",))
"""Folders an archive may wrap the save tree in, longest first.

Deliberately no `()`: a member with no wrapper is either the anchorless
`user/...` shape, which `_split` reads next, or not a Cemu save at all.
"""
_SAVE_SUBTREE = "usr/save"
"""The title save tree, relative to the MLC root; the same as `Cemu.save_subtrees`."""
_PROTECTED = ("usr/save/system/*",)
"""The account store. The hook refuses it first; the glob backstops a destination it would miss."""
_EXPECTED = (
    "usr/save/00050000/<title low>/user/<persistent id>/..., "
    "or user/<persistent id>/... for the session's game"
)
"""The shape an import member is asked to take, for refusals."""
_HEX8_LEVEL = re.compile(r"[0-9A-Fa-f]{8}", re.ASCII)
"""One half of a title id, as a folder name."""
_HEX16_LEVEL = re.compile(r"[0-9A-Fa-f]{16}", re.ASCII)
"""A whole title id in one folder name, the way Saviine dumps it."""
_PERSISTENT_ID_RE = re.compile(r"8[0-9A-Fa-f]{7}", re.ASCII)
"""An account's persistent id. Cemu and the Wii U only issue ids with the top bit set."""

_SETTINGS_PATCHES: dict[str, str] = {
    "check_update": "false",
    "receive_untested_updates": "false",
    "use_discord_presence": "false",
    "play_boot_sound": "false",
    "fullscreen_menubar": "false",
}
"""settings.xml keys forced before every launch, all children of `<content>`.

check_update phones home at startup; the rest keep the session free of
dialogs and chrome.
"""

_AUDIO_DEVICE_PATCHES: dict[str, str] = {
    "TVDevice": "default",
    "PadDevice": "default",
}
"""`Audio/<key>` values forced before every launch, all children of `<content>/Audio`.

Cemu ships these blank. `IAudioAPI::CreateDeviceFromConfig` treats an empty
device string as "no device" and silently skips opening any audio stream at
all, rather than falling back to the system default. `default` is the
sentinel `CubebAPI::GetDevices()` reserves for a null device id, the only
string that path resolves to "let cubeb pick the system default".
"""

_PAD_NAME = os.environ.get("CEMU_PAD_NAME", "Microsoft X-Box 360 pad")
"""The selkies virtual pad's name as the interposer presents it (env `CEMU_PAD_NAME`)."""

_VPAD_SDL_MAPPINGS: tuple[tuple[int, int], ...] = (
    (1, 1),    # A -> east
    (2, 0),    # B -> south
    (3, 3),    # X -> north
    (4, 2),    # Y -> west
    (5, 9),    # L -> left shoulder
    (6, 10),   # R -> right shoulder
    (7, 42),   # ZL -> left trigger axis
    (8, 43),   # ZR -> right trigger axis
    (9, 6),    # + -> start
    (10, 4),   # - -> back
    (11, 11),  # d-pad up
    (12, 12),  # d-pad down
    (13, 13),  # d-pad left
    (14, 14),  # d-pad right
    (15, 7),   # left stick click
    (16, 8),   # right stick click
    (17, 45),  # left stick up    -> axis Y-
    (18, 39),  # left stick down  -> axis Y+
    (19, 44),  # left stick left  -> axis X-
    (20, 38),  # left stick right -> axis X+
    (21, 47),  # right stick up    -> rotation Y-
    (22, 41),  # right stick down  -> rotation Y+
    (23, 46),  # right stick left  -> rotation X-
    (24, 40),  # right stick right -> rotation X+
    (27, 5),   # home -> guide
)
"""Wii U GamePad mapping ids to Cemu SDL button codes.

Cemu's own default layout for a generic SDL pad: labels map by position
(VPAD A is the east button), ZL/ZR are the analog trigger axes, sticks are
axis half-ranges.
"""


def _crc16(data: bytes) -> int:
    """CRC-16/ARC, the checksum SDL folds into a joystick GUID.

    Args:
        data: The bytes to checksum, the device name in SDL's case.

    Returns:
        The 16-bit checksum.
    """
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _sdl_guid(crc: int) -> str:
    """The SDL joystick GUID the interposer's virtual pad actually reports.

    The interposer only exposes the pad through the legacy /dev/input/jsN
    nodes, which carry no vendor/product ioctls, so SDL can't build the usual
    bus+vendor+product GUID and falls back to its name-based form instead:
    a zero bus, the name CRC, then the name itself (11 bytes, NUL-padded)
    filling the rest.

    Args:
        crc: The name CRC field, zero for SDL before 2.24.

    Returns:
        The 32-character lowercase hex GUID.
    """
    tail = _PAD_NAME.encode()[:11] + b"\x00"
    guid = bytearray(16)
    guid[2] = crc & 0xFF
    guid[3] = (crc >> 8) & 0xFF
    guid[4 : 4 + len(tail)] = tail
    return guid.hex()


def _pad_uuids() -> list[str]:
    """The uuids the profile binds, in Cemu's `<index>_<guid>` form.

    One with the CRC field zero (SDL before 2.24) and one with the name hash,
    unless `CEMU_PAD_UUIDS` overrides the list with comma-separated values.

    Returns:
        The candidate uuids, one controller node each.
    """
    override = os.environ.get("CEMU_PAD_UUIDS", "")
    if override.strip():
        return [u.strip() for u in override.split(",") if u.strip()]
    return [f"0_{_sdl_guid(0)}", f"0_{_sdl_guid(_crc16(_PAD_NAME.encode()))}"]


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
            log.debug("cemu: skipping candidate %s: %s", p, exc)
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


def _is_account_store(entry: Path) -> bool:
    """Whether `entry` under `usr/save` is the account store rather than save data.

    `usr/save` holds one `<titleHigh>` directory per title id half plus
    `system`, where Cemu keeps the account the save paths are keyed by.
    Anything that is not a title id half is treated as not being save data,
    so an entry nobody recognises is kept rather than deleted.

    Args:
        entry: A top-level entry under `usr/save`.

    Returns:
        True when the entry is not a title save directory.
    """
    return not _HEX8_RE.match(entry.name)


class _Split(NamedTuple):
    """An import member's path, cut into the title's id halves and what sits below them.

    Attributes:
        high: The high half as the member spells it, or None for an anchorless member.
        low: The low half as the member spells it, or None for an anchorless member.
        tail: The components below the title's folder, `user/<id>/...` or `meta/...`.
    """

    high: Optional[str]
    low: Optional[str]
    tail: tuple[str, ...]

    @property
    def persistent_id(self) -> Optional[str]:
        """The donor's account id in upper case, or None when the path names no account.

        Returns:
            The id under `user/`, or None for `user/common`, `meta` and the rest.
        """
        if len(self.tail) >= 2 and self.tail[0] == "user" and _PERSISTENT_ID_RE.fullmatch(self.tail[1]):
            return self.tail[1].upper()
        return None


_refuse = functools.partial(imports.refuse, expected=_EXPECTED)
"""Refuse a member, naming this emulator's accepted shapes (see `imports.refuse`)."""


def _user_problem(tail: tuple[str, ...]) -> Optional[str]:
    """Say why a path below a title's folder cannot be a file Cemu reads under `user/`.

    Cemu opens `user/common/<file>` and `user/<persistent id>/<file>` only, so
    anything else there would be written and never found.

    Args:
        tail: The components below the title's folder.

    Returns:
        What is wrong, or None when the path is not under `user/` or is well shaped.
    """
    if tail[0] != "user":
        return None
    if len(tail) < 3:
        return "no file under an account folder"
    if tail[1] != "common" and not _PERSISTENT_ID_RE.fullmatch(tail[1]):
        return "not an account id a Wii U issues"
    return None


def _split(member: imports.ImportMember) -> Union[_Split, imports.ImportRefusal]:
    """Read a member's path as a Cemu save: a wrapped title folder, or the anchorless `user/...` shape.

    The system tree (`usr/save/system`) and any first level that is not a
    title id are refused here, not left to the protected glob: a name like
    `usr/save/notes` matches no glob and would otherwise be written into the
    save tree. A title whose high half is not `00050000` (demos, updates, DLC)
    holds no player's save.

    Args:
        member: The member.

    Returns:
        The split path, or a refusal.
    """
    found = imports.match_anchored(member.parts, wrappers=_SAVE_WRAPPERS, levels=())
    if found is None:
        if member.parts[0] != "user":
            return _refuse(member, "unrecognised_layout", "not a file in a title's save folder")
        problem = _user_problem(member.parts)
        if problem is not None:
            return _refuse(member, "unrecognised_layout", problem)
        return _Split(None, None, member.parts)
    rest = found.tail
    if len(rest) < 2:
        return _refuse(member, "unrecognised_layout", "a loose file in the save tree")
    if _HEX16_LEVEL.fullmatch(rest[0]):
        high, low, tail = rest[0][:8], rest[0][8:], rest[1:]
    elif _HEX8_LEVEL.fullmatch(rest[0]):
        if len(rest) < 3 or not _HEX8_LEVEL.fullmatch(rest[1]):
            return _refuse(member, "unrecognised_layout", "no file under a title folder")
        high, low, tail = rest[0], rest[1], rest[2:]
    else:
        return _refuse(
            member,
            "protected_destination",
            "not a title's folder; the account store and play stats are Cemu's own",
        )
    if high.upper() != _TITLE_HIGH:
        return _refuse(
            member, "unrecognised_layout", "only titles with high half 00050000 keep a player's save"
        )
    problem = _user_problem(tail)
    if problem is not None:
        return _refuse(member, "unrecognised_layout", problem)
    return _Split(high, low, tail)


def _donor_persistent_ids(ctx: imports.ImportCtx) -> frozenset[str]:
    """Collect the account ids the archive's saves were taken under.

    Args:
        ctx: The launch context; its `memo` holds the answer for the other members.

    Returns:
        Every distinct persistent id across the save members.
    """
    key = "cemu-persistent-ids"
    cached = ctx.memo.get(key)
    if isinstance(cached, frozenset):
        return cached
    found: set[str] = set()
    for other in ctx.members:
        split = _split(other) if other.kind == "save" else None
        if isinstance(split, _Split) and split.persistent_id is not None:
            found.add(split.persistent_id)
    ctx.memo[key] = frozen = frozenset(found)
    return frozen


def _place_split(
    member: imports.ImportMember, split: _Split, session: imports.SessionIdentity
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place a split path under the session's title on the account Cemu created.

    A path that names its title is held strictly to the session's game; one
    that does not takes the session's title, and so needs one.

    Args:
        member: The member.
        split: Its path, cut by `_split`.
        session: The session's identity.

    Returns:
        The placement, or a refusal.
    """
    anchored = split.low is not None
    refusal = imports.check_member_identity(
        member,
        imports.NORMALISERS["hex8"](split.low) if split.low is not None else None,
        session,
        family="hex8",
        policy="strict" if anchored else "required",
        expected=_EXPECTED,
        keyed=anchored,
    )
    if refusal is not None:
        return refusal
    low = split.low if split.low is not None else session.value
    if low is None:
        return imports.ImportRefusal("identity_unknown", member.name, _EXPECTED)
    tail = split.tail
    if split.persistent_id is not None:
        tail = (tail[0], DEFAULT_PERSISTENT_ID, *tail[2:])
    # Cemu formats the title folders in lower case.
    dest = imports.build_dest(
        _SAVE_SUBTREE, (_TITLE_HIGH, low.lower()), tail, member=member, expected=_EXPECTED
    )
    if isinstance(dest, imports.ImportRefusal):
        return dest
    return imports.Placement(member, dest)


def _patch_settings() -> None:
    """Force broker-required settings.xml values before every launch.

    A missing file is seeded, which also skips the first-start Getting
    Started dialog. Patched key-wise so every other setting the user tuned
    through the GUI survives untouched.

    A failure is raised rather than logged and stepped over: without a
    settings.xml Cemu parks on the Getting Started modal, so the launch that
    would go ahead anyway hands the player a blocked stream while the
    activate reports success.

    Raises:
        RuntimeError: When settings.xml cannot be written.
    """
    try:
        root = None
        if SETTINGS_PATH.exists():
            try:
                root = ET.parse(SETTINGS_PATH).getroot()
            except ET.ParseError as exc:
                log.warning("settings.xml unreadable (%s), reseeding it", exc)
        if root is None or root.tag != "content":
            root = ET.Element("content")
        for key, value in _SETTINGS_PATCHES.items():
            node = root.find(key)
            if node is None:
                node = ET.SubElement(root, key)
            node.text = value
        audio = root.find("Audio")
        if audio is None:
            audio = ET.SubElement(root, "Audio")
        for key, value in _AUDIO_DEVICE_PATCHES.items():
            node = audio.find(key)
            if node is None:
                node = ET.SubElement(audio, key)
            node.text = value
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        ET.ElementTree(root).write(tmp, encoding="UTF-8", xml_declaration=True)
        tmp.replace(SETTINGS_PATH)
        log.debug("cemu: patched %s", SETTINGS_PATH)
    except OSError as exc:
        log.error("cemu: settings.xml patch failed at %s: %s", SETTINGS_PATH, exc)
        raise RuntimeError(
            f"could not apply broker settings to {SETTINGS_PATH}: {exc}"
        ) from exc


def _pad_profile_xml() -> str:
    """Build the controller0.xml body.

    One Wii U GamePad with a controller node per candidate uuid, all carrying
    the same mapping. Cemu loads controller nodes independently; a node whose
    device never connects is inert.

    Returns:
        The XML document text, declaration included.
    """
    root = ET.Element("emulated_controller")
    ET.SubElement(root, "type").text = "Wii U GamePad"
    for uuid in _pad_uuids():
        controller = ET.SubElement(root, "controller")
        ET.SubElement(controller, "api").text = "SDLController"
        ET.SubElement(controller, "uuid").text = uuid
        ET.SubElement(controller, "display_name").text = _PAD_NAME
        ET.SubElement(controller, "rumble").text = "0"
        for section in ("axis", "rotation", "trigger"):
            sec = ET.SubElement(controller, section)
            ET.SubElement(sec, "deadzone").text = "0.25"
            ET.SubElement(sec, "range").text = "1"
        mappings = ET.SubElement(controller, "mappings")
        for mapping, button in _VPAD_SDL_MAPPINGS:
            entry = ET.SubElement(mappings, "entry")
            ET.SubElement(entry, "mapping").text = str(mapping)
            ET.SubElement(entry, "button").text = str(button)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _seed_pad_profile() -> None:
    """Write the player-0 pad profile once, if the file is not already there.

    Seeded rather than patched so a player's own remapping, which Cemu
    writes back to this same file, survives every later launch.
    """
    if PROFILE_PATH.exists():
        return
    try:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(_pad_profile_xml())
        log.info("seeded %s", PROFILE_PATH)
    except OSError as exc:
        log.warning("could not seed the pad profile at %s: %s", PROFILE_PATH, exc)


class Cemu(Emulator):
    """Wii U via Cemu, driven by command line flags and config file patching.

    Cemu has no control API, so the broker pins settings.xml before every
    launch (no update check, no Discord presence, no boot sound, no menubar),
    seeds the player-0 GamePad profile once, and boots the game fullscreen
    with `-f -m -g`. Cemu installs no signal handler, so the stop is a hard
    SIGTERM kill; that is safe because the game's own save data is already
    on disk the moment the game writes it.

    There are no save states: persistence is the title save tree under
    `usr/save` in the virtual MLC, which is what the archive carries. At exit
    every file in the title save dirs written during the session gets its
    mtime refreshed so the delta dump ships those saves whole while other
    titles' saves stay filtered out. A resume slot is logged and ignored.

    Attributes:
        name: Provider key, `cemu`.
        display_name: Human-readable name.
        rom_cacheable: On; with the ROM cache enabled, launch boots a local copy of the ROM.
        save_root: The MLC directory the save subtrees hang off.
        save_subtrees: `usr/save`, the title save tree.
        rom_extensions: Bootable formats, best first.
        log_path: The emulator log file.
        term_timeout: SIGTERM grace before SIGKILL (env `CEMU_STOP_WAIT`, default 5).
        clears_stale_saves: On; activate empties the title save tree.
    """

    name = "cemu"
    rom_cacheable = True
    display_name = "Cemu"
    clears_stale_saves = True
    save_root = MLC_DIR
    save_subtrees = ("usr/save",)
    rom_extensions = ROM_EXTENSIONS
    log_path = CEMU_LOG_PATH
    term_timeout = float(os.environ.get("CEMU_STOP_WAIT", "5"))
    """SIGTERM grace before SIGKILL (env `CEMU_STOP_WAIT`, default 5).

    No SIGTERM handler: the default action ends the process at once, saves
    are already on disk. The grace window only covers process-group teardown.
    """

    def __init__(self) -> None:
        """Initialise the process handle and the session baseline."""
        super().__init__()
        self._session_start = float("inf")
        """Unix time `launch` started Cemu at; infinity until it does.

        Infinity rather than zero so an instance that never launched matches
        no file at all. Zero is newer than nothing, so every title in the
        container would read as written this session and `save_and_exit`
        would restamp and ship all of them.
        """

    def clear_working_slot(self, excluded: tuple[str, ...] = ()) -> None:
        """Empty the title save tree before the archive restore.

        Cemu keys a save by title id and by an account id the container shares
        across players, so the previous session's saves sit exactly where this
        one's belong. The restore only writes the members the incoming archive
        carries, and the exit restamp ships a title whole once anything under
        it is written, so a title the last player saved and this one's archive
        does not name would be readable here and would leave again in this
        player's dump.

        The account store under `usr/save/system` stays: it is what the save
        paths are keyed by, not one player's data, and Cemu only writes it on
        a first boot, so clearing it every activate would drop the session onto
        account setup instead of the game.

        Args:
            excluded: Subtrees carried by the whole-card routes. Cemu has no
                memory card, so this is always empty.
        """
        self._clear_save_subtrees(excluded, keep=_is_account_store)

    def prepare_restore(self) -> None:
        """Stop a running Cemu so the archive can be extracted under it."""
        self.stop()

    def import_spec(self) -> imports.ImportSpec:
        """Declare what Cemu takes: title save trees, and nothing else.

        Cemu has no states and no cards, so the save kind is the only one.

        Returns:
            The spec.
        """
        shapes = (
            "usr/save/00050000/<title low>/<tail>",
            "user/<persistent id>/<tail>",
        )
        return imports.ImportSpec(kinds=(imports.KindSpec("save", shapes),), protected=_PROTECTED)

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Place one declared save file under the session's title.

        More than one donor account in the archive is refused, for every file
        that names one: they would all land on Cemu's single account.

        Args:
            member: The member, already past the kind gate.
            spec: This emulator's spec.
            ctx: The launch context.

        Returns:
            The placement, or a refusal.
        """
        split = _split(member)
        if isinstance(split, imports.ImportRefusal):
            return split
        if split.persistent_id is not None and len(_donor_persistent_ids(ctx)) > 1:
            return _refuse(
                member,
                "destination_conflict",
                "the archive holds saves for more than one account; Cemu has one here",
            )
        return _place_split(member, split, imports.identity_for(self, ctx))

    def identity_source(self) -> Optional[imports.IdentitySource]:
        """Take the session's game from RomM's title id, in either Wii U spelling.

        RomM writes a Wii U title id as the low half on its own (`101C9400`)
        or as the whole sixteen-digit id (`00050000101C9400`), the same two
        spellings a member's path may carry, and both name the one save
        folder. Cemu boots titles by file, not by a path that names an id, so
        there is no rom reader.

        Returns:
            A `hex8` source with no reader, reading RomM's id as a `wiiu_title`.
        """
        return imports.IdentitySource("hex8", romm_family="wiiu_title")

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The file Cemu should boot for `path`.

        Args:
            path: A ROM file, or a folder searched up to three levels deep.

        Returns:
            The file itself, the best-ranked bootable file in the folder, or None.
        """
        if path.is_file():
            log.debug("cemu: resolved rom file %s", path)
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError as exc:
                log.debug("cemu: could not glob %s under %s: %s", pattern, path, exc)
                return None
        picked = _pick_rom_file(candidates, path)
        if picked is not None:
            log.debug("cemu: resolved rom file %s from %s", picked, path)
        return picked

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Patch settings, seed the pad profile and boot the game fullscreen.

        Args:
            rom_path: The file to boot.
            resume_slot: Ignored with a log line; Cemu has no save states.

        Raises:
            RuntimeError: When settings.xml cannot be patched, which would
                leave Cemu parked on its Getting Started modal.
        """
        self.stop()
        _patch_settings()
        _seed_pad_profile()
        if resume_slot:
            log.info(
                "cemu has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        self._session_start = time.time()
        binary = os.environ.get("CEMU_BIN", "Cemu")
        # Stated every launch, not only when CEMU_MLC_DIR is set: the mlc also
        # moves with XDG_DATA_HOME, and the path on the command line is what
        # keeps the emulator writing saves into the tree the dump reads back.
        try:
            MLC_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cemu: could not create the mlc at %s: %s", MLC_DIR, exc)
        cmd = [binary, "-f", "-m", str(MLC_DIR), "-g", str(rom_path)]
        # Nothing on the command line names the config, so Cemu resolves that
        # itself. Export the root the broker resolved so the settings.xml and
        # pad profile it just wrote are the ones this launch reads.
        env = base_launch_env()
        env["XDG_CONFIG_HOME"] = str(CONFIG_DIR.parent)
        env["XDG_DATA_HOME"] = str(DATA_DIR.parent)
        log.info("launching cemu (rom=%s, mlc=%s, config=%s)", rom_path, MLC_DIR, CONFIG_DIR)
        self._spawn(cmd, env)

    def _modified_title_saves(self) -> list[Path]:
        """Title save dirs holding a file written while the session ran.

        A listing that fails is logged and skipped rather than raised: the MLC
        can vanish under the walk, and the exit path calling this still has a
        process to stop and a report to hand back.

        Only `<8 hex>/<8 hex>` pairs count. `usr/save` also holds the `system`
        tree, and both halves feed the restamp walk, so a name that is not a
        title id half is not a title save dir.

        Returns:
            The `usr/save/<titleHigh>/<titleLow>` directories touched since
            launch, or nothing at all when no launch set a baseline.
        """
        selected: list[Path] = []
        if not SAVE_DIR.is_dir():
            return selected
        try:
            for high in sorted(SAVE_DIR.iterdir()):
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
                            "cemu: could not walk %s, its saves may be dropped from the dump: %s",
                            title,
                            exc,
                        )
                        continue
        except OSError as exc:
            log.warning(
                "cemu: could not list the save tree at %s, the dump may be incomplete: %s",
                SAVE_DIR,
                exc,
            )
        return selected

    def save_and_exit(self, slot: int) -> dict[str, Any]:
        """Stop Cemu and mark this session's title saves for the dump.

        Args:
            slot: Ignored; there are no save states.

        Returns:
            `state_saved`, `state_slot` and `state_file`, all None.
        """
        self.stop()
        # The dump ships files newer than the session baseline. A save is a
        # directory tree the game rewrites only partially, so refresh every
        # mtime in this session's title save dirs: they ship whole, other
        # titles' saves stay filtered out.
        now = time.time()
        modified = self._modified_title_saves()
        for d in modified:
            for p in d.rglob("*"):
                if p.is_file():
                    try:
                        os.utime(p, (now, now))
                    except OSError as exc:
                        log.warning("could not restamp %s, may be dropped from the dump: %s", p, exc)
        log.info("cemu: restamped %d title save dir(s) for the dump", len(modified))
        return {"state_saved": None, "state_slot": None, "state_file": None}
