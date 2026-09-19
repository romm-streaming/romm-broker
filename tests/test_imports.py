"""Declared imports: the pure pieces of `webstation_broker.imports`.

Each test drives one helper directly. The activate wiring is covered in
test_api.py, and the per-emulator hooks in test_emulators.py.
"""

import dataclasses
import importlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Union

import pytest

from webstation_broker import imports, saves

from .conftest import FakeEmulator, corrupt_zip_member, mangle_zip_member


def _zip(members: dict[str, bytes], manifest: Optional[Any] = None) -> bytes:
    """Build an in-memory zip, with a manifest when one is given.

    Args:
        members: Archive member names mapped to their bytes.
        manifest: JSON-serialisable manifest to add, or None for none.

    Returns:
        The zip file contents.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
        if manifest is not None:
            zf.writestr(saves.MANIFEST_NAME, json.dumps(manifest))
    return buf.getvalue()


def test_reasons_is_the_closed_set_of_eighteen() -> None:
    """The refusal codes are closed, and an unknown one cannot be built."""
    assert len(imports.REASONS) == 18
    assert "unreadable_member" in imports.REASONS
    with pytest.raises(ValueError, match="unknown import refusal reason"):
        imports.ImportRefusal("made_up", None, None)


def test_a_refusal_dict_carries_its_docs_anchor() -> None:
    """`docs` is the site-relative anchor, with underscores as hyphens."""
    body = imports.ImportRefusal("identity_mismatch", ".import/state/x", "a state").as_dict()

    assert body == {
        "reason": "identity_mismatch",
        "member": ".import/state/x",
        "expected": "a state",
        "detail": None,
        "suggest_emulator": None,
        "docs": "/docs/api/imports#identity-mismatch",
    }


def test_refusal_body_sorts_and_caps() -> None:
    """Refusals sort by (member, reason), archive-level first, and cap at 200."""
    many = [imports.ImportRefusal("unsafe_path", f".import/save/{i:03d}", None) for i in range(205)]
    many.append(imports.ImportRefusal("too_large", None, None))

    body = imports.refusal_body(many)

    assert body["error"] == "import_refused"
    assert len(body["refusals"]) == imports.REFUSAL_CAP
    assert body["truncated"] == 6
    assert body["refusals"][0]["reason"] == "too_large"
    assert body["refusals"][1]["member"] == ".import/save/000"


def test_fold_v1_problems_maps_each_kind() -> None:
    """Legacy v1 problems fold into refusal codes, keeping the legacy text as detail."""
    plan = saves.V1Plan(
        (),
        0,
        (
            ("../a", "archive member escapes save dir: ../a", "escapes"),
            ("s/b", "archive member resolves outside save dir: s/b", "symlink"),
            ("saves", "archive member names a save subtree: saves", "names_subtree"),
            ("x/c", "archive member outside save subtrees: x/c", "outside"),
            ("s/d", "archive member is encrypted: s/d", "unreadable"),
        ),
    )

    folded = imports.fold_v1_problems("archive exceeds size limit when extracted", plan)

    assert [(r.reason, r.member) for r in folded] == [
        ("too_large", None),
        ("unsafe_path", "../a"),
        ("unsafe_path", "s/b"),
        ("unrecognised_layout", "saves"),
        ("unrecognised_layout", "x/c"),
        ("unreadable_member", "s/d"),
    ]
    assert folded[1].detail == "archive member escapes save dir: ../a"
    assert folded[5].expected == imports.READABLE_EXPECTED


def test_fold_read_problems_maps_members_and_the_archive() -> None:
    """A named problem is `unreadable_member`; an archive-level one is `too_large`."""
    folded = imports.fold_read_problems(
        [
            ("saves/a", "archive member is corrupt: saves/a"),
            (None, "archive exceeds size limit when extracted"),
        ]
    )

    assert [(r.reason, r.member, r.expected, r.detail) for r in folded] == [
        ("unreadable_member", "saves/a", imports.READABLE_EXPECTED, "archive member is corrupt: saves/a"),
        ("too_large", None, None, "archive exceeds size limit when extracted"),
    ]


def test_import_spec_as_dict_is_the_discovery_shape() -> None:
    """The spec serialises to the discovery route's `kinds`, `state_channel` and `card_subtree`."""
    spec = imports.ImportSpec(
        kinds=(imports.KindSpec("state", ("<game>.sNN",), requires_resume_slot=True, max_members=1),),
        state_channel="archive",
        card_subtree="GC",
    )

    assert spec.kind("state") is spec.kinds[0]
    assert spec.kind("save") is None
    assert spec.as_dict() == {
        "kinds": [
            {
                "kind": "state",
                "shapes": ["<game>.sNN"],
                "requires_resume_slot": True,
                "max_members": 1,
            }
        ],
        "state_channel": "archive",
        "card_subtree": "GC",
    }


def test_member_head_reads_at_most_the_cap() -> None:
    """`head` never reads past `HEAD_MAX_BYTES`, however much is asked for."""
    body = _zip({".import/save/big": b"x" * (imports.HEAD_MAX_BYTES + 10)})
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        info = zf.getinfo(".import/save/big")
        member = imports.ImportMember(
            ".import/save/big", "save", "unknown", PurePosixPath("big"), ("big",),
            info.file_size, info, zf,
        )
        assert member.head(10) == b"x" * 10
        assert len(member.head(10**9)) == imports.HEAD_MAX_BYTES


def test_member_head_turns_a_failed_read_into_member_read_error(caplog: pytest.LogCaptureFixture) -> None:
    """A read that fails raises `MemberReadError`, whatever `zipfile` raised underneath.

    Args:
        caplog: Pytest's log capture.
    """
    body = corrupt_zip_member(_zip({".import/save/a": b"x" * 16}), ".import/save/a")
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        info = zf.getinfo(".import/save/a")
        member = imports.ImportMember(
            ".import/save/a", "save", "unknown", PurePosixPath("a"), ("a",), info.file_size, info, zf
        )
        with (
            caplog.at_level("WARNING", logger="webstation_broker.imports"),
            pytest.raises(imports.MemberReadError, match="the member's data is corrupt"),
        ):
            member.head(64)

    assert ".import/save/a could not be read" in caplog.text


def test_rom_ref_copies_the_body_fields() -> None:
    """`RomRef.from_body` copies what imports needs, and nothing ties it to the api module."""
    from webstation_broker.api import RomIn

    ref = imports.RomRef.from_body(
        RomIn(id=4, name="G", platform="ps2", path="/r", title_id="SLUS-20001")
    )

    assert ref == imports.RomRef(4, "G", "ps2", "SLUS-20001", None, None)


# ── manifest v2 ────────────────────────────────────────────────────────


def _v2(*files: dict[str, Any]) -> dict[str, Any]:
    """Build a version 2 manifest.

    Args:
        *files: The `files` entries.

    Returns:
        The manifest.
    """
    return {"version": 2, "created_at": 0, "files": list(files)}


def test_manifest_v2_declares_each_member() -> None:
    """Each `.import/` entry maps its member to a kind and origin; v1 entries are ignored."""
    entries, refusals = imports.parse_manifest_v2(
        _v2(
            {"path": "saves/a.srm", "kind": "save"},
            {"path": ".import/save/a.srm", "kind": "save", "origin": "standalone"},
            {"path": ".import/state/b.p2s", "kind": "state", "origin": "martian"},
        ),
        [".import/save/a.srm", ".import/state/b.p2s"],
    )

    assert refusals == []
    assert entries[".import/save/a.srm"] == imports.ManifestEntry(".import/save/a.srm", "save", "standalone")
    assert entries[".import/state/b.p2s"].origin == "unknown"


def test_manifest_v2_is_skipped_without_imports() -> None:
    """An archive without imports is never checked against v2 rules."""
    assert imports.parse_manifest_v2({"version": 1}, []) == ({}, [])


