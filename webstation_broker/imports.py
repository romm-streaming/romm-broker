"""Declared imports: save data RomM names, placed by each emulator's own rules.

A restored dump puts every member back at its own path. An import cannot
work that way: a player's save from another emulator, a card from real
hardware or an EmulatorJS export has no path this broker wrote. So RomM
declares what each import member is (a save, a state or a memory card),
under the `.import/<kind>/` prefix and in a version 2 manifest, and the
emulator either places it or refuses it with one of `REASONS`.

Everything here runs in preflight, before the working slot is cleared, so a
refusal always leaves the player's slot as it was. The emulator side is four
hooks on `Emulator` (`import_spec`, `place_import`, `validate_import_plan`
and `identity_source`); this module holds the shared machinery they lean on.
"""

import fnmatch
import io
import logging
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Optional, Union

from . import saves, settings

if TYPE_CHECKING:
    from .api import RomIn
    from .emulators.base import Emulator

log = logging.getLogger(__name__)

ImportKind = Literal["save", "state", "memcard"]
"""What RomM declares an import member to be."""
Origin = Literal["emulatorjs", "standalone", "hardware", "unknown"]
"""Where RomM says a member came from. Advisory: placement never reads it."""
StateChannel = Literal["archive", "push", "none"]
"""How an emulator takes states: in the archive, pushed after activate, or not at all."""
IdFamily = Literal[
    "ps_serial_dashed",
    "ps2_card_dir",
    "ps_serial_nodash",
    "hex8",
    "xbox",
    "gc_wii_disc",
    "hex16",
    "wiiu_title",
    "dc_product",
    "scummvm_target",
]
"""The game-id notations `NORMALISERS` can bring to one canonical form."""

REASONS: frozenset[str] = frozenset(
    {
        "manifest_invalid",
        "unsafe_path",
        "unreadable_member",
        "kind_not_accepted",
        "state_uses_push",
        "resume_slot_required",
        "memcard_synced_separately",
        "source_incompatible",
        "needs_conversion",
        "unrecognised_layout",
        "shape_unverified",
        "incomplete_unit",
        "destination_unresolvable",
        "identity_unknown",
        "identity_mismatch",
        "protected_destination",
        "destination_conflict",
        "too_large",
    }
)
"""Every refusal code. The set is closed: RomM branches on these."""
REFUSAL_CAP = 200
"""Most refusals one 422 lists; the rest are counted in `truncated`."""
HEAD_MAX_BYTES = 64 * 1024
"""Most bytes `ImportMember.head` reads, however many are asked for."""


@dataclass(frozen=True)
class ImportRefusal:
    """Why one member (or the whole import) cannot be placed.

    Attributes:
        reason: One of `REASONS`.
        member: The member's zip name, or None for an archive-level refusal.
        expected: What would have been accepted, in words, or None.
        detail: Specifics for this member, or None.
        suggest_emulator: An emulator that would take the member, or None.
    """

    reason: str
    member: Optional[str]
    expected: Optional[str]
    detail: Optional[str] = None
    suggest_emulator: Optional[str] = None

    def __post_init__(self) -> None:
        """Refuse a reason outside the closed set.

        Raises:
            ValueError: When `reason` is not in `REASONS`.
        """
        if self.reason not in REASONS:
            raise ValueError(f"unknown import refusal reason: {self.reason}")

    def as_dict(self) -> dict[str, Optional[str]]:
        """The refusal as the 422 lists it.

        Returns:
            The fields, plus `docs`: the site-relative anchor for the reason.
        """
        return {
            "reason": self.reason,
            "member": self.member,
            "expected": self.expected,
            "detail": self.detail,
            "suggest_emulator": self.suggest_emulator,
            "docs": f"/docs/api/imports#{self.reason.replace('_', '-')}",
        }


@dataclass(frozen=True)
class RomRef:
    """The broker's copy of the activate body's rom, so this module never imports `api`.

    Attributes:
        id: RomM's id for the rom.
        name: The rom's display name.
        platform: The platform slug.
        title_id: RomM's game id for the rom.
        save_target: RomM's name for where the game keeps its saves.
        save_target_layout: How `save_target` names that place.
    """

    id: Optional[int]
    name: Optional[str]
    platform: Optional[str]
    title_id: Optional[str] = None
    save_target: Optional[str] = None
    save_target_layout: Optional[str] = None

    @classmethod
    def from_body(cls, rom: "RomIn") -> "RomRef":
        """Copy the fields imports needs off the activate body's rom.

        Args:
            rom: The validated rom from the activate body.

        Returns:
            The copy.
        """
        return cls(
            rom.id, rom.name, rom.platform, rom.title_id, rom.save_target, rom.save_target_layout
        )


class MemberReadError(Exception):
    """A member's data could not be read, raised by `ImportMember.head`.

    Preflight turns it into an `unreadable_member` refusal, so a placement
    hook that sniffs content never needs a catch of its own.

    Attributes:
        member: The member's zip name, or None when the raiser did not say.
    """

    def __init__(self, message: str, member: Optional[str] = None) -> None:
        """Record what went wrong, and where.

        Args:
            message: What went wrong.
            member: The member's zip name.
        """
        super().__init__(message)
        self.member = member


@dataclass(frozen=True)
class ImportMember:
    """One `.import/` member that passed hygiene, ready for placement.

    Attributes:
        name: The original zip name, echoed in refusals.
        kind: The declared kind.
        origin: The declared origin; advisory, placement never reads it.
        rel: The path below `.import/<kind>/`.
        parts: `rel`'s components.
        size: The uncompressed size, from the zip header.
        info: The zip entry.
    """

    name: str
    kind: ImportKind
    origin: Origin
    rel: PurePosixPath
    parts: tuple[str, ...]
    size: int
    info: zipfile.ZipInfo = field(repr=False, compare=False)
    _zf: Optional[zipfile.ZipFile] = field(default=None, repr=False, compare=False)

    def head(self, n: int) -> bytes:
        """Read the start of the member, for a hook that sniffs content.

        Only valid inside preflight, while its archive is open.

        Args:
            n: Bytes wanted; clamped to `HEAD_MAX_BYTES`.

        Returns:
            Up to `n` bytes from the start of the member.

        Raises:
            RuntimeError: When the member was built without an open archive, or its archive is closed.
            MemberReadError: When the member's data cannot be read.
        """
        if self._zf is None:
            raise RuntimeError(f"{self.name} has no open archive to read from")
        if self._zf.fp is None:
            raise RuntimeError(f"{self.name}'s archive is already closed")
        try:
            with self._zf.open(self.info) as fh:
                return fh.read(max(0, min(n, HEAD_MAX_BYTES)))
        except saves.ZIP_READ_ERRORS as exc:
            log.warning("imports: %s could not be read: %s", self.name, exc)
            raise MemberReadError("the member's data is corrupt", self.name) from exc


