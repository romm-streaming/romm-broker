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


PLATFORMS = {
    "snes": {
        "core": "snes9x", "library_name": "Snes9x", "save_ram": True,
        "extensions": (".sfc", ".smc", ".bin"), "resume_settle": 3.0,
        "alternates": {"bsnes": {"library_name": "bsnes", "save_ram": True}},
    },
    "gba": {"core": "mgba", "library_name": "mGBA", "save_ram": True, "extensions": (".gba",)},
}
CATALOG = rc.build_catalog(
    info_zip({"snes9x": SNES9X, "bsnes": BSNES, "mgba": MGBA,
              "mesen": {"corename": "Mesen", "supported_extensions": "nes"},
              "beetle_snes": {"corename": "Mednafen bSNES", "supported_extensions": "sfc|smc"}}),
    "\n".join(f"{c}_libretro.so.zip" for c in ("snes9x", "bsnes", "mgba", "mesen", "beetle_snes")),
)
TIERS = {"beetle_snes": rc.TierEntry("blocked", "hangs on boot", ("#7",), None),
         "bsnes": rc.TierEntry("vetted", None, (), None)}


def resolve(platform: str, core: str | None, experimental: bool = False) -> rc.Profile:
    """Resolve against the test fixtures."""
    return rc.resolve_profile(
        PLATFORMS, platform, core, experimental=experimental, catalog=CATALOG, tiers=TIERS
    )


@pytest.mark.parametrize("core", [None, "snes9x"])
def test_no_core_or_the_default_core_is_the_platform_entry(core: str | None) -> None:
    """§6.1 row 1."""
    profile = resolve("snes", core)
    assert (profile["core"], profile["tier"], profile["resume_settle"]) == ("snes9x", "default", 3.0)


def test_an_alternate_is_vetted_and_inherits_no_core_owned_field() -> None:
    """§6.1 row 2 and §5.1: resume_settle belongs to snes9x, not bsnes."""
    profile = resolve("snes", "bsnes")
    assert (profile["core"], profile["tier"], profile["library_name"]) == ("bsnes", "vetted", "bsnes")
    assert profile["extensions"] == (".sfc", ".smc", ".bin")
    assert "resume_settle" not in profile


def test_a_vetted_core_is_untested_on_a_platform_without_an_alternate() -> None:
    """§5.2: vetted is per platform in practice."""
    gb_entry = {"core": "gambatte", "library_name": "Gambatte", "save_ram": True, "extensions": (".gb",)}
    profile = rc.resolve_profile(
        {**PLATFORMS, "gb": gb_entry},
        "gb", "bsnes", experimental=False, catalog=CATALOG, tiers=TIERS,
    )
    assert profile["tier"] == "untested"


def test_an_untested_core_takes_the_extension_intersection_in_platform_order() -> None:
    """§5.4: `.bin` is not in mGBA-for-SNES terms; order follows the platform."""
    profile = resolve("snes", "beetle_snes", experimental=True)
    assert profile["extensions"] == (".sfc", ".smc")
    tier_ram_name = (profile["tier"], profile["save_ram"], profile["library_name"])
    assert tier_ram_name == ("blocked", None, "Mednafen bSNES")
    assert profile.get("save_subtrees") is None


def test_a_blocked_core_needs_the_opt_in() -> None:
    """§6.1 row 3: the detail names the reason and both ways to opt in."""
    with pytest.raises(rc.CoreRejectedError) as err:
        resolve("snes", "beetle_snes")
    assert "hangs on boot" in err.value.detail
    assert "RETROARCH_EXPERIMENTAL_CORES" in err.value.detail and "experimental_cores" in err.value.detail
    assert "snes9x" in err.value.detail


def test_a_platform_scoped_block_does_not_apply_elsewhere() -> None:
    """A core broken on one platform is merely untested on another."""
    tiers = {"bsnes": rc.TierEntry("blocked", "bad on sfam", (), frozenset({"sfam"}))}
    profile = rc.resolve_profile(
        {"snes": {k: v for k, v in PLATFORMS["snes"].items() if k != "alternates"}},
        "snes", "bsnes", experimental=False, catalog=CATALOG, tiers=tiers,
    )
    assert profile["tier"] == "untested"


@pytest.mark.parametrize(
    ("platform", "core"), [("snes", "mesen"), ("snes", "nosuchcore"), ("gba", "bsnes"), ("n64", "bsnes")]
)
def test_unknown_core_or_no_shared_extension_is_rejected_with_options(platform: str, core: str) -> None:
    """§6.1 last row: the detail lists the default and points at the cores route."""
    with pytest.raises(rc.CoreRejectedError) as err:
        resolve(platform, core)
    assert "/api/retroarch/cores" in err.value.detail


def test_profile_is_read_only() -> None:
    """The resolved profile is frozen; an observed library_name lives on the instance."""
    with pytest.raises(TypeError):
        resolve("snes", None)["core"] = "x"  # type: ignore[index]


_GB_PLATFORMS = {
    **PLATFORMS,
    "gb": {"core": "gambatte", "library_name": "Gambatte", "save_ram": True, "extensions": (".gb",)},
}


@pytest.mark.parametrize(
    ("platforms", "platform", "core", "expected"),
    [
        (PLATFORMS, "snes", "snes9x", "default"),
        (PLATFORMS, "snes", "bsnes", "vetted"),
        (_GB_PLATFORMS, "gb", "bsnes", "untested"),
        (PLATFORMS, "snes", "beetle_snes", "blocked"),
    ],
)
def test_tier_of_covers_all_four_tiers(
    platforms: dict[str, dict], platform: str, core: str, expected: str
) -> None:
    """`tier_of` directly, one case per tier (§6.1): default, vetted, untested, blocked."""
    assert rc.tier_of(platforms, platform, core, CATALOG, TIERS) == expected


def test_a_platform_scoped_block_does_apply_on_a_listed_platform() -> None:
    """The complement of the elsewhere test: `snes` is inside the block's scope."""
    tiers = {"bsnes": rc.TierEntry("blocked", "bad on snes", (), frozenset({"snes"}))}
    platforms = {"snes": {k: v for k, v in PLATFORMS["snes"].items() if k != "alternates"}}
    with pytest.raises(rc.CoreRejectedError):
        rc.resolve_profile(platforms, "snes", "bsnes", experimental=False, catalog=CATALOG, tiers=tiers)
    profile = rc.resolve_profile(platforms, "snes", "bsnes", experimental=True, catalog=CATALOG, tiers=tiers)
    assert profile["tier"] == "blocked"