@pytest.mark.parametrize(
    ("manifest", "error"),
    [
        (None, "archive has no manifest"),
        ([1, 2], None),
        ({"version": 1, "files": []}, None),
        ({"version": 2, "files": "nope"}, None),
    ],
)
def test_an_unusable_manifest_refuses_the_whole_import(manifest: Any, error: Optional[str]) -> None:
    """No usable v2 manifest gives one archive-level `manifest_invalid`.

    Args:
        manifest: The parsed manifest.
        error: The manifest error `read_archive` recorded, if any.
    """
    entries, refusals = imports.parse_manifest_v2(manifest, [".import/save/a"], error)

    assert entries == {}
    assert [(r.reason, r.member) for r in refusals] == [("manifest_invalid", None)]


@pytest.mark.parametrize(
    ("files", "names", "member"),
    [
        ([], [".import/save/a"], ".import/save/a"),
        ([{"path": ".import/save/a", "kind": "state"}], [".import/save/a"], ".import/save/a"),
        ([{"path": ".import/save/a", "kind": "bios"}], [".import/save/a"], ".import/save/a"),
        ([{"path": ".import/save/gone", "kind": "save"}], [], ".import/save/gone"),
        (
            [{"path": ".import/save/a", "kind": "save"}, {"path": ".import/save/a", "kind": "save"}],
            [".import/save/a"],
            ".import/save/a",
        ),
    ],
    ids=["undeclared", "segment-mismatch", "unknown-kind", "missing-member", "duplicate"],
)
def test_manifest_v2_refuses_a_bad_declaration_once(
    files: list[dict[str, Any]], names: list[str], member: str
) -> None:
    """Each bad declaration is one `manifest_invalid` for its member, never two.

    Args:
        files: The manifest's `files` entries.
        names: The archive's `.import/` member names.
        member: The member the refusal must name.
    """
    entries, refusals = imports.parse_manifest_v2(_v2(*files), names or [".import/save/other"])

    assert [(r.reason, r.member) for r in refusals if r.member == member] == [
        ("manifest_invalid", member)
    ]
    assert member not in entries


def test_a_non_object_entry_is_refused_at_archive_level() -> None:
    """A `files` entry that is not an object cannot name a member."""
    _, refusals = imports.parse_manifest_v2(
        _v2("x", {"path": ".import/save/a", "kind": "save"}), [".import/save/a"]
    )

    assert [(r.reason, r.member) for r in refusals] == [("manifest_invalid", None)]


@pytest.mark.parametrize(
    "extra",
    [{"origin": []}, {"origin": {}}, {}],
    ids=["list-origin", "dict-origin", "no-origin"],
)
def test_manifest_v2_reads_an_odd_or_missing_origin_as_unknown(extra: dict[str, Any]) -> None:
    """An origin that is not a known string, or no origin at all, reads as `unknown` and is not refused.

    Args:
        extra: The origin field to add to the entry, if any.
    """
    entries, refusals = imports.parse_manifest_v2(
        _v2({"path": ".import/save/a", "kind": "save", **extra}), [".import/save/a"]
    )

    assert refusals == []
    assert entries[".import/save/a"].origin == "unknown"


def test_the_logged_import_block_is_bounded(caplog: pytest.LogCaptureFixture) -> None:
    """RomM's free-form `import` block is logged, but never more than 200 characters of it."""
    manifest = {"version": 2, "files": [], "import": {"note": "x" * 5000}}

    with caplog.at_level("INFO", logger="webstation_broker.imports"):
        imports.parse_manifest_v2(manifest, [".import/save/a"])

    [line] = [r.getMessage() for r in caplog.records if "import block" in r.getMessage()]
    assert "{'note': 'xxx" in line
    assert len(line) < 300


# ── hygiene ────────────────────────────────────────────────────────────


def _info(name: str, *, utf8: bool = True, size: int = 4) -> zipfile.ZipInfo:
    """Build a zip entry without an archive behind it.

    Args:
        name: The entry name.
        utf8: Whether the entry carries the zip UTF-8 flag.
        size: The uncompressed size to record.

    Returns:
        The entry.
    """
    info = zipfile.ZipInfo(name)
    info.file_size = size
    if utf8:
        info.flag_bits |= 0x800
    return info


def _entry(name: str, kind: str = "save") -> imports.ManifestEntry:
    """Declare `name` as `kind` with an unknown origin.

    Args:
        name: The member name.
        kind: The declared kind.

    Returns:
        The declaration.
    """
    return imports.ManifestEntry(name, kind, "unknown")


def test_normalise_member_keeps_the_users_own_path() -> None:
    """The path below `.import/<kind>/` becomes `rel`, component for component."""
    name = ".import/save/BASLUS-20001ALL/icon.sys"
    member = imports.normalise_member(_info(name), _entry(name), zf=None)

    assert isinstance(member, imports.ImportMember)
    assert member.rel == PurePosixPath("BASLUS-20001ALL/icon.sys")
    assert member.parts == ("BASLUS-20001ALL", "icon.sys")
    assert member.size == 4


@pytest.mark.parametrize(
    "tail",
    [
        "a\\b",
        "a:b",
        "a\x01b",
        "a\x7fb",
        "",
        "a//b",
        "a/./b",
        "a/../b",
        ".DS_Store",
        "dir/.hidden",
        "__MACOSX/a",
        "x" * 256,
    ],
)
def test_normalise_member_refuses_unsafe_paths(tail: str) -> None:
    """Every hygiene rule answers `unsafe_path`.

    Args:
        tail: The path below `.import/save/`.
    """
    name = f".import/save/{tail}"

    refusal = imports.normalise_member(_info(name), _entry(name), zf=None)

    assert isinstance(refusal, imports.ImportRefusal)
    assert refusal.reason == "unsafe_path"
    assert refusal.member == name


def test_normalise_member_refuses_non_ascii_without_the_utf8_flag() -> None:
    """A non-ASCII name only counts when the zip says it is UTF-8."""
    name = ".import/save/café.srm"

    flagged = imports.normalise_member(_info(name), _entry(name), zf=None)
    unflagged = imports.normalise_member(_info(name, utf8=False), _entry(name), zf=None)

    assert isinstance(flagged, imports.ImportMember)
    assert isinstance(unflagged, imports.ImportRefusal) and unflagged.reason == "unsafe_path"


@pytest.mark.parametrize(
    ("char", "detail"),
    [
        ("\x80", "control character in name"),
        ("\x85", "control character in name"),
        ("\x9f", "control character in name"),
        ("\u061c", "bidirectional control character in name"),
        ("\u200e", "bidirectional control character in name"),
        ("\u200f", "bidirectional control character in name"),
        ("\u202a", "bidirectional control character in name"),
        ("\u202e", "bidirectional control character in name"),
        ("\u2066", "bidirectional control character in name"),
        ("\u2069", "bidirectional control character in name"),
    ],
)
def test_normalise_member_refuses_c1_and_bidi_characters(char: str, detail: str) -> None:
    """A C1 control or a bidi control is refused, so a refusal list always shows the name it holds.

    Args:
        char: The character to embed.
        detail: The refusal's expected detail.
    """
    name = f".import/save/a{char}b.srm"

    refusal = imports.normalise_member(_info(name), _entry(name), zf=None)

    assert isinstance(refusal, imports.ImportRefusal)
    assert (refusal.reason, refusal.detail) == ("unsafe_path", detail)


def test_normalise_member_keeps_printable_latin1() -> None:
    """The check stops at U+009F: a no-break space or an accented letter is a plain name."""
    name = ".import/save/a\xa0\xe9.srm"

    assert isinstance(imports.normalise_member(_info(name), _entry(name), zf=None), imports.ImportMember)


