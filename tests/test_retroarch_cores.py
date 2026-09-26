"""Tests for the RetroArch core catalog, tiers and profile resolution."""

import io
import zipfile

import pytest

from webstation_broker.emulators import retroarch_cores as rc


def info_zip(infos: dict[str, dict[str, str]]) -> bytes:
    """Build an info.zip holding one `<core>_libretro.info` per entry.

    Args:
        infos: Core name to the `.info` keys to write.

    Returns:
        The zip bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for core, keys in infos.items():
            body = "".join(f'{k} = "{v}"\n' for k, v in keys.items())
            zf.writestr(f"{core}_libretro.info", body)
    return buf.getvalue()


SNES9X = {
    "display_name": "Nintendo - SNES / SFC (Snes9x)",
    "corename": "Snes9x",
    "supported_extensions": "smc|sfc|SWC|fig|bs|st",
}
BSNES = {
    "display_name": "Nintendo - SNES / SFC (bsnes)",
    "corename": "bsnes",
    "supported_extensions": "sfc|smc|gb|gbc|bs",
}
MGBA = {
    "display_name": "Nintendo - Game Boy Advance (mGBA)",
    "corename": "mGBA",
    "supported_extensions": "gb|gbc|gba",
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sfc|SMC", (".sfc", ".smc")),
        ("", ()),
        ("sfc|sfc|.bin", (".sfc", ".bin")),
        (" zip | 7z ", (".zip", ".7z")),
    ],
)
def test_extensions_are_lowercased_dotted_and_deduplicated(raw: str, expected: tuple[str, ...]) -> None:
    """Core-info lists extensions dotless and in any case; the table uses `.sfc`."""
    assert rc.normalize_extensions(raw) == expected


def test_parse_info_zip_reads_name_corename_and_extensions() -> None:
    """Each `<core>_libretro.info` becomes one CoreInfo keyed by core name."""
    cores = rc.parse_info_zip(info_zip({"snes9x": SNES9X}))

    expected = rc.CoreInfo(
        "snes9x",
        SNES9X["display_name"],
        "Snes9x",
        (".smc", ".sfc", ".swc", ".fig", ".bs", ".st"),
    )
    assert cores["snes9x"] == expected


def test_parse_info_zip_skips_members_that_are_not_core_info() -> None:
    """A README or a bad core name in the zip is not a core."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.md", "x")
        zf.writestr("Bad-Name_libretro.info", 'corename = "x"\n')
    assert rc.parse_info_zip(buf.getvalue()) == {}


@pytest.mark.parametrize(
    "line",
    ["snes9x_libretro.so.zip", "2026-09-01 0a1b2c3d snes9x_libretro.so.zip", "  snes9x_libretro.so.zip  "],
)
def test_parse_index_reads_plain_and_extended_lines(line: str) -> None:
    """The buildbot's `.index` is one file per line; `.index-extended` prefixes date and hash."""
    assert rc.parse_index(f"{line}\nnot-a-core.txt\n") == frozenset({"snes9x"})


def test_catalog_is_zip_intersect_index_plus_installed() -> None:
    """A core not built for x86_64 is never offered, unless its .so is already installed."""
    data = info_zip({"snes9x": SNES9X, "bsnes": BSNES, "mgba": MGBA})
    catalog = rc.build_catalog(data, "snes9x_libretro.so.zip\n", installed=frozenset({"mgba"}))

    assert sorted(catalog.cores) == ["mgba", "snes9x"]
    assert catalog.info_file("snes9x") is not None
    assert catalog.info_file("bsnes") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        (" TRUE ", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("", False),
        (None, False),
    ],
)
def test_truthy_matches_the_brokers_other_boolean_env_vars(
    value: str, expected: bool
) -> None:
    """Same spelling as rpcs3's `_truthy`."""
    assert rc.truthy(value) is expected


def test_tiers_accept_vetted_and_scoped_blocked() -> None:
    """A blocked core may be narrowed to some platforms; reports are optional."""
    tiers = rc.load_tiers({
        "bsnes": {"tier": "vetted"},
        "melonds": {"tier": "blocked", "reason": "crashes", "reports": ["#12"], "platforms": ["nds"]},
    })
    assert tiers["melonds"] == rc.TierEntry("blocked", "crashes", ("#12",), frozenset({"nds"}))
    assert tiers["bsnes"].platforms is None


@pytest.mark.parametrize(
    "raw",
    [
        {"x": {"tier": "blocked"}},                       # blocked needs a reason
        {"x": {"tier": "default"}},                       # default is implied by the platform table
        {"x": {"tier": "untested"}},                       # untested is implied by absence
        {"x": {"tier": "vetted", "colour": "red"}},       # unknown key
        {"Bad-Name": {"tier": "vetted"}},                 # bad core name
        {"x": {"tier": "vetted", "platforms": ["snes"]}}, # platforms only narrows a block
    ],
)
def test_tiers_reject_bad_entries(raw: dict) -> None:
    """Every rule in §5.2 is a load-time error."""
    with pytest.raises(ValueError):
        rc.load_tiers(raw)
