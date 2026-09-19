"""DuckStation launcher (RomM platform `psx`): disc resolution, ini patching, and shutdown save states.

DuckStation has no runtime control channel; the whole lifecycle rides its
CLI and settings.

Resume: `-statefile <sav>` loads a state as part of boot. The broker
resolves the state file itself because `-resume` and `-state` abort with an
error dialog when the file is missing.

Save: SIGTERM triggers a graceful shutdown which, with
`Main/SaveStateOnExit=true`, writes `<serial>_resume.sav` into `savestates/`
before the process exits. The write is confirmed by diffing the directory
across `stop()`. The resume state is the only state a shutdown produces, so
it doubles as the broker's save state; the slot number is carried only for
API symmetry.

Ownership: `savestates/` is flat and shared by every title, and the broker
never reads a disc's serial, so it records its own `<state>.rom` marker
beside each state it confirms. That marker, not the filename, is what ties a
state to the disc that produced it on the next resume. Markers ride the save
archive with the states they name.

A shutdown the broker had to force-kill may have torn the state mid-write.
Such a state is set aside under `UNTRUSTED_SUFFIX` rather than destroyed: it
can be the only copy of the player's progress, and only the resume path
needs to be kept away from it.
"""

import contextlib
import logging
import os
import re
import shutil
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Union

from .. import imports
from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Root of the RomM library mount (env `ROM_ROOT`, default `/romm`).

A resolved disc image must sit under it; candidates resolving outside are discarded.
"""


def _data_root() -> Path:
    """Work out DuckStation's data root the way DuckStation works it out.

    DuckStation keeps its whole tree (settings.ini, memcards, savestates)
    under one root, and picks that root from `XDG_CONFIG_HOME`, not
    `XDG_DATA_HOME`, despite what it holds. Probed against the container's
    build: a run with only `XDG_DATA_HOME` set still wrote to
    `$HOME/.local/share/duckstation`, and one with only `XDG_CONFIG_HOME` set
    wrote to `$XDG_CONFIG_HOME/duckstation`. Following the data variable would
    point the broker at a directory DuckStation never writes, and every card
    and state would look missing.

    Returns:
        `$XDG_CONFIG_HOME/duckstation` when that variable is set to an
        absolute path, otherwise DuckStation's own fallback,
        `~/.local/share/duckstation` under `$HOME` (default `/config`).
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "duckstation"
    return Path(os.environ.get("HOME", "/config")) / ".local/share" / "duckstation"


DATA_DIR = _data_root()
"""DuckStation's data root, holding settings.ini, the memory cards and the savestates.

Not configurable, and deliberately: nothing on DuckStation's command line
names it, so an override would move only the tree the broker patches and reads
its resume states out of and leave DuckStation writing its own. `launch`
exports the root this resolved to instead.
"""
INI_PATH = DATA_DIR / "settings.ini"
"""The settings.ini the broker patches before every launch."""
SSTATE_DIR = DATA_DIR / "savestates"
"""Directory DuckStation writes `<serial>_resume.sav` into on shutdown."""
MEMCARD_DIR = DATA_DIR / "memcards"
"""Directory DuckStation keeps its memory cards in, as the pinned `[MemoryCards] Directory` names it."""
PINNED_CARD = "shared_card_1.mcd"
"""The one card the pinned settings mount in slot 1, under MEMCARD_DIR.

DuckStation's default names card 1 after the game's title, which the broker
never reads, so an imported card would have no name to land under before the
disc boots. A Shared card has one fixed name.
"""
DUCKSTATION_LOG_PATH = Path(
    os.environ.get("DUCKSTATION_LOG_PATH", "/config/duckstation.log")
)
"""Log file the broker tails for this emulator (env `DUCKSTATION_LOG_PATH`).

Defaults to `/config/duckstation.log`.
"""

ROM_EXTENSIONS = (
    ".m3u", ".chd", ".cue", ".pbp", ".ccd", ".mds",
    ".iso", ".img", ".ecm", ".bin", ".exe", ".psexe",
)
"""Disc formats duckstation-qt can boot, best first.

A folder holding several candidates picks by this order so an `.m3u`
playlist or `.chd` beats the raw `.bin` beside it.
"""
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


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
        except (OSError, ValueError) as exc:
            log.debug("duckstation: skipping rom candidate %s: %s", p, exc)
            continue
        if not real.is_relative_to(ROM_ROOT):
            continue
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