def test_normalise_member_honours_a_tighter_component_limit() -> None:
    """An emulator with a short filesystem limit (xemu's 42) can lower it."""
    name = ".import/save/" + "x" * 43

    refusal = imports.normalise_member(_info(name), _entry(name), zf=None, max_component_bytes=42)

    assert isinstance(refusal, imports.ImportRefusal) and refusal.reason == "unsafe_path"


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("flag_bits", 0x801, "the member is encrypted"),
        ("compress_type", 99, "the member uses unsupported compression method 99"),
    ],
    ids=["encrypted", "compression"],
)
def test_normalise_member_refuses_a_member_that_cannot_be_read(field: str, value: int, detail: str) -> None:
    """A member whose header says the write would fail is refused before the clear.

    Args:
        field: The `ZipInfo` attribute to break.
        value: The value to give it.
        detail: The refusal's expected detail.
    """
    name = ".import/save/a.srm"
    info = _info(name)
    setattr(info, field, value)

    refusal = imports.normalise_member(info, _entry(name), zf=None)

    assert isinstance(refusal, imports.ImportRefusal)
    assert (refusal.reason, refusal.detail) == ("unreadable_member", detail)
    assert refusal.expected == imports.READABLE_EXPECTED


def test_normalise_member_ignores_the_date_a_placed_member_never_uses() -> None:
    """A placed member is stamped with the write time, so a bad zip date is harmless."""
    name = ".import/save/a.srm"
    info = _info(name)
    info.date_time = (1980, 0, 0, 0, 0, 0)

    assert isinstance(imports.normalise_member(info, _entry(name), zf=None), imports.ImportMember)


# ── the kind gate and the placement helpers ────────────────────────────


def _member(tail: str, kind: str = "save", size: int = 4, origin: str = "unknown") -> imports.ImportMember:
    """Build a hygienic member without an archive behind it.

    Args:
        tail: The path below `.import/<kind>/`.
        kind: The declared kind.
        size: The recorded size.
        origin: The declared origin.

    Returns:
        The member.
    """
    name = f".import/{kind}/{tail}"
    member = imports.normalise_member(
        _info(name, size=size), imports.ManifestEntry(name, kind, origin), zf=None
    )
    assert isinstance(member, imports.ImportMember)
    return member


def _ctx(**kwargs: Any) -> imports.ImportCtx:
    """Build a launch context with nothing set but what the test passes.

    Args:
        **kwargs: Fields to set.

    Returns:
        The context.
    """
    base: dict[str, Any] = {
        "rom_file": None,
        "rom": None,
        "memory_card_synced": False,
        "excluded": (),
        "resume_slot": None,
    }
    base.update(kwargs)
    return imports.ImportCtx(**base)


def test_gate_kind_answers_for_a_kind_the_spec_lacks() -> None:
    """No spec for the kind refuses it, naming the kinds that are taken."""
    spec = imports.ImportSpec(kinds=(imports.KindSpec("memcard", ("a card",)),))

    refusal = imports.gate_kind(_member("a.srm"), spec, _ctx())

    assert refusal is not None
    assert (refusal.reason, refusal.expected) == ("kind_not_accepted", "memcard")
    empty = imports.gate_kind(_member("a.srm"), imports.ImportSpec(), _ctx())
    assert empty is not None and empty.expected == "no imports"


def test_gate_kind_sends_states_to_the_push_route() -> None:
    """A push-channel emulator refuses archive states with `state_uses_push`."""
    refusal = imports.gate_kind(
        _member("a.p2s", kind="state"), imports.ImportSpec(state_channel="push"), _ctx()
    )

    assert refusal is not None and refusal.reason == "state_uses_push"


def test_gate_kind_requires_a_resume_slot_when_the_kind_does() -> None:
    """An archive-channel state without `resume_slot` is refused; with one it passes."""
    spec = imports.ImportSpec(
        kinds=(imports.KindSpec("state", ("s",), requires_resume_slot=True),), state_channel="archive"
    )

    refusal = imports.gate_kind(_member("a.s", kind="state"), spec, _ctx())

    assert refusal is not None and refusal.reason == "resume_slot_required"
    assert imports.gate_kind(_member("a.s", kind="state"), spec, _ctx(resume_slot=1)) is None


_SRM = re.compile(r"[^/]+\.srm", re.I)


def test_place_single_file_renames_into_the_subtree() -> None:
    """A matching single file lands under the subtree with the renamer's name."""
    dest = imports.place_single_file(
        _member("wrap/Game (USA).srm"),
        subtree="saves",
        pattern=_SRM,
        rename=lambda _: "Game.srm",
        expected="<game>.srm",
        allow_wrappers=("wrap",),
    )

    assert dest == PurePosixPath("saves/Game.srm")


@pytest.mark.parametrize(
    ("tail", "size", "reason"),
    [
        ("a/b/Game.srm", 4, "unrecognised_layout"),
        ("Game.state3", 4, "source_incompatible"),
        ("Game.state.auto", 4, "source_incompatible"),
        ("Game.sav", 4, "unrecognised_layout"),
        ("Game.srm", 0, "incomplete_unit"),
    ],
)
def test_place_single_file_refusals(tail: str, size: int, reason: str) -> None:
    """Nested, libretro-state, mismatched and empty members are each refused.

    Args:
        tail: The path below `.import/save/`.
        size: The member's size.
        reason: The expected refusal.
    """
    result = imports.place_single_file(
        _member(tail, size=size),
        subtree="saves",
        pattern=re.compile(r".+\.(srm|state\d+|state\.auto)"),
        rename=lambda n: n,
        expected="<game>.srm",
        nonempty=True,
    )

    assert isinstance(result, imports.ImportRefusal)
    assert result.reason == reason


def test_place_single_file_rechecks_the_renamed_name() -> None:
    """A renamer that produces an unsafe name is caught."""
    result = imports.place_single_file(
        _member("Game.srm"), subtree="saves", pattern=_SRM, rename=lambda _: ".hidden", expected="x"
    )

    assert isinstance(result, imports.ImportRefusal) and result.reason == "unsafe_path"


_SERIAL = re.compile(r"[A-Z]{4}\d{5}")


def test_match_anchored_strips_one_wrapper_and_matches_each_level() -> None:
    """The first matching wrapper wins, each id level must fullmatch, and a tail must remain."""
    match = imports.match_anchored(
        ("PSP", "SAVEDATA", "ULUS10064", "DATA.BIN"),
        wrappers=(("PSP", "SAVEDATA"), ("SAVEDATA",), ()),
        levels=(_SERIAL,),
    )

    assert match == imports.AnchoredMatch(("PSP", "SAVEDATA"), ("ULUS10064",), ("DATA.BIN",))
    assert imports.match_anchored(("ULUS10064",), wrappers=((),), levels=(_SERIAL,)) is None
    assert imports.match_anchored(("x", "ULUS10064", "a"), wrappers=((),), levels=(_SERIAL,)) is None


def test_match_anchored_never_falls_through_to_a_later_wrapper() -> None:
    """Once a wrapper matches, a failed id level is a miss, not a retry without it."""
    assert (
        imports.match_anchored(
            ("SAVEDATA", "SAVEDATA", "x"), wrappers=(("SAVEDATA",), ()), levels=(_SERIAL,)
        )
        is None
    )


def test_build_dest_joins_or_refuses() -> None:
    """Rewritten ids and the tail join under the subtree, and unsafe ids are refused."""
    member = _member("x/y")

    assert imports.build_dest(
        "SAVEDATA", ("ULUS10064",), ("DATA.BIN",), member=member, expected="e"
    ) == PurePosixPath("SAVEDATA/ULUS10064/DATA.BIN")
    refused = imports.build_dest("SAVEDATA", ("..",), ("a",), member=member, expected="e")
    assert isinstance(refused, imports.ImportRefusal) and refused.reason == "unsafe_path"


def test_place_single_file_refuses_when_the_renamer_rejects_the_name() -> None:
    """A renamer returning None is an `unrecognised_layout`, not a crash."""
    result = imports.place_single_file(
        _member("Game.srm"), subtree="saves", pattern=_SRM, rename=lambda _: None, expected="x"
    )

    assert isinstance(result, imports.ImportRefusal)
    assert (result.reason, result.detail) == ("unrecognised_layout", "name not recognised by the emulator")