@dataclass(frozen=True)
class ImportCtx:
    """What preflight knows about the launch, handed to every placement hook.

    Attributes:
        rom_file: The resolved bootable file, or None.
        rom: The activate body's rom, or None.
        memory_card_synced: Whether the card travels on its own routes this session.
        excluded: Subtrees the restore leaves alone this session.
        resume_slot: The activate's `save.resume_slot`, or None.
        members: Every member that passed hygiene.
        archive_paths: The same zip's v1 member names, less excluded ones.
        v1_bytes: Those members' total uncompressed size.
        memo: Per-preflight cache for hooks, keyed however they like.
    """

    rom_file: Optional[Path]
    rom: Optional[RomRef]
    memory_card_synced: bool
    excluded: tuple[str, ...]
    resume_slot: Optional[int]
    members: tuple[ImportMember, ...] = ()
    archive_paths: frozenset[str] = frozenset()
    v1_bytes: int = 0
    memo: dict[Hashable, object] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class KindSpec:
    """One kind an emulator accepts.

    Attributes:
        kind: The kind.
        shapes: The accepted shapes, in words, for `expected` and discovery.
        requires_resume_slot: Whether the member only boots with `save.resume_slot` set.
        max_members: Most members of this kind one import may place, or None.
        counts_v1: Whether v1 members of the same kind count toward `max_members`.
        companions: `fnmatch` globs for leaf names that ride with the kind's members, such as a
            state's screenshot. They are placed like any member but do not count toward `max_members`.
    """

    kind: ImportKind
    shapes: tuple[str, ...]
    requires_resume_slot: bool = False
    max_members: Optional[int] = None
    counts_v1: bool = False
    companions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportSpec:
    """What an emulator accepts on one platform, and the rules its plan is checked against.

    Attributes:
        kinds: The accepted kinds; empty means no imports.
        state_channel: How the emulator takes states.
        protected: `fnmatch` globs, relative to `save_root`, no member may land on.
        case_insensitive_dest: Whether destinations collide regardless of case.
        unit_depth: Leading destination components that name one save unit.
        unit_requires: Names every unit must hold, relative to the unit.
        unit_subtree: The subtree whose destinations are grouped into units; None groups every one.
        max_component_bytes: Longest path component the emulator's filesystem takes.
        card_subtree: The memory-card subtree, for discovery.
    """

    kinds: tuple[KindSpec, ...] = ()
    state_channel: StateChannel = "none"
    protected: tuple[str, ...] = ()
    case_insensitive_dest: bool = False
    unit_depth: int = 0
    unit_requires: frozenset[str] = frozenset()
    unit_subtree: Optional[str] = None
    max_component_bytes: int = 255
    card_subtree: Optional[str] = None

    def kind(self, k: str) -> Optional[KindSpec]:
        """Look up the spec for one kind.

        Args:
            k: The kind.

        Returns:
            Its `KindSpec`, or None when the kind is not accepted.
        """
        return next((s for s in self.kinds if s.kind == k), None)

    def as_dict(self) -> dict[str, Any]:
        """The spec as the discovery route reports it.

        Returns:
            `kinds`, `state_channel` and `card_subtree`.
        """
        return {
            "kinds": [
                {
                    "kind": s.kind,
                    "shapes": list(s.shapes),
                    "requires_resume_slot": s.requires_resume_slot,
                    "max_members": s.max_members,
                }
                for s in self.kinds
            ],
            "state_channel": self.state_channel,
            "card_subtree": self.card_subtree,
        }


@dataclass(frozen=True)
class Placement:
    """Where one member lands.

    Attributes:
        member: The member.
        dest: The destination, relative to `save_root`.
        sidecars: `(destination, bytes)` files the broker writes beside it.
    """

    member: ImportMember
    dest: PurePosixPath
    sidecars: tuple[tuple[PurePosixPath, bytes], ...] = ()


@dataclass(frozen=True)
class SessionIdentity:
    """The game id this session runs as, and who supplied it.

    Attributes:
        value: The canonical id, or None when nobody supplied one.
        source: `rom` (read off the rom), `romm` (from the activate body) or `none`.
    """

    value: Optional[str]
    source: Literal["rom", "romm", "none"]


@dataclass(frozen=True)
class PreflightResult:
    """What preflight decided.

    Attributes:
        placements: Every placement; empty whenever `refusals` is not.
        refusals: Every refusal, de-duplicated.
        identity: The session's identity.
    """

    placements: tuple[Placement, ...]
    refusals: tuple[ImportRefusal, ...]
    identity: SessionIdentity


def refusal_body(refusals: Iterable[ImportRefusal]) -> dict[str, Any]:
    """Build the 422 detail for a refused import.

    Args:
        refusals: Every refusal.

    Returns:
        `{"error": "import_refused", "refusals": [...], "truncated": n}`, the
        list sorted by `(member or "", reason)` and capped at `REFUSAL_CAP`.
    """
    ordered = sorted(refusals, key=lambda r: (r.member or "", r.reason))
    return {
        "error": "import_refused",
        "refusals": [r.as_dict() for r in ordered[:REFUSAL_CAP]],
        "truncated": max(0, len(ordered) - REFUSAL_CAP),
    }


_V1_REASONS: dict[str, str] = {
    "escapes": "unsafe_path",
    "symlink": "unsafe_path",
    "names_subtree": "unrecognised_layout",
    "outside": "unrecognised_layout",
    "unreadable": "unreadable_member",
    "scratch": "unrecognised_layout",
    "duplicate": "unsafe_path",
    "collides": "destination_conflict",
    "too_long": "unsafe_path",
    "unwritable": "destination_unresolvable",
}
"""Refusal code for each `saves.V1Problem` in an archive that also holds imports."""


def fold_v1_problems(
    view_error: Optional[str], v1_plan: Optional[saves.V1Plan]
) -> list[ImportRefusal]:
    """Turn the legacy whole-archive and v1-member errors into refusals.

    A v1-only archive keeps its legacy 422 string; this is for one that also
    holds imports, where RomM expects the structured list.

    Args:
        view_error: `ArchiveView.error`, or None.
        v1_plan: The v1 plan, or None when it was not run.

    Returns:
        A `too_large` refusal for a whole-archive error, then one per v1 problem,
        each carrying the legacy message as `detail`. An `unreadable_member`
        refusal also carries `READABLE_EXPECTED` as `expected`.
    """
    out: list[ImportRefusal] = []
    if view_error:
        out.append(ImportRefusal("too_large", None, None, detail=view_error))
    for name, message, kind in v1_plan.problems if v1_plan else ():
        reason = _V1_REASONS[kind]
        expected = READABLE_EXPECTED if reason == "unreadable_member" else None
        out.append(ImportRefusal(reason, name, expected, detail=message))
    return out


def fold_read_problems(problems: Iterable[tuple[Optional[str], str]]) -> list[ImportRefusal]:
    """Turn `saves.verify_members` problems into refusals, for an archive that holds imports.

    Args:
        problems: `(member, message)` pairs. A None member is an archive-level problem.

    Returns:
        An `unreadable_member` refusal for each named member and a `too_large`
        refusal for each archive-level problem, each carrying the message as `detail`.
    """
    return [
        ImportRefusal("unreadable_member", name, READABLE_EXPECTED, detail=message)
        if name is not None
        else ImportRefusal("too_large", None, None, detail=message)
        for name, message in problems
    ]


_KINDS: tuple[str, ...] = ("save", "state", "memcard")
"""The kinds a manifest may declare."""
_ORIGINS: frozenset[str] = frozenset({"emulatorjs", "standalone", "hardware", "unknown"})
"""The origins a manifest may declare; anything else is read as `unknown`."""
_MANIFEST_EXPECTED = "a version 2 manifest declaring every .import/ member once, by its kind"
"""The `expected` text on every `manifest_invalid`."""


@dataclass(frozen=True)
class ManifestEntry:
    """One `.import/` member's declaration.

    Attributes:
        path: The member's zip name.
        kind: The declared kind, which matches the path's kind segment.
        origin: The declared origin.
    """

    path: str
    kind: ImportKind
    origin: Origin