def _patch_ini() -> None:
    """Force broker-required settings.ini values before every launch.

    On a fresh container the file does not exist yet; seeding it with
    SetupWizardIncomplete already false keeps duckstation-qt from parking on
    the setup wizard, so the very first launch boots the disc. Existing keys
    are rewritten in place, missing ones are added under their section
    (created when absent), and the result is written through a temp file.

    Card 1 is pinned to one shared card, `PINNED_CARD`, and the card and
    state folders to DuckStation's relative defaults, so both stay inside the
    subtrees the save archive carries.

    A failure is raised rather than logged and stepped over: without
    `SaveStateOnExit` the shutdown writes no state at all, so a launch that
    goes ahead anyway costs the player the whole session.

    Raises:
        RuntimeError: When the file cannot be read or rewritten.
    """
    patches: dict[tuple[str, str], str] = {
        ("Main", "SetupWizardIncomplete"): "SetupWizardIncomplete = false",
        # SIGTERM's graceful shutdown must not raise a confirm dialog, and
        # must write the resume state on the way out.
        ("Main", "ConfirmPowerOff"): "ConfirmPowerOff = false",
        ("Main", "SaveStateOnExit"): "SaveStateOnExit = true",
        # .bak copies would leak into the save archive dump.
        ("Main", "CreateSaveStateBackups"): "CreateSaveStateBackups = false",
        ("AutoUpdater", "CheckAtStartup"): "CheckAtStartup = false",
        # Card 1 is one fixed file, so an imported card has a name to land
        # under before the disc boots. A relative path resolves against the
        # memory card directory.
        ("MemoryCards", "Card1Type"): "Card1Type = Shared",
        ("MemoryCards", "Card1Path"): f"Card1Path = {PINNED_CARD}",
        # DuckStation's own defaults, pinned so the cards and states stay in
        # the subtrees the save archive carries. Both resolve against the
        # data root.
        ("MemoryCards", "Directory"): "Directory = memcards",
        ("Folders", "SaveStates"): "SaveStates = savestates",
    }
    try:
        if not INI_PATH.exists():
            log.info("settings.ini not found at %s, seeding one", INI_PATH)
            INI_PATH.parent.mkdir(parents=True, exist_ok=True)
            INI_PATH.write_text("[Main]\nSetupWizardIncomplete = false\n")
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
            for (sec, key), val in patches.items():
                if section != sec:
                    continue
                if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
                    new_lines.append(val)
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
    except OSError as exc:
        log.error("duckstation: settings.ini patch failed at %s: %s", INI_PATH, exc)
        raise RuntimeError(
            f"could not apply broker settings to {INI_PATH}: {exc}"
        ) from exc


def _migrate_memory_card() -> None:
    """Carry an older archive's per-game memory card over to the pinned card.

    Archives written before card 1 was pinned hold DuckStation's per-game
    `<title>_1.mcd`, which the pinned settings no longer mount. While the
    pinned card is not a file, the newest `*_1.mcd` is copied to it. It is
    copied, never moved, so the per-game card stays on disk and in the
    archive. A path at the pinned card that is not a regular file counts as
    absent. A real directory there is never removed, so a card that needs
    carrying cannot land and the launch is refused. A symlink is replaced by
    the copy, never written through, and the launch goes ahead.

    The copy is written just before the dump baseline is taken, so its
    mtime normally falls inside the baseline's slack and it ships with the
    exit dump. If it does not, the unchanged copy is simply not shipped, and
    the next launch makes it again from the per-game card the archive still
    holds.

    Raises:
        RuntimeError: When the copy fails, a directory in its way included.
            Booting on would mount a blank card, and the exit dump would
            ship it as the player's.
    """
    pinned = MEMCARD_DIR / PINNED_CARD
    try:
        pinned_mode: Optional[int] = pinned.stat().st_mode
    except OSError:
        pinned_mode = None
    if pinned_mode is not None:
        if stat.S_ISREG(pinned_mode):
            return
        # Not a card DuckStation can mount, so it counts as absent. It is left
        # in place: a copy below replaces a symlink but fails on a directory.
        log.warning("duckstation: %s is not a memory card file, treating the pinned card as absent", pinned)
    found: list[tuple[float, Path]] = []
    for card in MEMCARD_DIR.glob("*_1.mcd"):
        try:
            st = card.stat()
        except OSError as exc:
            log.warning("duckstation: could not read memory card %s, skipping it: %s", card, exc)
            continue
        # An archive member below `X_1.mcd/` makes a directory by that name,
        # and copying it would fail every launch of that archive.
        if not stat.S_ISREG(st.st_mode):
            log.warning("duckstation: %s is not a memory card file, skipping it", card)
            continue
        found.append((st.st_mtime, card))
    if not found:
        return
    newest = max(found)[1]
    tmp = pinned.with_name(pinned.name + ".tmp")
    try:
        shutil.copyfile(newest, tmp)
        tmp.replace(pinned)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        log.error(
            "duckstation: could not carry memory card %s over to %s: %s", newest.name, PINNED_CARD, exc
        )
        raise RuntimeError(f"could not carry memory card {newest.name} over to {PINNED_CARD}: {exc}") from exc
    log.info(
        "duckstation: carried memory card %s over to %s (%d per-game card(s) found)",
        newest.name,
        PINNED_CARD,
        len(found),
    )