@pytest.mark.parametrize("renamed", ["a/b", "/etc/passwd"])
def test_place_single_file_refuses_a_renamed_path(renamed: str) -> None:
    """A renamer that returns a path, relative or absolute, cannot escape the subtree.

    Args:
        renamed: What the renamer returns.
    """
    result = imports.place_single_file(
        _member("Game.srm"), subtree="saves", pattern=_SRM, rename=lambda _: renamed, expected="x"
    )

    assert isinstance(result, imports.ImportRefusal) and result.reason == "unsafe_path"


def test_place_single_file_refuses_a_trailing_newline() -> None:
    """`fullmatch` rejects a name the pattern only matches up to a trailing newline.

    Hygiene already refuses the control character, so the member is built past
    it: this pins the placement check on its own.
    """
    member = dataclasses.replace(_member("Game.srm"), parts=("Game.srm\n",))

    result = imports.place_single_file(
        member, subtree="saves", pattern=_SRM, rename=lambda n: n, expected="x"
    )

    assert isinstance(result, imports.ImportRefusal) and result.reason == "unrecognised_layout"


def test_place_single_file_refuses_a_member_that_is_only_the_wrapper() -> None:
    """A file named like the wrapper is not stripped to nothing, and is refused."""
    result = imports.place_single_file(
        _member("wrap"),
        subtree="saves",
        pattern=_SRM,
        rename=lambda n: n,
        expected="x",
        allow_wrappers=("wrap",),
    )

    assert isinstance(result, imports.ImportRefusal) and result.reason == "unrecognised_layout"


@pytest.mark.parametrize("tail", ["a/b", ""])
def test_build_dest_refuses_an_unsafe_tail_component(tail: str) -> None:
    """A tail component holding a slash, or empty, is `unsafe_path`.

    Args:
        tail: The tail component.
    """
    refused = imports.build_dest("SAVEDATA", ("ULUS10064",), (tail,), member=_member("x/y"), expected="e")

    assert isinstance(refused, imports.ImportRefusal) and refused.reason == "unsafe_path"


@pytest.mark.parametrize("module_name", ["flycast", "duckstation"])
def test_owner_marker_sidecar_matches_the_marker_the_emulator_writes(
    tmp_path: Path, module_name: str
) -> None:
    """The sidecar is byte-identical to the marker the emulator writes on exit.

    The rom is reached through a symlink, so the test also pins that both
    sides record the resolved path, not the path the caller passed.

    Args:
        tmp_path: Pytest's per-test directory.
        module_name: The emulator module whose marker writer is compared.
    """
    module = importlib.import_module(f"webstation_broker.emulators.{module_name}")
    rom = tmp_path / "roms" / "Game (USA).cue"
    rom.parent.mkdir()
    rom.write_bytes(b"disc")
    link = tmp_path / "link.cue"
    link.symlink_to(rom)
    state = tmp_path / "state.bin"

    module._write_owner_marker(state, link)
    path, data = imports.owner_marker_sidecar(PurePosixPath("sub/state.bin"), link)

    assert path == PurePosixPath("sub/state.bin.rom")
    assert data == (tmp_path / "state.bin.rom").read_bytes()
    assert data == f"{rom.resolve()}\n".encode()


class _UnresolvablePath(type(Path())):
    """A path whose `resolve` fails, as it does on a symlink loop."""

    def resolve(self, strict: bool = False) -> Path:
        """Fail the way a symlink loop does.

        Args:
            strict: Unused; matches `Path.resolve`.

        Raises:
            OSError: Always.
        """
        raise OSError("loop")


def test_owner_marker_sidecar_falls_back_to_the_raw_path(caplog: pytest.LogCaptureFixture) -> None:
    """A rom path that cannot be resolved is recorded as given, with a warning.

    Args:
        caplog: Pytest's log capture.
    """
    path, data = imports.owner_marker_sidecar(PurePosixPath("a.state"), _UnresolvablePath("/roms/a.cdi"))

    assert (path, data) == (PurePosixPath("a.state.rom"), b"/roms/a.cdi\n")
    assert "could not resolve /roms/a.cdi" in caplog.text


# ── identity ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("family", "raw", "canonical"),
    [
        ("ps_serial_dashed", "slus_200.01", "SLUS-20001"),
        ("ps_serial_dashed", "SLUS20001", "SLUS-20001"),
        ("ps_serial_dashed", "not a serial", None),
        ("ps2_card_dir", "baslus-20001all", "BASLUS-20001ALL"),
        ("ps_serial_nodash", "ULUS-10064", "ULUS10064"),
        ("hex8", "0x0100abcd", "0100ABCD"),
        ("hex8", "0100ABC", None),
        ("xbox", "MS-100", "4D530064"),
        ("xbox", "4d530064", "4D530064"),
        ("xbox", "MS-\u0661\u0660\u0660", None),
        ("gc_wii_disc", "GZLE01", "475A4C45"),
        ("gc_wii_disc", "0x475a4c45", "475A4C45"),
        ("hex16", "0100/0000/0000/1000", "0100000000001000"),
        ("dc_product", "T-8101N", None),
        ("scummvm_target", "Monkey1", "monkey1"),
    ],
)
def test_normalisers_bring_each_notation_to_one_form(family: str, raw: str, canonical: Optional[str]) -> None:
    """Each family's notations all normalise to one canonical id, or to None.

    Args:
        family: The id family.
        raw: The id as some source writes it.
        canonical: What it must normalise to.
    """
    assert imports.NORMALISERS[family](raw) == canonical


def _rom(title_id: Optional[str] = None, save_target: Optional[str] = None) -> imports.RomRef:
    """Build a rom reference carrying RomM's ids.

    Args:
        title_id: RomM's game id.
        save_target: RomM's save-target name.

    Returns:
        The reference.
    """
    return imports.RomRef(5, "Game", "ps2", title_id, save_target)


def test_session_identity_prefers_the_rom_over_romm(caplog: pytest.LogCaptureFixture) -> None:
    """The id read off the rom wins over RomM's, and a disagreement is logged."""
    ctx = _ctx(rom_file=Path("/roms/g.iso"), rom=_rom("SLUS-20002"))

    with caplog.at_level("WARNING", logger="webstation_broker.imports"):
        identity = imports.resolve_session_identity(
            ctx, family="ps_serial_dashed", rom_reader=lambda _: "SLUS_200.01"
        )

    assert identity == imports.SessionIdentity("SLUS-20001", "rom")
    assert "SLUS-20002" in caplog.text


def test_session_identity_falls_back_to_romm_then_none() -> None:
    """With no id on the rom RomM's is used; with neither, the source is `none`."""
    romm = imports.resolve_session_identity(
        _ctx(rom=_rom(save_target="baslus-20001all")), family="ps2_card_dir", use_save_target=True
    )
    nothing = imports.resolve_session_identity(_ctx(), family="ps_serial_dashed")

    assert romm == imports.SessionIdentity("BASLUS-20001ALL", "romm")
    assert nothing == imports.SessionIdentity(None, "none")


def test_a_rom_reader_that_raises_reads_as_no_id() -> None:
    """A reader that throws is logged and treated as finding nothing."""

    def boom(_: Path) -> Optional[str]:
        """Fail the way a corrupt image does.

        Args:
            _: The rom file.

        Raises:
            OSError: Always.
        """
        raise OSError("bad image")

    identity = imports.resolve_session_identity(
        _ctx(rom_file=Path("/r"), rom=_rom("SLUS-20001")), family="ps_serial_dashed", rom_reader=boom
    )

    assert identity == imports.SessionIdentity("SLUS-20001", "romm")