def _manifest_invalid(member: Optional[str], detail: str) -> ImportRefusal:
    """Build a `manifest_invalid` refusal.

    Args:
        member: The member, or None for the whole manifest.
        detail: What is wrong.

    Returns:
        The refusal.
    """
    return ImportRefusal("manifest_invalid", member, _MANIFEST_EXPECTED, detail=detail)


def parse_manifest_v2(
    manifest: Any, import_names: Sequence[str], manifest_error: Optional[str] = None
) -> tuple[dict[str, ManifestEntry], list[ImportRefusal]]:
    """Match an archive's `.import/` members to their declarations.

    Entries whose path is not under `.import/` are the v1 entries RomM
    carried over and are ignored. Every import member needs exactly one
    entry whose kind matches its path segment.

    Args:
        manifest: The parsed manifest, or None.
        import_names: The archive's `.import/` member names.
        manifest_error: Why the manifest could not be read, if it could not.

    Returns:
        The valid declarations keyed by member name, and every refusal.
        Without imports, both are empty and the manifest is not checked.
    """
    if not import_names:
        return {}, []
    if manifest_error:
        return {}, [_manifest_invalid(None, manifest_error)]
    if not isinstance(manifest, dict):
        return {}, [_manifest_invalid(None, "manifest is not a JSON object")]
    if manifest.get("version") != 2:
        return {}, [_manifest_invalid(None, f"manifest version is {manifest.get('version')!r}, not 2")]
    files = manifest.get("files")
    if not isinstance(files, list):
        return {}, [_manifest_invalid(None, "manifest files is not a list")]
    if "import" in manifest:
        log.info("imports: manifest import block: %.200r", manifest["import"])

    present = set(import_names)
    entries: dict[str, ManifestEntry] = {}
    refusals: list[ImportRefusal] = []
    seen: set[str] = set()
    refused: set[str] = set()
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            refusals.append(_manifest_invalid(None, f"files[{index}] is not an object"))
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith(saves.IMPORT_PREFIX):
            continue
        if path in seen:
            entries.pop(path, None)
            if path not in refused:
                refused.add(path)
                refusals.append(_manifest_invalid(path, "declared more than once"))
            continue
        seen.add(path)
        kind = entry.get("kind")
        if kind not in _KINDS:
            refused.add(path)
            refusals.append(_manifest_invalid(path, f"kind {kind!r} is not save, state or memcard"))
            continue
        parts = path.split("/")
        if len(parts) < 3 or parts[1] != kind:
            refused.add(path)
            refusals.append(
                _manifest_invalid(path, f"declared {kind} but the path is not under .import/{kind}/")
            )
            continue
        if path not in present:
            refused.add(path)
            refusals.append(_manifest_invalid(path, "declared but not in the archive"))
            continue
        origin = entry.get("origin", "unknown")
        if not isinstance(origin, str) or origin not in _ORIGINS:
            log.info("imports: %s declares unknown origin %r, reading it as unknown", path, origin)
            origin = "unknown"
        entries[path] = ManifestEntry(path, kind, origin)
    for name in import_names:
        if name not in seen:
            seen.add(name)
            refusals.append(_manifest_invalid(name, "not declared in the manifest"))
    return entries, refusals


_UTF8_FLAG = 0x800
"""Zip general-purpose flag bit saying the entry name is UTF-8."""
_SAFE_EXPECTED = "a relative path of plain names: no hidden, system or oversized components"
"""The `expected` text on every hygiene `unsafe_path` refusal."""
READABLE_EXPECTED = "an unencrypted, intact member, stored or compressed with deflate"
"""The `expected` text on every `unreadable_member` refusal."""


_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
"""Characters that reorder how text displays, so a name holding one shows as a different name."""


def _name_problem(name: str) -> Optional[str]:
    """Check a whole name for characters no save path may hold.

    Refuses C0 and C1 control characters, DEL and the bidirectional controls.

    Args:
        name: A member name or a single component.

    Returns:
        What is wrong, or None.
    """
    if "\\" in name:
        return "backslash in name"
    if ":" in name:
        return "colon in name"
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in name):
        return "control character in name"
    if any(c in _BIDI_CONTROLS for c in name):
        return "bidirectional control character in name"
    return None


def _component_problem(part: str, max_component_bytes: int) -> Optional[str]:
    """Check one path component.

    Args:
        part: The component.
        max_component_bytes: The longest component allowed, in UTF-8 bytes.

    Returns:
        What is wrong, or None.
    """
    if part in ("", ".", ".."):
        return f"empty or relative component {part!r}"
    if part.startswith("."):
        return f"hidden component {part!r}"
    if part == "__MACOSX":
        return "__MACOSX component"
    if "/" in part:
        return f"slash in component {part!r}"
    if len(part.encode("utf-8")) > max_component_bytes:
        return f"component longer than {max_component_bytes} bytes"
    return None


def check_state_basename(name: str) -> bool:
    """Tell whether a pushed state's filename is a plain basename the broker may write under.

    The push route takes the name from a query parameter, so it gets the
    component and character checks an archive member gets, plus one of its
    own: a leading space, which no emulator writes and which hides the
    start of the name in a listing.

    Args:
        name: The filename as pushed.

    Returns:
        True when the name is safe to join onto a state directory.
    """
    return not (_component_problem(name, 255) or _name_problem(name) or name[:1].isspace())


def normalise_member(
    info: zipfile.ZipInfo,
    entry: ManifestEntry,
    *,
    zf: Optional[zipfile.ZipFile],
    max_component_bytes: int = 255,
) -> Union[ImportMember, ImportRefusal]:
    """Apply every hygiene rule to one member, in one pass.

    This is the only hygiene check: placement hooks can rely on a member's
    parts being plain names.

    Args:
        info: The member's zip entry.
        entry: Its declaration, whose kind has already been matched to the path.
        zf: The open archive, for `ImportMember.head`; None in tests.
        max_component_bytes: The longest component the emulator's filesystem takes.

    Returns:
        The member, or an `unsafe_path` or `unreadable_member` refusal.
    """
    name = info.filename

    def unsafe(detail: str) -> ImportRefusal:
        """Build this member's `unsafe_path` refusal.

        Args:
            detail: What is wrong.

        Returns:
            The refusal.
        """
        return ImportRefusal("unsafe_path", name, _SAFE_EXPECTED, detail=detail)

    if not name.isascii() and not info.flag_bits & _UTF8_FLAG:
        return unsafe("non-ASCII name without the zip UTF-8 flag")
    problem = _name_problem(name)
    if problem:
        return unsafe(problem)
    prefix = f"{saves.IMPORT_PREFIX}{entry.kind}/"
    if not name.startswith(prefix) or len(name) == len(prefix):
        return unsafe(f"nothing below {prefix}")
    tail = name[len(prefix):].split("/")
    for part in tail:
        problem = _component_problem(part, max_component_bytes)
        if problem:
            return unsafe(problem)
    # A placed member is stamped with the write time, so its date is never read.
    problem = saves.member_problem(info, check_date=False)
    if problem:
        return ImportRefusal("unreadable_member", name, READABLE_EXPECTED, detail=f"the member {problem}")
    return ImportMember(
        name=name,
        kind=entry.kind,
        origin=entry.origin,
        rel=PurePosixPath(*tail),
        parts=tuple(tail),
        size=info.file_size,
        info=info,
        _zf=zf,
    )