_RESUME_SUFFIX = "_resume.sav"
"""Suffix DuckStation appends to a game's serial when it writes a resume state."""


def _resume_snapshot() -> dict[Path, tuple[int, float]]:
    """Snapshot every `<serial>_resume.sav` in `SSTATE_DIR`.

    Returns:
        A dict of state path to `(size, mtime)`, empty when the directory is missing. Files that
        vanish mid-scan are skipped.
    """
    if not SSTATE_DIR.is_dir():
        return {}
    snap: dict[Path, tuple[int, float]] = {}
    for p in SSTATE_DIR.glob(f"*{_RESUME_SUFFIX}"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError as exc:
            log.debug("duckstation: resume state %s vanished mid-scan, skipping: %s", p, exc)
    return snap


OWNER_SUFFIX = ".rom"
"""Suffix of the marker file recording which disc a resume state belongs to.

The marker sits beside the state it names, holds the booted disc's resolved
path, and rides the save archive with it. It does not match `*_resume.sav`,
so it is never mistaken for a state.
"""

UNTRUSTED_SUFFIX = ".untrusted"
"""Suffix a resume state is renamed with when a force-killed exit may have torn it.

The file is only suspected of being incomplete, never known to be, so it is
set aside under this name rather than deleted. It stops matching
`*_resume.sav`, so no resume can pick it up, and `clear_working_slot` keeps
it where it sweeps everything else. It rides the save archive as ordinary
save data (see `save_file_kind`), which is what makes it recoverable at all:
SSTATE_DIR does not outlive the container.
"""


def _is_quarantined(entry: Path) -> bool:
    """Whether `entry` is a state set aside as possibly torn, or that state's marker."""
    name = entry.name
    return name.endswith(UNTRUSTED_SUFFIX) or name.endswith(UNTRUSTED_SUFFIX + OWNER_SUFFIX)


def _rom_identity(rom: Path) -> str:
    """The disc identity an owner marker records.

    Args:
        rom: The disc image or playlist the session booted.

    Returns:
        The resolved absolute path as text, so the same disc matches across
        sessions while two discs of one title stay distinct.
    """
    try:
        return str(rom.resolve())
    except OSError as exc:
        log.warning("duckstation: could not resolve %s for its state marker: %s", rom, exc)
        return str(rom)


def _owner_marker(state: Path) -> Path:
    """The owner marker path belonging to a resume state."""
    return state.with_name(state.name + OWNER_SUFFIX)


def _state_owner(state: Path) -> Optional[str]:
    """The disc identity recorded for a resume state.

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
        log.debug("duckstation: no state marker at %s", marker)
        return None
    except (OSError, ValueError) as exc:
        log.warning("duckstation: could not read the state marker %s: %s", marker, exc)
        return None
    return recorded or None


def _write_owner_marker(state: Path, rom: Optional[Path]) -> None:
    """Record which disc wrote a resume state, so a later resume can identify it.

    A failure is logged and stepped over: an unmarked state still ships in
    the archive and still resumes when it is the only one there, so the cost
    is a resume that may later be refused as ambiguous, not lost progress.

    Args:
        state: The resume state this exit confirmed.
        rom: The disc the session booted, or None when no launch on this
            object recorded one.
    """
    if rom is None:
        log.warning(
            "duckstation: no rom recorded for this session, leaving %s unmarked", state.name
        )
        return
    marker = _owner_marker(state)
    try:
        marker.write_text(_rom_identity(rom) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("duckstation: could not mark %s as belonging to %s: %s", state.name, rom, exc)
    else:
        log.info("marked resume state %s as belonging to %s", state.name, rom)


def _resume_state_for(rom: Path) -> Optional[Path]:
    """The resume state belonging to the disc about to boot, if it can be told.

    `savestates/` is flat and shared by every title and the broker never
    reads a disc's serial, so a filename says nothing about ownership. The
    marker the last confirmed exit wrote does, and it is matched exactly: a
    state marked for a different disc is never claimed, and a name that
    merely resembles the ROM's path is never enough on its own. A lone
    unmarked state is still taken, which is what carries archives written
    before markers existed. Anything past that is ambiguous, and booting
    clean costs a resume where handing DuckStation another game's state
    costs the player their save.

    Args:
        rom: The disc image or playlist about to boot.

    Returns:
        The state to pass to `-statefile`, or None when there is none or the
        choice cannot be made.
    """
    snapshot = _resume_snapshot()
    states = sorted(snapshot)
    if not states:
        return None
    identity = _rom_identity(rom)
    owners = {p: _state_owner(p) for p in states}
    owned = [p for p in states if owners[p] == identity]
    if owned:
        # Several markers for one disc means a swap wrote more than one
        # serial; the newest is where the player actually left off.
        best = max(owned, key=lambda p: snapshot[p][1])
        if len(owned) > 1:
            log.info(
                "duckstation: %d resume states marked for %s, resuming the newest (%s)",
                len(owned),
                rom.name,
                best.name,
            )
        else:
            log.debug("duckstation: resuming %s, marked for %s", best.name, rom.name)
        return best
    if len(states) == 1 and owners[states[0]] is None:
        log.info(
            "duckstation: resuming %s, the only state in %s, though it carries no owner marker",
            states[0].name,
            SSTATE_DIR,
        )
        return states[0]
    log.error(
        "duckstation: %d resume states in %s and none is marked for %s, booting clean: %s",
        len(states),
        SSTATE_DIR,
        rom.name,
        ", ".join(p.name for p in states),
    )
    return None


def _changed_resume_state(before: dict[Path, tuple[int, float]]) -> Optional[Path]:
    """Find the newest resume state that is new or has changed since `before` was taken.

    Args:
        before: Snapshot from `_resume_snapshot` taken before the shutdown.

    Returns:
        The most recently modified resume state whose size or mtime differs
        from the snapshot, or None when nothing was written.
    """
    best: Optional[tuple[float, Path]] = None
    for p, cur in _resume_snapshot().items():
        if before.get(p) != cur:
            mtime = cur[1]
            if best is None or mtime > best[0]:
                best = (mtime, p)
    return best[1] if best is not None else None


MEMCARD_BYTES = 131072
"""The size of a raw PlayStation memory card, which is what DuckStation mounts: 16 blocks of 8 KiB."""

_RAW_CARD_SUFFIXES = frozenset({".mcd", ".mcr", ".mc", ".srm"})
"""Raw memory card suffixes: each is the bare card image, as DuckStation mounts it.

A RetroArch PlayStation core's `.srm` is the same raw card.
"""

_CONVERTIBLE_CARD_SUFFIXES = frozenset({".gme", ".psm", ".ps", ".ddf", ".mem", ".vgs", ".psx"})
"""Headered memory card formats DuckStation's editor imports only by converting them to a raw card."""

_STATE_NAME = re.compile(r"(?P<base>.+?)(?:_resume|_\d+)\.sav", re.IGNORECASE | re.ASCII)
"""A DuckStation save state's file name: the resume state or a numbered one, after a base."""

_CARD_EXPECTED = f"one raw {MEMCARD_BYTES}-byte .mcd, .mcr, .mc or .srm memory card, as a single file"
"""The `expected` text on every refusal DuckStation's own hook gives a `save` or `memcard` member."""
_STATE_EXPECTED = "one non-empty <serial>_resume.sav or <serial>_<n>.sav, as a single file"
"""The `expected` text on every refusal DuckStation's own hook gives a `state` member."""


def _place_card(member: imports.ImportMember) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place a `save` or `memcard` member: one raw memory card, as the pinned card.

    Args:
        member: The member.

    Returns:
        The placement, or a refusal.
    """
    if len(member.parts) != 1:
        return imports.ImportRefusal(
            "unrecognised_layout", member.name, _CARD_EXPECTED, detail="expected a single file"
        )
    leaf = member.parts[0]
    if leaf.endswith((OWNER_SUFFIX, UNTRUSTED_SUFFIX)):
        # Placed as named so the plan check refuses it as protected_destination,
        # which tells the player more than "not recognised" would.
        return imports.Placement(member, PurePosixPath(SSTATE_DIR.name, leaf))
    suffix = PurePosixPath(leaf).suffix.lower()
    if suffix in _RAW_CARD_SUFFIXES:
        if member.size != MEMCARD_BYTES:
            return imports.ImportRefusal(
                "unrecognised_layout",
                member.name,
                _CARD_EXPECTED,
                detail=f"a raw memory card is {MEMCARD_BYTES} bytes, this one is {member.size}",
            )
        return imports.Placement(member, PurePosixPath(MEMCARD_DIR.name, PINNED_CARD))
    if suffix in _CONVERTIBLE_CARD_SUFFIXES:
        return imports.ImportRefusal(
            "needs_conversion",
            member.name,
            _CARD_EXPECTED,
            detail=f"{suffix} is not a raw memory card; convert it to a raw {MEMCARD_BYTES}-byte .mcd",
        )
    if _STATE_NAME.fullmatch(leaf):
        return imports.ImportRefusal(
            "unrecognised_layout",
            member.name,
            _CARD_EXPECTED,
            detail="a DuckStation save state: declare it as kind state",
        )
    return imports.ImportRefusal("unrecognised_layout", member.name, _CARD_EXPECTED)


def _place_state(
    member: imports.ImportMember, ctx: imports.ImportCtx, session: imports.SessionIdentity
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place a `state` member as the session's resume state, with its owner marker.

    DuckStation names a resume state after the disc's serial, and the broker
    resumes only the state whose marker names the booted disc, so the member
    is renamed to `<serial>_resume.sav` and the marker written as a sidecar.
    The serial is the session's when RomM knows it, else the one in the
    member's name, else the name's own base.

    Args:
        member: The member.
        ctx: The launch context; its `rom_file` is the disc the marker names.
        session: The session's identity.

    Returns:
        The placement, or a refusal.
    """
    rom_file = ctx.rom_file
    if rom_file is None:
        return imports.ImportRefusal(
            "destination_unresolvable",
            member.name,
            _STATE_EXPECTED,
            detail="no rom file to mark the state for",
        )
    single = len(member.parts) == 1
    if single and PurePosixPath(member.parts[0]).suffix.lower() == ".srm":
        return imports.ImportRefusal(
            "source_incompatible", member.name, _STATE_EXPECTED, detail="a RetroArch save file"
        )
    m = _STATE_NAME.fullmatch(member.parts[0]) if single else None
    member_id = imports.NORMALISERS["ps_serial_dashed"](m["base"]) if m else None
    refusal = imports.check_member_identity(
        member,
        member_id,
        session,
        family="ps_serial_dashed",
        policy="strict",
        expected=_STATE_EXPECTED,
        keyed=False,
    )
    if refusal is not None:
        return refusal

    def rename(name: str) -> Optional[str]:
        """Name the state for the session's serial, or failing that the member's.

        Args:
            name: The member's file name.

        Returns:
            `<serial>_resume.sav`, or None when `name` is not a state's.
        """
        found = _STATE_NAME.fullmatch(name)
        if found is None:
            return None
        return f"{session.value or member_id or found['base']}{_RESUME_SUFFIX}"

    dest = imports.place_single_file(
        member,
        subtree=SSTATE_DIR.name,
        pattern=_STATE_NAME,
        rename=rename,
        expected=_STATE_EXPECTED,
        nonempty=True,
        # With no serial to rename to, the state keeps its own base, so a long
        # name reaches the marker. The marker's name is the state's plus
        # `.rom`, and it is written only after the working slot is cleared, so
        # the state leaves room for it.
        max_component_bytes=255 - len(imports.OWNER_MARKER_SUFFIX),
    )
    if isinstance(dest, imports.ImportRefusal):
        return dest
    return imports.Placement(member, dest, (imports.owner_marker_sidecar(dest, rom_file),))


class Duckstation(Emulator):
    """PlayStation 1 sessions on duckstation-qt.

    The broker launches `duckstation-qt -batch -fullscreen -- <disc>` after
    forcing its settings.ini (no setup wizard, no power-off confirm, save a
    state on exit, no state backups, no update check). There is no runtime
    control channel, so the lifecycle is entirely command line and shutdown
    driven. A resume passes the newest `<serial>_resume.sav` with
    `-statefile`, resolved by the broker because DuckStation's own `-resume`
    aborts on a missing file. A save is the graceful shutdown itself: stop()
    sends SIGTERM, DuckStation writes the resume state on the way out, and
    the write is confirmed by diffing the savestates directory across the
    stop. `term_timeout` is raised well above the base default so the SIGKILL
    escalation does not discard that write.

    Because the resume state is the only state a shutdown produces, there is
    no mid-session save or load, and `supports_states` stays at the base
    default; the requested slot is echoed back purely for API symmetry. Save
    data (`memcards`) and states (`savestates`) both ride the save archive.
    DuckStation writes the resume state whether or not one was asked for, so
    an exit without a slot simply leaves it unreported, and the emulator
    resumes from it locally as usual. The state a saving exit does report is
    also what `state_path` serves, so RomM can file it in its state library.

    Memory card 1 is pinned to one shared card, `memcards/shared_card_1.mcd`,
    so an imported card has a fixed name to land under. Launch copies an
    older archive's per-game card over to it once, while it does not exist
    yet. Declared imports take a raw card, as kind `save` or `memcard`, and
    one resume state, renamed to the session's serial and marked as this
    disc's; the state resumes through `save.resume_slot`.

    Attributes:
        name: RomM platform key, `duckstation`.
        display_name: Human-readable name shown in the UI.
        save_root: DuckStation's data root, which the save subtrees hang off.
        save_subtrees: `memcards` and `savestates`, the directories the save archive carries.
        clears_stale_saves: On; activate empties both save subtrees, keeping quarantined states.
        rom_extensions: Bootable disc formats, best first.
        log_path: The DuckStation log the broker exposes.
        term_timeout: Seconds SIGTERM gets before SIGKILL (env `DUCKSTATION_STOP_WAIT`, default 30).
    """

    name = "duckstation"
    display_name = "DuckStation"
    save_root = DATA_DIR
    save_subtrees = ("memcards", "savestates")
    state_subtrees = ("savestates",)
    clears_stale_saves = True
    rom_extensions = ROM_EXTENSIONS
    log_path = DUCKSTATION_LOG_PATH
    term_timeout = float(os.environ.get("DUCKSTATION_STOP_WAIT", "30"))
    """Seconds to wait on SIGTERM before SIGKILL (env `DUCKSTATION_STOP_WAIT`, default 30).

    SIGTERM's graceful shutdown serializes the resume state before exiting;
    give it room before the SIGKILL escalation discards it.
    """

    def __init__(self) -> None:
        """Initialize the emulator with no disc booted yet."""
        super().__init__()
        self._rom_path: Optional[Path] = None
        self._exit_state: Optional[Path] = None

    def save_file_kind(self, rel: str) -> str:
        """Classify an archive member for the manifest.

        The subtree default labels everything under `savestates/` a state,
        which would offer RomM the broker's own owner markers and the states
        a force-killed exit set aside as states the player can pick. Neither
        is loadable, so both ride the archive as opaque save data instead.

        Args:
            rel: The member path, relative to `save_root` and posix-separated.

        Returns:
            One of the kinds `Emulator.save_file_kind` defines.
        """
        if rel.lower().endswith((OWNER_SUFFIX, UNTRUSTED_SUFFIX)):
            return "save"
        return super().save_file_kind(rel)

    def import_spec(self) -> imports.ImportSpec:
        """Declare what DuckStation takes: a raw memory card, and one resume state.

        A card is accepted under either kind and lands as the pinned card.
        The state rides the archive and resumes through `save.resume_slot`,
        like the one the broker saves on exit. There is still no mid-session
        save or load, so `supports_states` stays False.

        Returns:
            The spec.
        """
        card = (f"raw {MEMCARD_BYTES}-byte .mcd/.mcr/.mc/.srm card",)
        return imports.ImportSpec(
            kinds=(
                imports.KindSpec("save", card),
                imports.KindSpec(
                    "state",
                    ("<serial>_resume.sav", "<serial>_<n>.sav"),
                    requires_resume_slot=True,
                    max_members=1,
                    counts_v1=True,
                ),
                imports.KindSpec("memcard", card),
            ),
            state_channel="archive",
            # `*.rom` covers every owner marker, including a set-aside state's:
            # only the broker writes one.
            protected=(f"*{OWNER_SUFFIX}", f"*{UNTRUSTED_SUFFIX}"),
        )

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Place one declared member: a card as the pinned card, a state as the session's resume state.

        Args:
            member: The member, already past the kind gate.
            spec: This emulator's spec.
            ctx: The launch context.

        Returns:
            The placement, or a refusal.
        """
        if member.kind == "state":
            return _place_state(member, ctx, imports.identity_for(self, ctx))
        return _place_card(member)

    def validate_import_plan(
        self, plan: list[imports.Placement], ctx: imports.ImportCtx
    ) -> list[imports.ImportRefusal]:
        """Refuse an imported card when the archive already carries another one.

        Launch carries an older archive's per-game card over to the pinned
        card only while the pinned card does not exist. Once an import has
        put one there, the per-game card is never mounted again, and the
        progress on it drops out of play without a word.

        A card that clashes with another member is left to the shared
        one-member-per-destination check, which has already refused it, so
        it is not refused twice. That covers a second imported card, and an
        archive member at the pinned card's path, below it, or at a path the
        pinned card needs as a directory.

        Args:
            plan: The placements that passed every per-member check.
            ctx: The launch context; its `archive_paths` are the archive's ordinary members.

        Returns:
            One `destination_conflict` for the imported card, or none.
        """
        cards = MEMCARD_DIR.name
        pinned = f"{cards}/{PINNED_CARD}"
        imported = [p for p in plan if p.dest.as_posix() == pinned]
        # Keyed the way the shared check keys them: PurePosixPath drops `.` and
        # empty components, so `./memcards/X_1.mcd` is the file it extracts to.
        archived = {PurePosixPath(rel).as_posix() for rel in ctx.archive_paths}
        carried = sorted(rel for rel in archived if rel.startswith(f"{cards}/"))
        # The shared check's own clash rule: the same file, or one path a
        # strict prefix of the other.
        clashes = len(imported) > 1 or any(
            rel == pinned or rel.startswith(f"{pinned}/") or pinned.startswith(f"{rel}/") for rel in archived
        )
        if not carried or clashes:
            return []
        return [
            imports.ImportRefusal(
                "destination_conflict",
                p.member.name,
                "one memory card per archive",
                detail=f"the archive already carries {', '.join(carried)}",
            )
            for p in imported
        ]

    def identity_source(self) -> Optional[imports.IdentitySource]:
        """Take the session's serial from RomM.

        The broker never reads a serial off the disc, so RomM's `title_id` is
        the only source. An imported state is renamed to it, and one named for
        another serial is refused.

        Returns:
            A PlayStation serial source with no rom reader.
        """
        return imports.IdentitySource("ps_serial_dashed")

    def clear_working_slot(self, excluded: tuple[str, ...] = ()) -> None:
        """Empty the save subtrees this session owns before the archive restore.

        All titles share one flat state directory, and the broker cannot read
        the booting disc's serial to tell which state is its own. Emptying the
        directory here is what leaves the incoming archive's states, and the
        markers restored beside them, as the only pairs a resume can see. A
        marker outliving its state would be worse than none: DuckStation
        reuses a serial's filename, so the next state written under that name
        would inherit an ownership claim nothing verified.

        The memory cards go the same way. A `.mcd` is named by slot, not by
        player, DuckStation has no whole-card route to move it on, and the
        restore only writes the members the incoming archive carries, so a
        card the last player left would otherwise be mounted for this one and
        ship back out in their dump.

        States set aside under `UNTRUSTED_SUFFIX` are left alone. They can be
        the only copy of that progress and no resume can pick them up anyway.

        Args:
            excluded: Subtrees carried by the whole-card routes. DuckStation
                names no memory card subtree, so this is always empty.
        """
        self._clear_save_subtrees(excluded, keep=_is_quarantined)

    def _set_aside_untrusted_state(self, path: Path) -> None:
        """Rename a resume state a force-killed exit may have torn, marker and all.

        Size and mtime are all the broker has to judge a state by, and that
        is enough to refuse to resume from one but not enough to destroy what
        can be the only copy of the player's progress, so the file is moved
        to an `UNTRUSTED_SUFFIX` sidecar instead of unlinked. Its marker
        travels with it: left behind it would claim the next state DuckStation
        writes under the same serial.

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

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a RomM path to the disc image to boot.

        A file is taken as is. A directory is searched one level deep for the
        best candidate by `_pick_rom_file`.

        Args:
            path: The ROM file or folder RomM handed over.

        Returns:
            The image to pass to duckstation-qt, or None when there is nothing bootable.
        """
        if path.is_file():
            log.debug("duckstation: resolve_rom_file resolved directly to %s", path)
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError as exc:
                log.debug("duckstation: rom search %r under %s failed: %s", pattern, path, exc)
                return None
        resolved = _pick_rom_file(candidates, path)
        if resolved is not None:
            log.debug("duckstation: resolve_rom_file resolved %s to %s", path, resolved)
        return resolved

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Stop any running instance, prepare the data dir, and start duckstation-qt.

        Preparing the data dir means patching settings.ini, then carrying an
        older archive's per-game memory card over to the pinned card. The
        binary comes from env `DUCKSTATION_BIN` (default
        `/opt/duckstation/AppRun`). With `resume_slot` set, the resume state
        `_resume_state_for` claims for this disc is passed with `-statefile`;
        a resume with no state on disk is logged and boots clean. The disc is
        recorded on the instance so the exit can mark the state it writes as
        belonging to it.

        Args:
            rom_path: The disc image or playlist to boot.
            resume_slot: Any slot to resume from (the number itself is not used), or None to
                boot clean.

        Raises:
            RuntimeError: When the broker's settings.ini values cannot be
                applied, which would cost the session its exit save state,
                or when an older archive's memory card cannot be carried
                over, which would boot the session on a blank card.
        """
        self.stop()
        self._exit_state = None
        _patch_ini()
        _migrate_memory_card()

        cmd = [os.environ.get("DUCKSTATION_BIN", "/opt/duckstation/AppRun"), "-batch", "-fullscreen"]
        state = _resume_state_for(rom_path) if resume_slot is not None else None
        if resume_slot is not None and state is None:
            log.warning("resume requested but no resume state in %s", SSTATE_DIR)
        if state is not None:
            cmd += ["-statefile", str(state)]
        cmd += ["--", str(rom_path)]

        # Nothing on the command line names the data root, so DuckStation
        # resolves it itself, from XDG_CONFIG_HOME. Export the root the broker
        # resolved so the settings.ini it patched and the resume state it reads
        # back afterwards belong to the tree this launch writes.
        env = base_launch_env()
        env["XDG_CONFIG_HOME"] = str(DATA_DIR.parent)
        log.info("launching duckstation (rom=%s, statefile=%s, data=%s)",
                 rom_path, state, DATA_DIR)
        # The exit's owner marker names this disc, so it has to outlive launch().
        self._rom_path = rom_path
        self._spawn(cmd, env)

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Stop the emulator and report the resume state its shutdown wrote.

        The save is the graceful shutdown: the savestates directory is
        snapshotted, the process is stopped, and a resume state that appeared
        or changed across the stop is reported as the saved state and marked
        as belonging to the booted disc.

        A state a force-killed exit may have torn is neither reported nor
        left where a resume can find it, but it is kept:
        `_set_aside_untrusted_state` renames it rather than deleting it.

        Args:
            slot: The slot RomM asked for, echoed back unchanged; None reports no state even
                though DuckStation still writes one.

        Returns:
            A dict with `state_saved` (bool), `state_slot` (`slot` as given) and `state_file`
            (a dict of `path`, `size` and `mtime` for the resume state, or None).
        """
        saved = False
        state_file: Optional[dict[str, Any]] = None
        was_alive = self.alive()
        before = _resume_snapshot()
        proc = self._proc
        self.stop()
        # SaveStateOnExit is forced true in _patch_ini, so DuckStation writes
        # its resume state on every graceful shutdown regardless of `slot`;
        # an exit with no state requested only skips reporting it here, the
        # file still lands in the save archive dump since it is newer than
        # the session baseline. We can only trust that write if SIGTERM ran
        # its graceful shutdown to completion: death by any signal, not just
        # a SIGKILL escalation, can cut the write off mid-flight (SIGTERM
        # itself included, since term_timeout can still expire and force an
        # OS-level SIGKILL after a hung shutdown). A killed process's changed
        # file has to leave the resume path rather than just go unreported,
        # since the archive dump sweeps up anything with a fresh mtime whether
        # or not this method reports it.
        killed = proc is None or proc.returncode is None or proc.returncode < 0
        if was_alive:
            p = _changed_resume_state(before)
            # Both the set-aside and the marker are independent of `slot`:
            # the file ships in the archive whatever this method reports.
            if killed:
                if p is None:
                    log.warning("duckstation had to be force-killed and wrote no resume state")
                else:
                    log.warning(
                        "duckstation had to be force-killed, setting aside the resume state "
                        "%s it may have torn",
                        p.name,
                    )
                    self._set_aside_untrusted_state(p)
            elif p is None:
                if slot is None:
                    log.info("duckstation exited without a state requested and wrote none")
                else:
                    log.warning("no resume state written during shutdown")
            else:
                _write_owner_marker(p, self._rom_path)
                if slot is None:
                    log.info(
                        "duckstation exited without a state requested, resume state left unreported"
                    )
                else:
                    try:
                        st = p.stat()
                    except OSError as exc:
                        log.warning("could not stat resume state %s: %s", p, exc)
                    else:
                        saved = True
                        state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}
                        self._exit_state = p
        return {"state_saved": saved, "state_slot": slot, "state_file": state_file}

    def state_path(self) -> Optional[Path]:
        """Return the resume state the last saving exit confirmed, or None.

        Only a confirmed exit state is served, never whatever sits in the
        savestates directory: a state already there came in with the archive
        and can belong to another disc, and one a force-killed exit set aside
        may be torn. A launch clears it, since the new session has confirmed
        nothing yet.

        Returns:
            The state file's path, or None when no saving exit has confirmed one
            since the last launch or the file has since gone.
        """
        p = self._exit_state
        return p if p is not None and p.is_file() else None