def test_session_identity_is_memoised_per_preflight() -> None:
    """The rom is read once per preflight, whoever asks."""
    reads: list[Path] = []
    ctx = _ctx(rom_file=Path("/r"))

    def reader(path: Path) -> Optional[str]:
        """Record the read and answer a serial.

        Args:
            path: The rom file.

        Returns:
            A serial.
        """
        reads.append(path)
        return "SLUS-20001"

    for _ in range(3):
        imports.resolve_session_identity(ctx, family="ps_serial_dashed", rom_reader=reader)

    assert reads == [Path("/r")]


@pytest.mark.parametrize(
    ("member_id", "session", "policy", "keyed", "reason"),
    [
        ("SLUS-20001", imports.SessionIdentity("SLUS-20001", "rom"), "strict", True, None),
        ("SLUS-20002", imports.SessionIdentity("SLUS-20001", "rom"), "strict", True, "identity_mismatch"),
        ("SLUS-20002", imports.SessionIdentity("SLUS-20001", "rom"), "advisory", True, None),
        ("SLUS-20002", imports.SessionIdentity("SLUS-20001", "rom"), "none", True, None),
        ("SLUS-20001", imports.SessionIdentity(None, "none"), "required", True, "identity_unknown"),
        ("SLUS-20001", imports.SessionIdentity(None, "none"), "strict", True, None),
        (None, imports.SessionIdentity("SLUS-20001", "rom"), "strict", True, "unrecognised_layout"),
        (None, imports.SessionIdentity("SLUS-20001", "rom"), "strict", False, None),
    ],
)
def test_check_member_identity_applies_the_policy(
    member_id: Optional[str],
    session: imports.SessionIdentity,
    policy: str,
    keyed: bool,
    reason: Optional[str],
) -> None:
    """Each policy refuses exactly what it says it does.

    Args:
        member_id: The id read off the member, or None.
        session: The session's identity.
        policy: The emulator's policy.
        keyed: Whether the layout carries an id at all.
        reason: The expected refusal, or None for none.
    """
    refusal = imports.check_member_identity(
        _member("x"), member_id, session, family="ps_serial_dashed", policy=policy, expected="e", keyed=keyed
    )

    assert (refusal.reason if refusal else None) == reason


def test_a_ps2_card_dir_matches_by_prefix() -> None:
    """A PS2 card dir is the serial plus a suffix, so it matches by prefix."""
    session = imports.SessionIdentity("BASLUS-20001", "romm")

    assert imports.check_member_identity(
        _member("x"), "BASLUS-20001ALL", session, family="ps2_card_dir", policy="strict", expected="e"
    ) is None


def test_a_mismatch_against_romm_says_how_to_fix_romm() -> None:
    """When RomM supplied the id, the refusal names the route that corrects it."""
    refusal = imports.check_member_identity(
        _member("x"),
        "SLUS-20002",
        imports.SessionIdentity("SLUS-20001", "romm"),
        family="ps_serial_dashed",
        policy="strict",
        expected="e",
    )

    assert refusal is not None and refusal.detail is not None
    assert "PUT /api/roms/" in refusal.detail


class _Source:
    """A stand-in emulator with an identity source and nothing else."""

    def __init__(self, source: Optional[imports.IdentitySource]) -> None:
        """Keep the source.

        Args:
            source: What `identity_source` answers.
        """
        self._source = source

    def identity_source(self) -> Optional[imports.IdentitySource]:
        """Answer the source.

        Returns:
            The source, or None.
        """
        return self._source


def test_resolve_activate_identity_uses_the_emulators_source() -> None:
    """An emulator without a source runs as `none`; one with a source is resolved."""
    none = imports.resolve_activate_identity(_Source(None), None, _rom("SLUS-20001"))  # type: ignore[arg-type]
    some = imports.resolve_activate_identity(
        _Source(imports.IdentitySource("ps_serial_dashed")), None, _rom("SLUS-20001")  # type: ignore[arg-type]
    )

    assert none == imports.SessionIdentity(None, "none")
    assert some == imports.SessionIdentity("SLUS-20001", "romm")


def test_the_memo_does_not_serve_a_readerless_answer_to_a_reader() -> None:
    """A lookup without a reader never answers for a later one that has a reader."""
    ctx = _ctx(rom_file=Path("/r"), rom=_rom("SLUS-20002"))

    first = imports.resolve_session_identity(ctx, family="ps_serial_dashed")
    second = imports.resolve_session_identity(
        ctx, family="ps_serial_dashed", rom_reader=lambda _: "SLUS-20001"
    )

    assert first == imports.SessionIdentity("SLUS-20002", "romm")
    assert second == imports.SessionIdentity("SLUS-20001", "rom")


def test_a_rom_reader_answering_a_non_string_reads_as_no_id(caplog: pytest.LogCaptureFixture) -> None:
    """A reader that answers bytes is logged and treated as finding nothing, not a crash.

    Args:
        caplog: Captures the module's log.
    """
    with caplog.at_level("WARNING", logger="webstation_broker.imports"):
        identity = imports.resolve_session_identity(
            _ctx(rom_file=Path("/r"), rom=_rom("SLUS-20001")),
            family="ps_serial_dashed",
            rom_reader=lambda _: b"SLUS-20001",  # type: ignore[arg-type,return-value]
        )

    assert identity == imports.SessionIdentity("SLUS-20001", "romm")
    assert "/r" in caplog.text


def test_a_rom_id_outside_the_family_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """An id read off the rom that is not in the family is logged and ignored.

    Args:
        caplog: Captures the module's log.
    """
    with caplog.at_level("INFO", logger="webstation_broker.imports"):
        identity = imports.resolve_session_identity(
            _ctx(rom_file=Path("/r")), family="ps_serial_dashed", rom_reader=lambda _: "not a serial"
        )

    assert identity == imports.SessionIdentity(None, "none")
    assert "not a serial" in caplog.text


@pytest.mark.parametrize(
    ("family", "raw"),
    [
        ("ps_serial_dashed", "SLUS-٢0001"),
        ("ps_serial_dashed", "ſlus-20001"),
        ("ps_serial_nodash", "ULUS٢0064"),
    ],
)
def test_serials_are_read_as_ascii_only(family: str, raw: str) -> None:
    """Non-ASCII digits and letters that fold to ASCII are not a serial.

    Args:
        family: The id family.
        raw: A near-serial with one non-ASCII character.
    """
    assert imports.NORMALISERS[family](raw) is None


# ── check_plan ─────────────────────────────────────────────────────────


class _PlanEmu:
    """The emulator surface `check_plan` reads, and nothing more."""

    def __init__(self, root: Path, subtrees: tuple[str, ...] = ("saves", "states")) -> None:
        """Root the stand-in.

        Args:
            root: Its `save_root`.
            subtrees: Its `restore_subtrees`.
        """
        self.save_root = root
        self.restore_subtrees = subtrees
        self.validated: list[int] = []

    def save_file_kind(self, rel: str) -> str:
        """Classify by first component.

        Args:
            rel: The member path.

        Returns:
            `state` under `states/`, else `save`.
        """
        return "state" if rel.startswith("states/") else "save"

    def validate_import_plan(
        self, plan: list[imports.Placement], ctx: imports.ImportCtx
    ) -> list[imports.ImportRefusal]:
        """Record the call and refuse nothing.

        Args:
            plan: The placements.
            ctx: The launch context.

        Returns:
            No refusals.
        """
        self.validated.append(len(plan))
        return []


def _placed(tail: str, dest: str, kind: str = "save", size: int = 4) -> imports.Placement:
    """Place a member at `dest`.

    Args:
        tail: The member path below `.import/<kind>/`.
        dest: The destination.
        kind: The declared kind.
        size: The member's size.

    Returns:
        The placement.
    """
    return imports.Placement(_member(tail, kind=kind, size=size), PurePosixPath(dest))