def _accepted_kinds(spec: ImportSpec) -> str:
    """Name the kinds an emulator takes, for `expected`.

    Args:
        spec: The emulator's spec.

    Returns:
        A comma-separated list, or `no imports`.
    """
    return ", ".join(s.kind for s in spec.kinds) or "no imports"


def gate_kind(member: ImportMember, spec: ImportSpec, ctx: ImportCtx) -> Optional[ImportRefusal]:
    """Refuse a member whose kind the emulator does not take, before any hook sees it.

    Args:
        member: The member.
        spec: The emulator's spec for this platform.
        ctx: The launch context.

    Returns:
        `state_uses_push`, `kind_not_accepted` or `resume_slot_required`, or
        None when the member may go on to `place_import`.
    """
    kind_spec = spec.kind(member.kind)
    if kind_spec is None:
        if member.kind == "state" and spec.state_channel == "push":
            return ImportRefusal(
                "state_uses_push",
                member.name,
                "a state PUT to /api/session/state-file after activate, with resume_slot set",
            )
        return ImportRefusal("kind_not_accepted", member.name, _accepted_kinds(spec))
    if kind_spec.requires_resume_slot and ctx.resume_slot is None:
        return ImportRefusal(
            "resume_slot_required",
            member.name,
            "save.resume_slot set on the activate",
            detail="without it the state is never loaded, and the exit dump overwrites it",
        )
    return None


LIBRETRO_STATE_RE = re.compile(r"^.+\.state(\d+|\.auto)$", re.I | re.ASCII)
"""A RetroArch numbered or auto state name (`.state3`, `.state.auto`), which no standalone loads.

A bare `.state` is not matched: flycast takes that name as its own.
"""


def place_single_file(
    member: ImportMember,
    *,
    subtree: str,
    pattern: re.Pattern[str],
    rename: Callable[[str], Optional[str]],
    expected: str,
    allow_wrappers: tuple[str, ...] = (),
    nonempty: bool = False,
    refuse_libretro_states: bool = True,
    max_component_bytes: int = 255,
) -> Union[PurePosixPath, ImportRefusal]:
    """Place a member that is one file in one directory.

    Args:
        member: The member.
        subtree: The directory it lands in, relative to `save_root`.
        pattern: What the file's name must `fullmatch`.
        rename: The emulator's own pure renamer, called with pre-launch inputs only.
            It returns None for a name it does not recognise.
        expected: The accepted shape, in words.
        allow_wrappers: Leading folders (slash-joined) to strip, at most one.
        nonempty: Whether an empty file is an `incomplete_unit`.
        refuse_libretro_states: Whether a RetroArch state name is `source_incompatible`.
        max_component_bytes: Longest name the destination filesystem takes.

    Returns:
        The destination, or a refusal.
    """
    parts = member.parts
    for wrapper in allow_wrappers:
        head = tuple(wrapper.split("/"))
        if parts[: len(head)] == head and len(parts) > len(head):
            parts = parts[len(head):]
            break
    if len(parts) != 1:
        return ImportRefusal("unrecognised_layout", member.name, expected, detail="expected a single file")
    leaf = parts[0]
    if refuse_libretro_states and LIBRETRO_STATE_RE.fullmatch(leaf):
        return ImportRefusal(
            "source_incompatible", member.name, expected, detail="a RetroArch (libretro) state"
        )
    if not pattern.fullmatch(leaf):
        return ImportRefusal("unrecognised_layout", member.name, expected)
    if nonempty and member.size == 0:
        return ImportRefusal("incomplete_unit", member.name, expected, detail="the file is empty")
    new = rename(leaf)
    if new is None:
        return ImportRefusal(
            "unrecognised_layout", member.name, expected, detail="name not recognised by the emulator"
        )
    problem = _name_problem(new) or _component_problem(new, max_component_bytes)
    if problem:
        return ImportRefusal("unsafe_path", member.name, expected, detail=f"renamed to {new!r}: {problem}")
    return PurePosixPath(subtree) / new


@dataclass(frozen=True)
class AnchoredMatch:
    """A member path split at its id levels.

    Attributes:
        wrapper: The leading folders that were stripped.
        ids: One component per id level.
        tail: Everything below the ids.
    """

    wrapper: tuple[str, ...]
    ids: tuple[str, ...]
    tail: tuple[str, ...]


def match_anchored(
    parts: Sequence[str],
    *,
    wrappers: Sequence[tuple[str, ...]],
    levels: Sequence[re.Pattern[str]],
    min_tail: int = 1,
) -> Optional[AnchoredMatch]:
    """Match a tree whose id folders sit at a fixed depth.

    Exactly one leading wrapper is stripped: the first in `wrappers` that
    matches, so callers list them longest first and `()` last. There is no
    fall-through and no substring search.

    Args:
        parts: The member's components.
        wrappers: Candidate leading folders.
        levels: One pattern per id level, each `fullmatch`ed.
        min_tail: Fewest components required below the ids.

    Returns:
        The split, or None when the path does not fit.
    """
    for wrapper in wrappers:
        if tuple(parts[: len(wrapper)]) == tuple(wrapper):
            rest = tuple(parts[len(wrapper):])
            break
    else:
        return None
    if len(rest) < len(levels) + min_tail:
        return None
    ids = rest[: len(levels)]
    if not all(p.fullmatch(i) for p, i in zip(levels, ids)):
        return None
    return AnchoredMatch(tuple(wrapper), ids, rest[len(levels):])


def build_dest(
    subtree: str,
    ids: Sequence[str],
    tail: Sequence[str],
    *,
    member: ImportMember,
    expected: str,
    max_component_bytes: int = 255,
) -> Union[PurePosixPath, ImportRefusal]:
    """Join rewritten ids and a tail under a subtree, re-checking every component.

    Args:
        subtree: The destination subtree, relative to `save_root`.
        ids: The id components, already rewritten by the caller.
        tail: The components below them.
        member: The member, for the refusal.
        expected: The accepted shape, in words.
        max_component_bytes: Longest name the destination filesystem takes.

    Returns:
        The destination, or an `unsafe_path` refusal.
    """
    for part in (*ids, *tail):
        problem = _name_problem(part) or _component_problem(part, max_component_bytes)
        if problem:
            return ImportRefusal("unsafe_path", member.name, expected, detail=problem)
    return PurePosixPath(subtree, *ids, *tail)


OWNER_MARKER_SUFFIX = ".rom"
"""What flycast and duckstation append to a resume state's name to name its owner marker."""


def owner_marker_sidecar(dest: PurePosixPath, rom_file: Path) -> tuple[PurePosixPath, bytes]:
    """Build the owner marker an imported resume state needs, as a placement sidecar.

    Flycast and DuckStation resume a state only when the marker beside it
    names the rom the session boots. The bytes match what both modules'
    `_write_owner_marker` writes on exit, so an imported state resumes like
    one the broker saved.

    Args:
        dest: The state's destination, relative to `save_root`.
        rom_file: The file this activate boots. For an `.m3u` boot it is the playlist.

    Returns:
        The marker's destination and its bytes: the rom's resolved path and a newline, in UTF-8.
    """
    try:
        identity = str(rom_file.resolve())
    except OSError as exc:
        log.warning("imports: could not resolve %s for its owner marker: %s", rom_file, exc)
        identity = str(rom_file)
    return dest.with_name(dest.name + OWNER_MARKER_SUFFIX), (identity + "\n").encode("utf-8")


_PS_DASHED = re.compile(r"([A-Z]{4})[-_ ]?(\d{3})\.?(\d{2})", re.I | re.ASCII)
"""A PlayStation serial in any of its spellings: `SLUS-20001`, `SLUS_200.01`, `slus20001`."""
_PS_NODASH = re.compile(r"([A-Za-z]{4})[-_ ]?(\d{5})", re.ASCII)
"""A PSP/PS3 serial with or without its separator."""
_HEX8 = re.compile(r"(?:0x)?([0-9A-Fa-f]{8})", re.ASCII)
"""An eight-digit hex title id, optionally `0x`-prefixed."""
_XBOX_CODE = re.compile(r"([A-Za-z]{2})-(\d{3})", re.ASCII)
"""An Xbox publisher-code-and-number id such as `MS-100`."""
_GAME_ID = re.compile(r"[A-Za-z0-9]{4}(?:[A-Za-z0-9]{2})?", re.ASCII)
"""A GameCube/Wii game id: four characters, plus two for the maker."""
_HEX16 = re.compile(r"(?:0x)?([0-9A-Fa-f]{16})", re.ASCII)
"""A sixteen-digit hex title id, as the Switch writes it."""


def _ps_serial_dashed(raw: str) -> Optional[str]:
    """Normalise a PlayStation serial to `XXXX-NNNNN`.

    Args:
        raw: The id as found.

    Returns:
        The canonical id, or None.
    """
    m = _PS_DASHED.fullmatch(raw.strip())
    return f"{m[1].upper()}-{m[2]}{m[3]}" if m else None


def _ps_serial_nodash(raw: str) -> Optional[str]:
    """Normalise a PSP/PS3 serial to `XXXXNNNNN`.

    Args:
        raw: The id as found.

    Returns:
        The canonical id, or None.
    """
    m = _PS_NODASH.fullmatch(raw.strip())
    return f"{m[1].upper()}{m[2]}" if m else None


def _hex8(raw: str) -> Optional[str]:
    """Normalise an eight-digit hex id to upper case, without `0x`.

    Args:
        raw: The id as found.

    Returns:
        The canonical id, or None.
    """
    m = _HEX8.fullmatch(raw.strip())
    return m[1].upper() if m else None


def _xbox(raw: str) -> Optional[str]:
    """Normalise an Xbox title id: hex, or a publisher code like `MS-100`.

    Args:
        raw: The id as found.

    Returns:
        The canonical eight-digit hex id, or None.
    """
    hexed = _hex8(raw)
    if hexed:
        return hexed
    m = _XBOX_CODE.fullmatch(raw.strip())
    if not m:
        return None
    a, b = m[1].upper()
    return f"{ord(a):02X}{ord(b):02X}{int(m[2]):04X}"


def _gc_wii_disc(raw: str) -> Optional[str]:
    """Normalise a GameCube/Wii id to the hex of its four-character game code.

    Args:
        raw: The id as found: a game id like `GZLE01`, its hex, or a sixteen-digit Wii
            title id like `00010000524D4345`, whose low half is the game code's hex.

    Returns:
        The canonical eight-digit hex id, or None.
    """
    hexed = _hex8(raw)
    if hexed:
        return hexed
    wide = _hex16(raw)
    if wide:
        # A Wii title id is its category in the high half and the game code,
        # in hex, in the low half: 00010000 524D4345 is RMCE.
        return wide[8:]
    value = raw.strip()
    if not _GAME_ID.fullmatch(value):
        return None
    return value[:4].upper().encode("ascii").hex().upper()


def _hex16(raw: str) -> Optional[str]:
    """Normalise a sixteen-digit hex id, dropping any `/` separators.

    Args:
        raw: The id as found.

    Returns:
        The canonical id, or None.
    """
    m = _HEX16.fullmatch(raw.strip().replace("/", ""))
    return m[1].upper() if m else None


_WIIU_TITLE_HIGH = "00050000"
"""The only Wii U title type that keeps a player's save, and so the only one a save folder is named for.

The same value guards `cemu.py`'s own reading of a member's path: a member
under any other high half is refused there, so an id RomM writes with
another one names nothing this emulator could place a save under.
"""


def _wiiu_title(raw: str) -> Optional[str]:
    """Normalise a Wii U title id to the eight-digit low half its save folder is named for.

    Args:
        raw: The id as found: the low half on its own (`1010EC00`), or the
            whole sixteen-digit title id (`00050000101C9400`), whose high
            half must be `00050000`.

    Returns:
        The canonical low half, or None, which includes a sixteen-digit id of
        any other title type: its low half names a demo, an update or DLC,
        none of which keeps a save, so guessing one would compare the session
        against a folder that cannot exist. The bare high half is None too,
        since it is the one eight-digit value that provably names no title.
    """
    wide = _hex16(raw)
    if wide is not None:
        return wide[8:] if wide[:8] == _WIIU_TITLE_HIGH else None
    low = _hex8(raw)
    return None if low == _WIIU_TITLE_HIGH else low


def _dc_product(raw: str) -> Optional[str]:
    """Dreamcast product numbers are not unique enough to compare.

    Args:
        raw: The id as found.

    Returns:
        None, always: identity is not checked for this family.
    """
    return None


NORMALISERS: dict[str, Callable[[str], Optional[str]]] = {
    "ps_serial_dashed": _ps_serial_dashed,
    "ps2_card_dir": lambda raw: raw.strip().upper() or None,
    "ps_serial_nodash": _ps_serial_nodash,
    "hex8": _hex8,
    "xbox": _xbox,
    "gc_wii_disc": _gc_wii_disc,
    "hex16": _hex16,
    "wiiu_title": _wiiu_title,
    "dc_product": _dc_product,
    "scummvm_target": lambda raw: raw.strip().casefold() or None,
}
"""One normaliser per `IdFamily`: each brings every notation of an id to one form."""

IdentityPolicy = Literal["none", "advisory", "strict", "required"]
"""How hard a hook holds a member to the session's id."""


@dataclass(frozen=True)
class IdentitySource:
    """Where an emulator's session identity comes from.

    Attributes:
        family: The id family the session id is normalised in.
        rom_reader: Reads the id off the rom file, or None when it cannot.
        use_save_target: Whether RomM's value is `save_target` rather than `title_id`.
        romm_family: The family RomM's value is written in, when it differs from `family`.
        fall_back_to_title_id: Whether `title_id` stands in for a missing `save_target`.
    """

    family: IdFamily
    rom_reader: Optional[Callable[[Path], Optional[str]]] = None
    use_save_target: bool = False
    romm_family: Optional[IdFamily] = None
    fall_back_to_title_id: bool = False