def _check(
    tmp_path: Path, plan: list[imports.Placement], spec: Optional[imports.ImportSpec] = None, **ctx: Any
) -> list[tuple[str, Optional[str]]]:
    """Run `check_plan` and reduce each refusal to `(reason, member)`.

    Args:
        tmp_path: The per-test temporary directory; the save root lives under it.
        plan: The placements.
        spec: The spec, or an accepting default.
        **ctx: Launch-context fields.

    Returns:
        The refusals, reduced.
    """
    spec = spec or imports.ImportSpec(kinds=(imports.KindSpec("save", ("x",)),))
    return [
        (r.reason, r.member)
        for r in imports.check_plan(plan, _ctx(**ctx), spec, _PlanEmu(tmp_path / "root"))  # type: ignore[arg-type]
    ]


def test_a_clean_plan_passes_and_reaches_the_hook(tmp_path: Path) -> None:
    """A plan with nothing wrong has no refusals, and the emulator's own check runs."""
    emu = _PlanEmu(tmp_path / "root")
    spec = imports.ImportSpec(kinds=(imports.KindSpec("save", ("x",)),))

    refusals = imports.check_plan([_placed("a", "saves/a.srm")], _ctx(), spec, emu)  # type: ignore[arg-type]

    assert refusals == []
    assert emu.validated == [1]


def test_two_members_on_one_destination_conflict(tmp_path: Path) -> None:
    """Colliding destinations, including a case-folded collision, refuse both members."""
    spec = imports.ImportSpec(kinds=(imports.KindSpec("save", ("x",)),), case_insensitive_dest=True)

    refusals = _check(tmp_path, [_placed("a", "saves/A.srm"), _placed("b", "saves/a.srm")], spec)

    assert sorted(refusals) == [
        ("destination_conflict", ".import/save/a"),
        ("destination_conflict", ".import/save/b"),
    ]


def test_a_destination_a_v1_member_also_writes_conflicts(tmp_path: Path) -> None:
    """An import may not land where the same archive's v1 member lands."""
    refusals = _check(tmp_path, [_placed("a", "saves/a.srm")], archive_paths=frozenset({"saves/a.srm"}))

    assert refusals == [("destination_conflict", ".import/save/a")]


def test_a_v1_name_spelled_differently_still_conflicts(tmp_path: Path) -> None:
    """`saves/./a` writes the same file as `saves/a`, so the two clash."""
    refusals = _check(tmp_path, [_placed("a", "saves/a")], archive_paths=frozenset({"saves/./a"}))

    assert refusals == [("destination_conflict", ".import/save/a")]


def test_max_members_counts_v1_members_when_asked(tmp_path: Path) -> None:
    """With `counts_v1`, a v1 member of the kind uses up the one allowed place."""
    spec = imports.ImportSpec(kinds=(imports.KindSpec("state", ("x",), max_members=1, counts_v1=True),))

    refusals = _check(
        tmp_path, [_placed("a", "states/a.s", kind="state")], spec, archive_paths=frozenset({"states/old.s"})
    )

    assert refusals == [("destination_conflict", ".import/state/a")]


@pytest.mark.parametrize(
    ("dest", "reason", "ctx"),
    [
        ("saves/a", "memcard_synced_separately", {"excluded": ("saves",)}),
        ("elsewhere/a", "unrecognised_layout", {}),
        ("saves", "unrecognised_layout", {}),
        ("saves/config.ini", "protected_destination", {}),
    ],
)
def test_each_placement_is_checked_against_the_save_tree(
    tmp_path: Path, dest: str, reason: str, ctx: dict[str, Any]
) -> None:
    """Excluded, outside, subtree-itself and protected destinations are each refused.

    Args:
        tmp_path: The per-test temporary directory.
        dest: The destination.
        reason: The expected refusal.
        ctx: Launch-context fields.
    """
    spec = imports.ImportSpec(kinds=(imports.KindSpec("save", ("x",)),), protected=("saves/*.ini",))

    assert _check(tmp_path, [_placed("a", dest)], spec, **ctx) == [(reason, ".import/save/a")]


def test_a_destination_through_an_escaping_symlink_is_unsafe(tmp_path: Path) -> None:
    """A subtree the clear leaves standing as a link out of the save root is never written through.

    Only the chain down to the subtree survives the clear, so that is where
    the link goes; anything deeper is emptied before the write.
    """
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (root / "saves").symlink_to(tmp_path / "outside")

    assert _check(tmp_path, [_placed("a", "saves/a")]) == [("unsafe_path", ".import/save/a")]


def test_the_size_cap_counts_v1_bytes_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Imports plus v1 members over the cap refuse the whole import."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 10)

    assert _check(tmp_path, [_placed("a", "saves/a", size=6)], v1_bytes=6) == [("too_large", None)]


def test_a_unit_missing_a_required_file_is_incomplete(tmp_path: Path) -> None:
    """Every unit must hold `unit_requires`, and a partial plan says so."""
    spec = imports.ImportSpec(
        kinds=(imports.KindSpec("save", ("x",)),), unit_depth=2, unit_requires=frozenset({"icon.sys"})
    )
    emu = _PlanEmu(tmp_path / "root")

    refusals = imports.check_plan(
        [_placed("u/a", "saves/U/a")], _ctx(), spec, emu, partial=True  # type: ignore[arg-type]
    )

    assert [(r.reason, r.member) for r in refusals] == [("incomplete_unit", ".import/save/u/a")]
    assert refusals[0].detail == "missing icon.sys; plan is partial: other members were refused"


def test_a_file_directory_clash_between_imports_conflicts(tmp_path: Path) -> None:
    """One member's file cannot also be another's directory, case-folded or not."""
    spec = imports.ImportSpec(kinds=(imports.KindSpec("save", ("x",)),), case_insensitive_dest=True)

    refusals = _check(tmp_path, [_placed("a", "saves/X"), _placed("b", "saves/x/y")], spec)

    assert sorted(refusals) == [
        ("destination_conflict", ".import/save/a"),
        ("destination_conflict", ".import/save/b"),
    ]


@pytest.mark.parametrize(("v1", "dest"), [("saves/x", "saves/x/y"), ("saves/x/y", "saves/x")])
def test_a_file_directory_clash_with_a_v1_member_conflicts(tmp_path: Path, v1: str, dest: str) -> None:
    """A v1 file on an import's directory, or the other way round, refuses the import.

    Args:
        tmp_path: The per-test temporary directory.
        v1: The v1 member's path.
        dest: The import's destination.
    """
    refusals = _check(tmp_path, [_placed("a", dest)], archive_paths=frozenset({v1}))

    assert refusals == [("destination_conflict", ".import/save/a")]


def test_a_v1_only_file_directory_clash_is_left_to_the_restore(tmp_path: Path) -> None:
    """Two v1 members that clash refuse no import member."""
    refusals = _check(tmp_path, [_placed("a", "saves/a")], archive_paths=frozenset({"saves/x", "saves/x/y"}))

    assert refusals == []


def test_a_colliding_member_over_max_members_is_refused_once(tmp_path: Path) -> None:
    """A member refused for a collision is not refused again for the count."""
    spec = imports.ImportSpec(kinds=(imports.KindSpec("save", ("x",), max_members=1),))

    refusals = _check(tmp_path, [_placed("a", "saves/a"), _placed("b", "saves/a")], spec)

    assert sorted(refusals) == [
        ("destination_conflict", ".import/save/a"),
        ("destination_conflict", ".import/save/b"),
    ]


def test_protected_globs_fold_case_when_the_filesystem_does(tmp_path: Path) -> None:
    """On a case-insensitive filesystem, `Saves/CONFIG.INI` is still `saves/*.ini`."""
    spec = imports.ImportSpec(
        kinds=(imports.KindSpec("save", ("x",)),), protected=("saves/*.ini",), case_insensitive_dest=True
    )
    emu = _PlanEmu(tmp_path / "root", subtrees=("saves", "Saves"))

    refusals = imports.check_plan([_placed("a", "Saves/CONFIG.INI")], _ctx(), spec, emu)  # type: ignore[arg-type]

    assert [(r.reason, r.member) for r in refusals] == [("protected_destination", ".import/save/a")]


# ── refinement ─────────────────────────────────────────────────────────


@pytest.fixture
def _empty_retroarch_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin RetroArch to the empty `import_spec` every emulator has in Wave 1.

    Keeps these refinement tests independent of the spec RetroArch gains in its batch.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    from webstation_broker.emulators.retroarch import Retroarch

    def _spec(self: Retroarch) -> imports.ImportSpec:
        """The empty spec.

        Args:
            self: The launcher.

        Returns:
            An `ImportSpec` accepting nothing.
        """
        return imports.ImportSpec()

    monkeypatch.setattr(Retroarch, "import_spec", _spec)


@pytest.mark.usefixtures("_empty_retroarch_spec")
def test_an_unrecognised_emulatorjs_member_is_source_incompatible() -> None:
    """A layout miss on a member from EmulatorJS or hardware says where it came from."""
    member = _member("x.bin", origin="emulatorjs")
    refusal = imports.ImportRefusal("unrecognised_layout", member.name, "e")

    refined = imports.refine_refusal(refusal, member, "ps2", current="pcsx2")

    assert refined.reason == "source_incompatible"
    assert refined.detail is not None and "emulatorjs" in refined.detail


def test_an_unknown_origin_keeps_its_reason() -> None:
    """Without a declared origin, the layout miss stands as it is."""
    member = _member("x.bin")
    refusal = imports.ImportRefusal("unrecognised_layout", member.name, "e")

    assert imports.refine_refusal(refusal, member, "ps2", current="pcsx2") == refusal


def test_a_psp_savedata_dir_suggests_ppsspp_but_never_itself() -> None:
    """A PSP SAVEDATA dir name points at PPSSPP, unless PPSSPP is the one refusing."""
    member = _member("ULUS10064DATA00/DATA.BIN", origin="standalone")
    refusal = imports.ImportRefusal("source_incompatible", member.name, "e")

    assert imports.refine_refusal(refusal, member, "psp", current="retroarch").suggest_emulator == "ppsspp"
    assert imports.refine_refusal(refusal, member, "psp", current="ppsspp").suggest_emulator is None


@pytest.mark.usefixtures("_empty_retroarch_spec")
def test_a_savedata_dir_with_non_ascii_digits_suggests_nothing() -> None:
    """Only ASCII digits make a PSP serial, so Arabic-Indic digits never point at PPSSPP."""
    member = _member("ULUS\u0661\u0660\u0660\u0666\u0664DATA00/DATA.BIN", origin="standalone")

    assert imports.suggest_for(member, "psp", current="retroarch") is None


@pytest.mark.parametrize("name", ["a.state3", "a.STATE12", "a.state.auto"])
def test_libretro_state_re_matches_retroarch_slot_names(name: str) -> None:
    """The numbered and auto slot names RetroArch writes are recognised, in any case.

    Args:
        name: A RetroArch state name.
    """
    assert imports.LIBRETRO_STATE_RE.fullmatch(name) is not None


@pytest.mark.parametrize("name", ["a.state", "a.state\u0663", "a.state\u0661\u0662"])
def test_libretro_state_re_takes_only_ascii_slot_digits(name: str) -> None:
    """A bare `.state` is flycast's own name, and a non-ASCII digit is no slot RetroArch writes.

    Args:
        name: A name that is not a RetroArch slot state.
    """
    assert imports.LIBRETRO_STATE_RE.fullmatch(name) is None


# ── preflight ──────────────────────────────────────────────────────────


def _declared(members: dict[str, bytes], kinds: Optional[dict[str, str]] = None) -> bytes:
    """Build an archive whose `.import/` members are all declared in a v2 manifest.

    Args:
        members: Member names mapped to bytes.
        kinds: Kind overrides by name; otherwise the path's kind segment.

    Returns:
        The zip.
    """
    files = [
        {"path": n, "kind": (kinds or {}).get(n, n.split("/")[1])}
        for n in members
        if n.startswith(saves.IMPORT_PREFIX)
    ]
    return _zip(members, {"version": 2, "created_at": 0, "files": files})


class _Accepting(FakeEmulator):
    """A fake that takes saves into `saves/` by their file name."""

    save_subtrees = ("saves", "states")

    def import_spec(self) -> imports.ImportSpec:
        """Accept saves.

        Returns:
            The spec.
        """
        return imports.ImportSpec(kinds=(imports.KindSpec("save", ("<name>",)),))

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Place the member under `saves/`.

        Args:
            member: The member.
            spec: The spec.
            ctx: The launch context.

        Returns:
            The placement.
        """
        return imports.Placement(member, PurePosixPath("saves", *member.parts))


def _preflight(emu: FakeEmulator, body: bytes, **kwargs: Any) -> imports.PreflightResult:
    """Run preflight over an archive the way activate does.

    Args:
        emu: The emulator.
        body: The zip.
        **kwargs: Overrides for preflight's keyword arguments.

    Returns:
        The result.
    """
    view = saves.read_archive(body)
    args: dict[str, Any] = {
        "rom_file": None,
        "rom": None,
        "memory_card_synced": False,
        "excluded": (),
        "resume_slot": None,
    }
    args.update(kwargs)
    return imports.preflight(emu, view, body, **args)


def test_the_default_emulator_refuses_every_import(tmp_path: Path) -> None:
    """With no hooks overridden, every member is `kind_not_accepted` and nothing is placed."""
    emu = FakeEmulator()
    emu.save_root = tmp_path

    result = _preflight(emu, _declared({".import/save/a.srm": b"x", ".import/state/b": b"y"}))

    assert result.placements == ()
    assert sorted((r.reason, r.member) for r in result.refusals) == [
        ("kind_not_accepted", ".import/save/a.srm"),
        ("kind_not_accepted", ".import/state/b"),
    ]
    assert result.identity == imports.SessionIdentity(None, "none")


def test_an_accepting_emulator_gets_a_plan(tmp_path: Path) -> None:
    """A clean import comes back placed, with no refusals."""
    emu = _Accepting()
    emu.save_root = tmp_path

    result = _preflight(emu, _declared({".import/save/a.srm": b"x", "saves/v1.srm": b"v"}))

    assert result.refusals == ()
    assert [(p.member.name, p.dest) for p in result.placements] == [
        (".import/save/a.srm", PurePosixPath("saves/a.srm"))
    ]
    assert emu.import_identity == imports.SessionIdentity(None, "none")


class _WithSidecar(_Accepting):
    """A fake that places each save under `saves/` and writes a marker at `sidecar` beside it."""

    sidecar = "saves/marker"

    def import_spec(self) -> imports.ImportSpec:
        """Accept saves, and reserve `*.rom` for the broker the way Flycast and DuckStation do.

        Returns:
            The spec.
        """
        return imports.ImportSpec(kinds=(imports.KindSpec("save", ("<name>",)),), protected=("*.rom",))

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Place the member under `saves/`, with one sidecar at `sidecar`.

        Args:
            member: The member.
            spec: The spec.
            ctx: The launch context.

        Returns:
            The placement.
        """
        dest = PurePosixPath("saves", *member.parts)
        return imports.Placement(member, dest, ((PurePosixPath(self.sidecar), b"marker\n"),))


@pytest.mark.parametrize(
    ("sidecar", "refusals"),
    [
        ("saves/a.srm.rom", []),
        (
            "elsewhere/a.srm.rom",
            [("unrecognised_layout", "elsewhere/a.srm.rom is not inside a save subtree")],
        ),
        ("states/a.srm.rom", [("unsafe_path", "states/a.srm.rom resolves outside the save root")]),
    ],
)
def test_a_sidecar_is_held_to_the_save_tree_rules_its_destination_is(
    tmp_path: Path, sidecar: str, refusals: list[tuple[str, str]]
) -> None:
    """A sidecar outside the save tree, or through a link out of it, refuses its member before any write.

    The destination always lands safely in `saves/`, and `states/` is a link
    out of the save root, so a refusal can only come from the sidecar. The
    sidecar in `saves/` matches the protected `*.rom` and still lands: those
    globs reserve the marker for the broker, and a sidecar is the broker's.

    Args:
        tmp_path: The per-test temporary directory.
        sidecar: Where the hook puts the sidecar, relative to the save root.
        refusals: The expected `(reason, detail)` pairs, all for the one member.
    """
    root = tmp_path / "root"
    (root / "saves").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "states").symlink_to(outside)
    emu = _WithSidecar()
    emu.save_root = root
    emu.sidecar = sidecar

    result = _preflight(emu, _declared({".import/save/a.srm": b"x"}))

    assert [(r.reason, r.detail) for r in result.refusals] == refusals
    assert all(r.member == ".import/save/a.srm" for r in result.refusals)
    assert len(result.placements) == (0 if refusals else 1)
    assert list(outside.iterdir()) == []
    assert list((root / "saves").iterdir()) == []