def resolve_session_identity(
    ctx: ImportCtx,
    *,
    family: IdFamily,
    rom_reader: Optional[Callable[[Path], Optional[str]]] = None,
    use_save_target: bool = False,
    romm_family: Optional[IdFamily] = None,
    fall_back_to_title_id: bool = False,
) -> SessionIdentity:
    """Work out the game id the session runs as, once per preflight.

    The rom wins over RomM: RomM's id is metadata a user can get wrong,
    while the id read off the rom is what the emulator will actually use.

    Args:
        ctx: The launch context; its `memo` caches the answer.
        family: The id family to normalise into.
        rom_reader: Reads the id off `ctx.rom_file`, or None.
        use_save_target: Whether RomM's value is `save_target` rather than `title_id`.
        romm_family: The family RomM's value is written in, when it differs.
        fall_back_to_title_id: Whether `title_id` stands in when RomM sent no
            `save_target`. Only for a platform whose two spellings normalise
            to the same id, since both are read in the one family.

    Returns:
        The identity, with the source it came from.
    """
    key = ("identity", family, rom_reader, use_save_target, romm_family, fall_back_to_title_id)
    cached = ctx.memo.get(key)
    if isinstance(cached, SessionIdentity):
        return cached
    normalise = NORMALISERS[family]
    from_rom: Optional[str] = None
    if rom_reader is not None and ctx.rom_file is not None:
        try:
            raw = rom_reader(ctx.rom_file)
            from_rom = normalise(raw) if raw else None
            if raw and from_rom is None:
                log.info("imports: rom id %r is not a %s id, ignoring it", raw, family)
        except Exception as exc:
            log.warning("imports: could not read an id off %s: %s", ctx.rom_file, exc)
    from_romm: Optional[str] = None
    raw_romm = (ctx.rom.save_target if use_save_target else ctx.rom.title_id) if ctx.rom else None
    if not raw_romm and use_save_target and fall_back_to_title_id and ctx.rom:
        raw_romm = ctx.rom.title_id
    if raw_romm:
        from_romm = NORMALISERS[romm_family or family](raw_romm)
        if from_romm is None:
            log.info("imports: RomM id %r is not a %s id, ignoring it", raw_romm, romm_family or family)
    if from_rom and from_romm and from_rom != from_romm:
        log.warning("imports: the rom says %s but RomM says %s; going with the rom", from_rom, from_romm)
    if from_rom:
        identity = SessionIdentity(from_rom, "rom")
    elif from_romm:
        identity = SessionIdentity(from_romm, "romm")
    else:
        identity = SessionIdentity(None, "none")
    ctx.memo[key] = identity
    return identity


def check_member_identity(
    member: ImportMember,
    member_id: Optional[str],
    session: SessionIdentity,
    *,
    family: IdFamily,
    policy: IdentityPolicy,
    expected: str,
    keyed: bool = True,
) -> Optional[ImportRefusal]:
    """Hold one member's id to the session's, under the hook's policy.

    Args:
        member: The member.
        member_id: The id the hook read off the member, already normalised, or None.
        session: The session's identity.
        family: The id family; `ps2_card_dir` matches by prefix.
        policy: `none` never refuses; `advisory` logs a mismatch; `strict`
            refuses one; `required` also refuses when the session has no id.
        expected: The accepted shape, in words.
        keyed: Whether the layout carries an id at all; an unkeyed layout is
            never refused for lacking one.

    Returns:
        A refusal, or None.
    """
    if policy == "none":
        return None
    if session.value is None:
        if policy == "required":
            return ImportRefusal(
                "identity_unknown",
                member.name,
                expected,
                detail="neither the rom nor RomM says which game this is",
            )
        return None
    if member_id is None:
        if keyed and policy in ("strict", "required"):
            return ImportRefusal(
                "unrecognised_layout", member.name, expected, detail="no game id in the path"
            )
        return None
    if family == "ps2_card_dir":
        matches = member_id.startswith(session.value)
    else:
        matches = member_id == session.value
    if matches:
        return None
    detail = f"member {member_id}, session {session.value} (from {session.source})"
    if policy == "advisory":
        log.info("imports: %s id mismatch, allowed: %s", member.name, detail)
        return None
    detail += override_hint(session)
    return ImportRefusal("identity_mismatch", member.name, expected, detail=detail)


def override_hint(session: SessionIdentity) -> str:
    """Point at the identity override when the session's id is RomM's word.

    An id read off the rom beats RomM's, so the override only helps when
    RomM supplied the id; for any other source the hint is empty.

    Args:
        session: The session's identity.

    Returns:
        The hint, with its leading separator, or an empty string.
    """
    if session.source == "romm":
        return " - fix via PUT /api/roms/{id}/identity if RomM is wrong"
    return ""


def foreign_id(raw: Optional[str], session: Optional[SessionIdentity], family: IdFamily) -> Optional[str]:
    """Name the other game a pushed state belongs to, when it provably is another's.

    The push route has no refusal list, only yes or no. A state is refused
    only when its id normalises and differs from the session's. No id, an
    id that does not normalise, or a session with no id is taken on trust,
    as the route always has.

    Args:
        raw: The id the state gives, in its name or its header, or None.
        session: The session's identity, or None before any activate.
        family: The id family both sides are normalised in.

    Returns:
        The state's canonical id when it differs from the session's, else None.
    """
    if raw is None or session is None or session.value is None:
        return None
    member_id = NORMALISERS[family](raw)
    if member_id is None or member_id == session.value:
        return None
    return member_id


def identity_for(emulator: "Emulator", ctx: ImportCtx) -> SessionIdentity:
    """Resolve the session identity through the emulator's declared source.

    Args:
        emulator: The emulator.
        ctx: The launch context.

    Returns:
        The identity, or `none` when the emulator declares no source.
    """
    source = emulator.identity_source()
    if source is None:
        return SessionIdentity(None, "none")
    return resolve_session_identity(
        ctx,
        family=source.family,
        rom_reader=source.rom_reader,
        use_save_target=source.use_save_target,
        romm_family=source.romm_family,
        fall_back_to_title_id=source.fall_back_to_title_id,
    )


def resolve_activate_identity(
    emulator: "Emulator", rom_file: Optional[Path], rom: Optional[RomRef]
) -> SessionIdentity:
    """Resolve the identity for a launch that ran no preflight.

    Args:
        emulator: The emulator.
        rom_file: The resolved bootable file, or None.
        rom: The activate body's rom, or None.

    Returns:
        The identity.
    """
    ctx = ImportCtx(rom_file=rom_file, rom=rom, memory_card_synced=False, excluded=(), resume_slot=None)
    return identity_for(emulator, ctx)


def _dest_key(dest: PurePosixPath, fold: bool) -> str:
    """Key a destination for collision checks.

    Args:
        dest: The destination.
        fold: Whether the emulator's filesystem ignores case.

    Returns:
        The posix path, casefolded when `fold`.
    """
    text = dest.as_posix()
    return text.casefold() if fold else text


def _v1_kind(emulator: "Emulator", rel: str) -> str:
    """Classify a v1 member, falling back to `save` if the classifier fails.

    Args:
        emulator: The emulator.
        rel: The member path.

    Returns:
        The member's kind.
    """
    try:
        return emulator.save_file_kind(rel)
    except Exception as exc:
        log.warning("imports: could not classify %s, counting it as a save: %s", rel, exc)
        return "save"


def _is_protected(rel: str, spec: ImportSpec) -> bool:
    """Whether a destination matches one of the spec's trusted protected globs.

    Args:
        rel: The destination, as a posix path relative to `save_root`.
        spec: The emulator's spec; its `case_insensitive_dest` folds both sides.

    Returns:
        True when any glob in `spec.protected` matches `rel`.
    """
    if spec.case_insensitive_dest:
        return any(fnmatch.fnmatchcase(rel.casefold(), g.casefold()) for g in spec.protected)
    return any(fnmatch.fnmatchcase(rel, g) for g in spec.protected)


def destination_conflicts(
    plan: Sequence[Placement], archive_paths: frozenset[str], fold: bool
) -> dict[str, str]:
    """Find the import members whose destinations clash with another destination.

    `check_plan` refuses every member named here. An emulator's own plan
    check calls it too, to skip members already refused.

    Two destinations clash when they are the same file, or when one is a
    strict path prefix of the other, so one would need to be both a file and
    a directory. Either way the write would fail after the working slot was
    cleared. v1 members take part as the other side of a clash but are never
    refused here: a v1-only clash is the restore's own business.

    Args:
        plan: The placements; each member's destination and sidecars count.
        archive_paths: The same zip's v1 member names.
        fold: Whether the emulator's filesystem ignores case.

    Returns:
        Each clashing import member's name, mapped to the detail for its refusal.
    """
    # PurePosixPath drops `.` and empty components, so `saves/./a` keys as the
    # `saves/a` it writes to; the owner stays the raw name `refuse` matches on.
    entries = [(_dest_key(PurePosixPath(p), fold), p) for p in archive_paths]
    for placement in plan:
        for dest in (placement.dest, *(d for d, _ in placement.sidecars)):
            entries.append((_dest_key(dest, fold), placement.member.name))
    files: dict[str, str] = {}
    conflicted: dict[str, str] = {}

    def refuse(owners: set[str], detail: str) -> None:
        """Record a clash against every import member among its owners.

        Args:
            owners: The members on either side of the clash.
            detail: Why they clash.
        """
        for name in sorted(owners - archive_paths):
            conflicted.setdefault(name, detail)

    for key, owner in entries:
        first = files.setdefault(key, owner)
        if first != owner:
            refuse({first, owner}, "another member lands on the same file")
    for key, owner in entries:
        parts = key.split("/")
        for i in range(1, len(parts)):
            above = files.get("/".join(parts[:i]))
            if above is not None:
                refuse({above, owner}, "another member lands on a file this destination needs as a directory")
    return conflicted


def _save_tree_refusal(
    dest: PurePosixPath,
    name: str,
    ctx: ImportCtx,
    spec: ImportSpec,
    save_root: Path,
    subtrees: tuple[str, ...],
    link_roots: tuple[Path, ...] = (),
    *,
    sidecar: bool = False,
) -> Optional[ImportRefusal]:
    """Hold one path an import writes to the save tree's rules.

    A sidecar skips only the protected globs. They reserve names like the
    `.rom` owner marker for the broker, and a sidecar is the broker's own write.

    Args:
        dest: A placement's destination, or one of its sidecars, relative to `save_root`.
        name: The member's zip name, for the refusal.
        ctx: The launch context; its `excluded` subtrees travel on their own routes.
        spec: The emulator's spec, for its protected globs.
        save_root: The emulator's save data root.
        subtrees: The emulator's restore subtrees.
        link_roots: The emulator's `link_roots`, for the chain check.
        sidecar: Whether `dest` is a sidecar the broker builds rather than a member.

    Returns:
        `memcard_synced_separately`, `unrecognised_layout`, `protected_destination`
        or `unsafe_path`, or None when the path may be written.
    """
    rel = dest.as_posix()
    if saves.under_subtrees(dest, ctx.excluded):
        return ImportRefusal(
            "memcard_synced_separately",
            name,
            "the memory card through PUT /api/session/memory-card",
            detail="the card travels on its own routes this session",
        )
    if rel in subtrees or not saves.under_subtrees(dest, subtrees):
        return ImportRefusal(
            "unrecognised_layout", name, ", ".join(subtrees), detail=f"{rel} is not inside a save subtree"
        )
    if not sidecar and _is_protected(rel, spec):
        return ImportRefusal("protected_destination", name, None, detail=f"{rel} is emulator configuration")
    if saves.surviving_chain_escapes(save_root, dest, subtrees, link_roots):
        return ImportRefusal(
            "unsafe_path", name, _SAFE_EXPECTED, detail=f"{rel} resolves outside the save root"
        )
    return None


def _is_companion(member: ImportMember, kind_spec: KindSpec) -> bool:
    """Tell whether a member is a companion of its kind, exempt from the kind's count.

    Args:
        member: The member.
        kind_spec: Its kind's spec.

    Returns:
        True when the member's leaf matches one of the kind's companion globs.
    """
    return any(fnmatch.fnmatchcase(member.parts[-1], glob) for glob in kind_spec.companions)


def check_plan(
    plan: Sequence[Placement],
    ctx: ImportCtx,
    spec: ImportSpec,
    emulator: "Emulator",
    *,
    partial: bool = False,
) -> list[ImportRefusal]:
    """Check the placements as a whole: collisions, counts, the save tree, size and units.

    Args:
        plan: Every placement that survived the per-member hooks.
        ctx: The launch context.
        spec: The emulator's spec.
        emulator: The emulator, for its save tree and its own plan check.
        partial: Whether some members were already refused, which makes an
            incomplete unit a likely knock-on rather than a real gap.

    Returns:
        Every refusal; empty when the plan may be written.
    """
    refusals: list[ImportRefusal] = []
    fold = spec.case_insensitive_dest
    conflicted = destination_conflicts(plan, ctx.archive_paths, fold)
    for name, detail in sorted(conflicted.items()):
        refusals.append(
            ImportRefusal("destination_conflict", name, "one member per destination", detail=detail)
        )

    for kind_spec in spec.kinds:
        if kind_spec.max_members is None:
            continue
        mine = [
            p for p in plan if p.member.kind == kind_spec.kind and not _is_companion(p.member, kind_spec)
        ]
        count = len(mine)
        # A member already refused for a collision is not refused a second time.
        over = [p for p in mine if p.member.name not in conflicted]
        if kind_spec.counts_v1:
            count += sum(
                1
                for rel in ctx.archive_paths
                if not _is_protected(rel, spec)
                and _v1_kind(emulator, rel) == kind_spec.kind
            )
        if count > kind_spec.max_members:
            for p in over:
                refusals.append(
                    ImportRefusal(
                        "destination_conflict",
                        p.member.name,
                        f"at most {kind_spec.max_members} {kind_spec.kind} member(s)",
                        detail=f"{count} {kind_spec.kind} members in the archive",
                    )
                )

    subtrees = tuple(emulator.restore_subtrees)
    link_roots = tuple(emulator.link_roots)
    for placement in plan:
        # A sidecar is written just like its destination, so it is held to the
        # same save-tree rules, and the first path that fails refuses the member.
        paths = ((placement.dest, False), *((d, True) for d, _ in placement.sidecars))
        for dest, sidecar in paths:
            refusal = _save_tree_refusal(
                dest,
                placement.member.name,
                ctx,
                spec,
                emulator.save_root,
                subtrees,
                link_roots,
                sidecar=sidecar,
            )
            if refusal is not None:
                refusals.append(refusal)
                break

    total_bytes = ctx.v1_bytes + sum(p.member.size + sum(len(b) for _, b in p.sidecars) for p in plan)
    total_entries = len(ctx.archive_paths) + sum(1 + len(p.sidecars) for p in plan)
    if total_bytes > saves.SAVE_FILE_MAX_BYTES or total_entries > settings.SAVE_FILE_MAX_ENTRIES:
        refusals.append(
            ImportRefusal(
                "too_large",
                None,
                f"at most {saves.SAVE_FILE_MAX_BYTES} bytes in {settings.SAVE_FILE_MAX_ENTRIES} files",
                detail=f"{total_bytes} bytes in {total_entries} files",
            )
        )

    if spec.unit_depth and spec.unit_requires:
        units: dict[tuple[str, ...], list[Placement]] = {}
        for p in plan:
            if spec.unit_subtree is not None and p.dest.parts[0] != spec.unit_subtree:
                continue
            units.setdefault(p.dest.parts[: spec.unit_depth], []).append(p)
        for members in units.values():
            held = {PurePosixPath(*p.dest.parts[spec.unit_depth :]).as_posix() for p in members}
            missing = sorted(spec.unit_requires - held)
            if not missing:
                continue
            detail = f"missing {', '.join(missing)}"
            if partial:
                detail += "; plan is partial: other members were refused"
            for p in members:
                refusals.append(
                    ImportRefusal(
                        "incomplete_unit", p.member.name, ", ".join(sorted(spec.unit_requires)), detail=detail
                    )
                )

    try:
        refusals.extend(emulator.validate_import_plan(list(plan), ctx))
    except MemberReadError as exc:
        # A hook that sniffs member data can find a corrupt one here, after
        # placement; it is the same refusal preflight gives during placement.
        refusals.append(ImportRefusal("unreadable_member", exc.member, READABLE_EXPECTED, detail=str(exc)))
    return refusals