def test_a_member_named_like_a_sidecar_is_still_protected(tmp_path: Path) -> None:
    """Only the broker's own sidecar skips the protected globs; a member on one is refused.

    Args:
        tmp_path: The per-test temporary directory.
    """
    emu = _WithSidecar()
    emu.save_root = tmp_path
    (tmp_path / "saves").mkdir()

    result = _preflight(emu, _declared({".import/save/a.srm.rom": b"x"}))

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("protected_destination", "saves/a.srm.rom is emulator configuration")
    ]
    assert result.placements == ()


def test_one_refusal_empties_the_whole_plan(tmp_path: Path) -> None:
    """All or nothing: a single refused member leaves no placements at all."""
    emu = _Accepting()
    emu.save_root = tmp_path

    result = _preflight(emu, _declared({".import/save/a.srm": b"x", ".import/state/s": b"y"}))

    assert result.placements == ()
    assert [(r.reason, r.member) for r in result.refusals] == [("kind_not_accepted", ".import/state/s")]


def test_manifest_hygiene_and_v1_refusals_all_come_back_together(tmp_path: Path) -> None:
    """Every problem in the archive is reported at once, de-duplicated."""
    emu = _Accepting()
    emu.save_root = tmp_path
    body = _zip(
        {".import/save/.hidden": b"x", ".import/save/undeclared": b"y"},
        {"version": 2, "created_at": 0, "files": [{"path": ".import/save/.hidden", "kind": "save"}]},
    )
    v1 = imports.ImportRefusal("unsafe_path", "../x", None)

    result = _preflight(emu, body, v1_refusals=(v1, v1))

    assert sorted((r.reason, r.member) for r in result.refusals) == [
        ("manifest_invalid", ".import/save/undeclared"),
        ("unsafe_path", "../x"),
        ("unsafe_path", ".import/save/.hidden"),
    ]


def test_a_hook_that_answers_nonsense_is_a_bug(tmp_path: Path) -> None:
    """A `place_import` that returns neither a placement nor a refusal raises."""

    class Broken(_Accepting):
        """Answers None."""

        def place_import(self, member: Any, spec: Any, ctx: Any) -> Any:
            """Answer None.

            Args:
                member: The member.
                spec: The spec.
                ctx: The launch context.

            Returns:
                None.
            """
            return None

    emu = Broken()
    emu.save_root = tmp_path

    with pytest.raises(TypeError, match="place_import"):
        _preflight(emu, _declared({".import/save/a": b"x"}))


def test_an_unreadable_import_member_is_refused_by_preflight(tmp_path: Path) -> None:
    """An encrypted `.import/` member is refused, so nothing is placed and the slot is never cleared."""
    emu = _Accepting()
    emu.save_root = tmp_path
    body = mangle_zip_member(_declared({".import/save/a.srm": b"x"}), ".import/save/a.srm", flags=0x1)

    result = _preflight(emu, body)

    assert result.placements == ()
    assert [(r.reason, r.member, r.detail) for r in result.refusals] == [
        ("unreadable_member", ".import/save/a.srm", "the member is encrypted")
    ]


def test_a_hook_whose_head_read_fails_gets_unreadable_member(tmp_path: Path) -> None:
    """A hook that sniffs a corrupt member needs no catch of its own: preflight refuses the member.

    Args:
        tmp_path: The per-test temporary directory.
    """

    class Sniffing(_Accepting):
        """Reads each member's first bytes before placing it."""

        def place_import(
            self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
        ) -> Union[imports.Placement, imports.ImportRefusal]:
            """Sniff the member, then place it.

            Args:
                member: The member.
                spec: The spec.
                ctx: The launch context.

            Returns:
                The placement.
            """
            member.head(64)
            return super().place_import(member, spec, ctx)

    emu = Sniffing()
    emu.save_root = tmp_path
    body = corrupt_zip_member(_declared({".import/save/a.srm": b"x" * 16}), ".import/save/a.srm")

    result = _preflight(emu, body)

    assert result.placements == ()
    assert [(r.reason, r.member, r.expected, r.detail) for r in result.refusals] == [
        ("unreadable_member", ".import/save/a.srm", imports.READABLE_EXPECTED, "the member's data is corrupt")
    ]


def _with_duplicate(body: bytes, name: str, data: bytes) -> bytes:
    """Append a second entry under a name the archive already holds.

    Args:
        body: The archive.
        name: The name to repeat.
        data: The second entry's bytes.

    Returns:
        The archive, holding both entries.
    """
    buf = io.BytesIO(body)
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(buf, "a") as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def test_a_name_the_archive_holds_twice_is_refused_once(tmp_path: Path) -> None:
    """`zipfile` reads a repeated name as its last entry, so the first would escape every check.

    Args:
        tmp_path: The per-test temporary directory.
    """
    emu = _Accepting()
    emu.save_root = tmp_path
    body = _with_duplicate(_declared({".import/save/a.srm": b"x"}), ".import/save/a.srm", b"y")

    result = _preflight(emu, body)

    assert result.placements == ()
    assert [(r.reason, r.member, r.detail) for r in result.refusals] == [
        ("unsafe_path", ".import/save/a.srm", "the archive holds this name 2 times")
    ]


def test_preflight_suggests_from_the_emulators_platform(tmp_path: Path) -> None:
    """The suggestion reads `emulator.platform`, the same value `import_spec` reads."""

    class Refusing(_Accepting):
        """Refuses every member as another emulator's format."""

        def place_import(self, member: Any, spec: Any, ctx: Any) -> Any:
            """Refuse the member.

            Args:
                member: The member.
                spec: The spec.
                ctx: The launch context.

            Returns:
                A `source_incompatible` refusal.
            """
            return imports.ImportRefusal("source_incompatible", member.name, None)

    emu = Refusing()
    emu.save_root = tmp_path
    emu.platform = "psp"
    body = _declared({".import/save/ULUS10041DATA/PARAM.SFO": b"x"})

    result = _preflight(emu, body, rom=imports.RomRef(1, "Game", None))

    assert [r.suggest_emulator for r in result.refusals] == ["ppsspp"]


def test_an_archive_level_manifest_refusal_refuses_the_whole_import(tmp_path: Path) -> None:
    """A non-object `files[i]` refuses with no member, and still leaves nothing placed."""
    emu = _Accepting()
    emu.save_root = tmp_path
    body = _zip(
        {".import/save/a.srm": b"x"},
        {"version": 2, "created_at": 0, "files": [{"path": ".import/save/a.srm", "kind": "save"}, 7]},
    )

    result = _preflight(emu, body)

    assert result.placements == ()
    assert [(r.reason, r.member) for r in result.refusals] == [("manifest_invalid", None)]


def test_every_refusal_code_has_a_docs_anchor() -> None:
    """Each `docs` link a refusal carries lands on a heading in the imports page."""
    page = Path(__file__).resolve().parents[1] / "docs" / "content" / "docs" / "api" / "imports.mdx"
    text = page.read_text(encoding="utf-8")

    missing = [r for r in sorted(imports.REASONS) if f"[#{r.replace('_', '-')}]" not in text]

    assert missing == []