PSP_SAVEDATA_DIR = re.compile(r"[A-Z]{4}[0-9]{5}[A-Za-z0-9_\-]{0,23}", re.ASCII)
"""A PSP `SAVEDATA` folder name: a product code, then up to 23 characters the game picks."""


def suggest_for(
    member: ImportMember, platform: Optional[str], *, current: Optional[str] = None
) -> Optional[str]:
    """Name an emulator on the same platform that would take this member.

    Args:
        member: The refused member.
        platform: The session's platform slug.
        current: The emulator that refused it, which is never suggested.

    Returns:
        An emulator name, or None.
    """
    from .emulators import get_emulator

    suggestion: Optional[str] = None
    if platform == "psp" and any(PSP_SAVEDATA_DIR.fullmatch(p) for p in member.parts[:-1]):
        suggestion = "ppsspp"
    else:
        ra = get_emulator("retroarch")
        if ra is not None:
            ra.platform = platform
            spec = ra.import_spec()
            # Only a numbered or `.auto` slot name points at RetroArch: a `.srm`
            # declared as a state is not one RetroArch would take, and a bare
            # `.state` is Flycast's name as much as RetroArch's.
            is_ra_state = LIBRETRO_STATE_RE.fullmatch(member.parts[-1]) is not None
            if member.kind == "state" and spec.state_channel == "push" and is_ra_state:
                suggestion = "retroarch"
            save = spec.kind("save")
            if member.kind == "save" and save and any(s.endswith(".srm") for s in save.shapes):
                suggestion = "retroarch"
    return None if suggestion == current else suggestion


def refine_refusal(
    refusal: ImportRefusal, member: ImportMember, platform: Optional[str], *, current: Optional[str] = None
) -> ImportRefusal:
    """Sharpen a hook's refusal with what the declared origin and the platform say.

    `replace` re-runs `ImportRefusal.__post_init__`, so a reclassified
    refusal is validated again.

    Args:
        refusal: The hook's refusal.
        member: The member.
        platform: The session's platform slug.
        current: The emulator that refused it.

    Returns:
        The refusal, possibly reclassified or given a suggestion.
    """
    if refusal.reason == "unrecognised_layout" and member.origin in ("emulatorjs", "hardware"):
        note = f"declared origin {member.origin}: this emulator cannot read that source's layout"
        refusal = replace(
            refusal,
            reason="source_incompatible",
            detail=f"{refusal.detail}; {note}" if refusal.detail else note,
        )
    if refusal.reason == "source_incompatible" and refusal.suggest_emulator is None:
        suggestion = suggest_for(member, platform, current=current)
        if suggestion:
            refusal = replace(refusal, suggest_emulator=suggestion)
    return refusal


def preflight(
    emulator: "Emulator",
    view: saves.ArchiveView,
    content: bytes,
    *,
    rom_file: Optional[Path],
    rom: Optional[RomRef],
    memory_card_synced: bool,
    excluded: tuple[str, ...],
    resume_slot: Optional[int],
    v1_refusals: Sequence[ImportRefusal] = (),
) -> PreflightResult:
    """Decide every import member's fate before the working slot is touched.

    Runs the manifest, hygiene, the kind gate, the emulator's hook, the plan
    checks and identity, collecting every refusal instead of stopping at the
    first, so RomM can show the player the whole list at once.

    Args:
        emulator: The emulator, with `platform` already set.
        view: The archive as `saves.read_archive` read it.
        content: The zip bytes, reopened here so hooks can `head()` members.
        rom_file: The resolved bootable file, or None.
        rom: The activate body's rom, or None.
        memory_card_synced: Whether the card travels on its own routes.
        excluded: Subtrees the restore leaves alone.
        resume_slot: The activate's `save.resume_slot`, or None.
        v1_refusals: The same archive's v1 problems, already folded.

    Returns:
        The result. `placements` is empty whenever `refusals` is not.

    Raises:
        TypeError: When `place_import` answers neither a placement nor a refusal.
    """
    spec = emulator.import_spec()
    import_names = [i.filename for i in view.imports]
    counts = Counter(import_names)
    entries, refusals = parse_manifest_v2(view.manifest, import_names, view.manifest_error)
    refusals = [*v1_refusals, *refusals]
    kept = [i for i in view.v1 if not saves.under_subtrees(PurePosixPath(i.filename), excluded)]
    archive_paths = frozenset(i.filename for i in kept)
    v1_bytes = sum(i.file_size for i in kept)
    plan: list[Placement] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        members: list[ImportMember] = []
        refused_twice: set[str] = set()
        for info in view.imports:
            if counts[info.filename] > 1:
                # `zipfile` reads a repeated name as its last entry, so the
                # others would escape every check below.
                if info.filename not in refused_twice:
                    refused_twice.add(info.filename)
                    refusals.append(
                        ImportRefusal(
                            "unsafe_path",
                            info.filename,
                            _SAFE_EXPECTED,
                            detail=f"the archive holds this name {counts[info.filename]} times",
                        )
                    )
                continue
            entry = entries.get(info.filename)
            if entry is None:
                continue
            # Re-fetched from this handle so `head()` reads through the open archive.
            got = normalise_member(
                zf.getinfo(info.filename), entry, zf=zf, max_component_bytes=spec.max_component_bytes
            )
            if isinstance(got, ImportRefusal):
                refusals.append(got)
            else:
                members.append(got)
        ctx = ImportCtx(
            rom_file=rom_file,
            rom=rom,
            memory_card_synced=memory_card_synced,
            excluded=excluded,
            resume_slot=resume_slot,
            members=tuple(members),
            archive_paths=archive_paths,
            v1_bytes=v1_bytes,
        )
        platform = emulator.platform
        for member in members:
            try:
                answer = gate_kind(member, spec, ctx) or emulator.place_import(member, spec, ctx)
            except MemberReadError as exc:
                answer = ImportRefusal("unreadable_member", member.name, READABLE_EXPECTED, detail=str(exc))
            if isinstance(answer, ImportRefusal):
                refusals.append(refine_refusal(answer, member, platform, current=emulator.name))
            elif isinstance(answer, Placement):
                plan.append(answer)
            else:
                raise TypeError(f"{emulator.name}.place_import answered {answer!r} for {member.name}")
        partial = len(plan) < len(view.imports)
        refusals.extend(check_plan(plan, ctx, spec, emulator, partial=partial))
        identity = identity_for(emulator, ctx)
    emulator.import_identity = identity
    unique = tuple(dict.fromkeys(refusals))
    return PreflightResult(() if unique else tuple(plan), unique, identity)
