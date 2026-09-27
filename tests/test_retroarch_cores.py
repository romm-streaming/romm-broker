"""Tests for the RetroArch core catalog, tiers and profile resolution."""

import io
import logging
import threading
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
from fastapi.testclient import TestClient

from webstation_broker import settings
from webstation_broker.app import create_app
from webstation_broker.emulators import retroarch
from webstation_broker.emulators import retroarch_cores as rc

from .conftest import PREFIX


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


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False), (None, False)])
def test_parse_info_zip_reads_block_extract(raw: Optional[str], expected: bool) -> None:
    """`block_extract = "true"` means RetroArch hands the core an archive unopened."""
    keys = dict(SNES9X) if raw is None else {**SNES9X, "block_extract": raw}
    assert rc.parse_info_zip(info_zip({"snes9x": keys}))["snes9x"].block_extract is expected


def test_parse_info_zip_skips_members_that_are_not_core_info() -> None:
    """A README or a bad core name in the zip is not a core."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.md", "x")
        zf.writestr("Bad-Name_libretro.info", 'corename = "x"\n')
    assert rc.parse_info_zip(buf.getvalue()) == {}


@pytest.mark.parametrize("corename", ["../../../tmp/x", "a/b", "a\\b", "..", ".", "", "a\x00b"])
def test_parse_info_zip_replaces_an_unsafe_corename_with_the_core_name(
    corename: str, caplog: pytest.LogCaptureFixture
) -> None:
    """I2: a hostile catalog's corename becomes a dir name, so it falls back to the validated core name.

    Args:
        corename: A corename that could name a path outside `saves/` or `states/`.
        caplog: The pytest log capture fixture.
    """
    with caplog.at_level(logging.WARNING):
        cores = rc.parse_info_zip(info_zip({"evil": {**SNES9X, "corename": corename}}))
    assert cores["evil"].corename == "evil"
    assert "unsafe corename" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Snes9x", "Snes9x"),
        ("Mesen-S", "Mesen-S"),
        ("", None),
        (".", None),
        ("..", None),
        ("a/b", None),
        ("a\\b", None),
        ("a\x00b", None),
        (None, None),
        (3, None),
    ],
)
def test_safe_dir_name_accepts_only_a_single_plain_component(value: object, expected: object) -> None:
    """The shared guard refuses anything that is not one plain path component.

    Args:
        value: The candidate.
        expected: What the guard answers.
    """
    assert rc.safe_dir_name(value) == expected


def test_parse_info_zip_skips_a_member_over_the_decompressed_cap(caplog: pytest.LogCaptureFixture) -> None:
    """A member that inflates past INFO_MEMBER_CAP is skipped; the rest still parse.

    Args:
        caplog: The pytest log capture fixture.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("snes9x_libretro.info", "".join(f'{k} = "{v}"\n' for k, v in SNES9X.items()))
        zf.writestr("bomb_libretro.info", 'corename = "bomb"\n' + " " * (rc.INFO_MEMBER_CAP + 1))
    data = buf.getvalue()
    assert len(data) < rc.INFO_MEMBER_CAP  # small on the wire, large once inflated
    with caplog.at_level(logging.WARNING):
        cores = rc.parse_info_zip(data)
    assert set(cores) == {"snes9x"}
    assert "bomb_libretro.info" in caplog.text and "over the" in caplog.text
    catalog = rc.Catalog({"bomb": rc.CoreInfo("bomb", "Bomb", "bomb", ())}, data)
    assert catalog.info_file("bomb") is None


def test_the_cap_drops_no_bundled_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundled zip parses to the same cores with the cap as without it.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    data = rc.BUNDLED_ZIP.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert max(m.file_size for m in zf.infolist()) <= rc.INFO_MEMBER_CAP
    capped = rc.parse_info_zip(data)
    monkeypatch.setattr(rc, "INFO_MEMBER_CAP", len(data) * 1000)
    assert capped == rc.parse_info_zip(data)


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
    assert settings.truthy(value) is expected


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
    """Every platform-table rule is a load-time error."""
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
              "beetle_snes": {"corename": "Mednafen bSNES", "supported_extensions": "sfc|smc"},
              "armsnes": {"corename": "ARM SNES", "supported_extensions": "sfc|smc"}}),
    "\n".join(f"{c}_libretro.so.zip" for c in ("snes9x", "bsnes", "mgba", "mesen", "beetle_snes", "armsnes")),
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
    """No core, or the default core named, resolves to the platform entry."""
    profile = resolve("snes", core)
    assert (profile["core"], profile["tier"], profile["resume_settle"]) == ("snes9x", "default", 3.0)


def test_an_alternate_is_vetted_and_inherits_no_core_owned_field() -> None:
    """Resume_settle belongs to snes9x, not bsnes."""
    profile = resolve("snes", "bsnes")
    assert (profile["core"], profile["tier"], profile["library_name"]) == ("bsnes", "vetted", "bsnes")
    assert profile["extensions"] == (".sfc", ".smc", ".bin")
    assert "resume_settle" not in profile


def test_a_vetted_core_is_untested_on_a_platform_without_an_alternate() -> None:
    """Vetted is per platform in practice."""
    gb_entry = {"core": "gambatte", "library_name": "Gambatte", "save_ram": True, "extensions": (".gb",)}
    profile = rc.resolve_profile(
        {**PLATFORMS, "gb": gb_entry},
        "gb", "bsnes", experimental=False, catalog=CATALOG, tiers=TIERS,
    )
    assert profile["tier"] == "untested"


def test_an_untested_core_takes_the_extension_intersection_in_platform_order() -> None:
    """`.bin` is not in mGBA-for-SNES terms; order follows the platform."""
    profile = resolve("snes", "beetle_snes", experimental=True)
    assert profile["extensions"] == (".sfc", ".smc")
    tier_ram_name = (profile["tier"], profile["save_ram"], profile["library_name"])
    assert tier_ram_name == ("blocked", None, "Mednafen bSNES")
    assert profile.get("save_subtrees") is None


def test_a_blocked_core_needs_the_opt_in() -> None:
    """The detail names the reason and both ways to opt in."""
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
    """The detail lists the default and points at the cores route."""
    with pytest.raises(rc.CoreRejectedError) as err:
        resolve(platform, core)
    assert "/api/retroarch/cores" in err.value.detail


@pytest.mark.parametrize("core", [["bsnes"], {"bsnes": 1}, 5])
def test_a_core_that_is_not_a_string_is_rejected(core: object) -> None:
    """A manifest's `core` is archive content; an unhashable one must not raise TypeError."""
    with pytest.raises(rc.CoreRejectedError) as err:
        resolve("snes", core)  # type: ignore[arg-type]
    assert "not a" in err.value.detail


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
    """`tier_of` directly, one case per tier: default, vetted, untested, blocked."""
    assert rc.tier_of(platforms, platform, core, CATALOG, TIERS) == expected


def test_a_platform_scoped_block_does_apply_on_a_listed_platform() -> None:
    """The complement of the elsewhere test: `snes` is inside the block's scope."""
    tiers = {"bsnes": rc.TierEntry("blocked", "bad on snes", (), frozenset({"snes"}))}
    platforms = {"snes": {k: v for k, v in PLATFORMS["snes"].items() if k != "alternates"}}
    with pytest.raises(rc.CoreRejectedError):
        rc.resolve_profile(platforms, "snes", "bsnes", experimental=False, catalog=CATALOG, tiers=tiers)
    profile = rc.resolve_profile(platforms, "snes", "bsnes", experimental=True, catalog=CATALOG, tiers=tiers)
    assert profile["tier"] == "blocked"


def test_cores_for_platform_lists_every_tier_in_order() -> None:
    """All cores offered on a platform, sorted default first, then vetted, untested, blocked."""
    cores = rc.cores_for_platform(PLATFORMS, "snes", CATALOG, TIERS)

    tiers = [c["tier"] for c in cores]
    assert tiers == sorted(tiers, key=lambda t: rc._TIER_ORDER[t])
    assert cores[0]["core"] == "snes9x"
    assert cores[0]["tier"] == "default"


def test_cores_for_platform_includes_display_name_and_verified() -> None:
    """Every core row has its display_name and verified flag."""
    cores = rc.cores_for_platform(PLATFORMS, "snes", CATALOG, TIERS)

    default = [c for c in cores if c["core"] == "snes9x"][0]
    assert (default["display_name"], default["verified"]) == (SNES9X["display_name"], True)
    untested = [c for c in cores if c["tier"] == "untested"]
    assert untested and all(c["verified"] is False for c in untested)


def test_cores_for_platform_includes_report_url() -> None:
    """Every core row includes a report_url with core and platform."""
    cores = rc.cores_for_platform(PLATFORMS, "snes", CATALOG, TIERS)

    for core in cores:
        assert f"core={core['core']}" in core["report_url"]
        assert "platform=snes" in core["report_url"]
        assert rc.REPORT_URL in core["report_url"]


def test_cores_for_platform_blocked_includes_reason() -> None:
    """Blocked cores include the reason."""
    cores = rc.cores_for_platform(PLATFORMS, "snes", CATALOG, TIERS)

    blocked = [c for c in cores if c["tier"] == "blocked"]
    assert blocked and all(c["reason"] is not None for c in blocked)
    untested = [c for c in cores if c["tier"] == "untested"]
    assert untested and all(c["reason"] is None for c in untested)


def test_cores_route_needs_the_secret(secret_client: TestClient) -> None:
    """Every route is gated (CONTRIBUTING)."""
    assert secret_client.get(f"{PREFIX}/api/retroarch/cores", params={"platform": "snes"}).status_code == 403


def test_cores_route_lists_one_platform(client: TestClient) -> None:
    """The default comes first and is verified; untested rows carry a report link."""
    body = client.get(f"{PREFIX}/api/retroarch/cores", params={"platform": "snes"}).json()
    assert body["default"] == "snes9x"
    first = body["cores"][0]
    assert (first["core"], first["tier"], first["verified"]) == ("snes9x", "default", True)
    untested = [c for c in body["cores"] if c["tier"] == "untested"]
    assert untested and all(not c["verified"] and "core-report" in c["report_url"] for c in untested)


def test_cores_route_lists_every_platform_without_one(client: TestClient) -> None:
    """No platform: all of them."""
    body = client.get(f"{PREFIX}/api/retroarch/cores").json()
    assert body["platforms"]["snes"]["default"] == "snes9x"


def test_cores_route_404s_an_unknown_platform(client: TestClient) -> None:
    """Not a RetroArch platform."""
    assert client.get(f"{PREFIX}/api/retroarch/cores", params={"platform": "ps2"}).status_code == 404


def _fetcher(zip_bytes: bytes, index: str) -> Callable[[str, int], bytes]:
    """A fetch stub serving one zip and one index by URL suffix.

    Args:
        zip_bytes: The body served for a URL ending in `.zip`.
        index: The body served for any other URL, encoded as UTF-8.

    Returns:
        A `fetch(url, cap)` callable matching `refresh_once`'s `fetch` parameter.
    """

    def fetch(url: str, cap: int) -> bytes:
        """Serve `zip_bytes` or `index`, refusing a body over `cap`.

        Args:
            url: The URL being fetched.
            cap: The largest body accepted.

        Returns:
            The body.

        Raises:
            ValueError: When the body is over `cap`.
        """
        body = zip_bytes if url.endswith(".zip") else index.encode()
        if len(body) > cap:
            raise ValueError("over cap")
        return body

    return fetch


class TestRefresh:
    """Background catalog refresh, all offline."""

    def test_refresh_adds_an_untested_core_and_writes_the_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A new core becomes launchable; files land atomically."""
        rc.set_catalog(rc.load_bundled_catalog())
        new = info_zip({"brand_new": {"corename": "BN", "supported_extensions": "sfc"}})
        assert rc.refresh_once(
            tmp_path, tmp_path, fetch=_fetcher(new, "brand_new_libretro.so.zip"), platforms=PLATFORMS
        )
        assert "brand_new" in rc.catalog().cores
        assert (tmp_path / "core_info_cache.zip").read_bytes() == new
        assert not list(tmp_path.glob("*.tmp"))

    def test_refresh_never_changes_a_protected_core(self, tmp_path: Path) -> None:
        """default, vetted and tiered cores keep the bundled entry."""
        rc.set_catalog(rc.load_bundled_catalog())
        evil = info_zip({"snes9x": {"corename": "EVIL", "supported_extensions": "exe"}})
        rc.refresh_once(
            tmp_path,
            tmp_path,
            fetch=_fetcher(evil, "snes9x_libretro.so.zip"),
            platforms={"snes": {"core": "snes9x", "extensions": (".sfc",)}},
        )
        assert rc.catalog().cores["snes9x"].corename == "Snes9x"
        assert b"EVIL" not in (rc.catalog().info_file("snes9x") or b"")

    @pytest.mark.parametrize("bad", ["cap", "notzip", "noinfo"])
    def test_a_bad_fetch_keeps_the_current_catalog(
        self, tmp_path: Path, bad: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Over cap, not a zip, or no .info members: warn and keep."""
        rc.set_catalog(rc.load_bundled_catalog())
        before = rc.catalog()
        body = {"cap": b"x" * (rc.ZIP_CAP + 1), "notzip": b"nope", "noinfo": info_zip({})}[bad]
        with caplog.at_level(logging.WARNING):
            assert not rc.refresh_once(tmp_path, tmp_path, fetch=_fetcher(body, ""), platforms=PLATFORMS)
        assert rc.catalog() is before and "core info refresh" in caplog.text

    def test_bad_core_names_are_dropped_and_counted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Names are filtered by ^[a-z0-9_]+$."""
        rc.set_catalog(rc.load_bundled_catalog())
        z = info_zip({"good_one": {"supported_extensions": "sfc"}})
        with caplog.at_level(logging.INFO):
            rc.refresh_once(
                tmp_path,
                tmp_path,
                fetch=_fetcher(z, "good_one_libretro.so.zip\nBad-One_libretro.so.zip"),
                platforms=PLATFORMS,
            )
        assert "dropped 1" in caplog.text

    def test_removal_is_sticky_for_an_installed_core(self, tmp_path: Path) -> None:
        """A core gone from the index stays if its .so is installed."""
        rc.set_catalog(rc.load_bundled_catalog())
        (tmp_path / "gone_libretro.so").write_bytes(b"so")
        z = info_zip({"gone": {"supported_extensions": "sfc"}})
        rc.refresh_once(tmp_path, tmp_path, fetch=_fetcher(z, ""), platforms=PLATFORMS)
        assert "gone" in rc.catalog().cores

    def test_load_startup_catalog_merges_a_valid_cache(self, tmp_path: Path) -> None:
        """A cache-only core, not in the bundled catalog, is present after startup."""
        z = info_zip({"cache_only": {"corename": "Cache Only", "supported_extensions": "sfc"}})
        (tmp_path / rc.CACHE_INDEX).write_text("cache_only_libretro.so.zip\n")
        (tmp_path / rc.CACHE_ZIP).write_bytes(z)

        rc.load_startup_catalog(tmp_path, tmp_path, PLATFORMS)

        assert "cache_only" in rc.catalog().cores

    @pytest.mark.parametrize("kind", ["truncated", "zlib_error"])
    def test_load_startup_catalog_falls_back_on_a_corrupt_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
    ) -> None:
        """A corrupt on-disk cache must not stop the broker from booting.

        A truncated zip raises `zipfile.BadZipFile` on its own. The second case
        stands in for a member that fails to decompress (`zlib.error`), which
        needs a real deflate-stream corruption to trigger naturally; monkeypatch
        `parse_info_zip` instead, but only for the cached bytes, so the bundled
        catalog's own (unrelated) parse is untouched.
        """
        (tmp_path / rc.CACHE_INDEX).write_text("x_libretro.so.zip\n")
        if kind == "truncated":
            (tmp_path / rc.CACHE_ZIP).write_bytes(b"PK\x03\x04not a real zip")
        else:
            bad_zip = info_zip({"x": {"supported_extensions": "sfc"}})
            (tmp_path / rc.CACHE_ZIP).write_bytes(bad_zip)
            real_parse_info_zip = rc.parse_info_zip

            def flaky(data: bytes) -> dict[str, rc.CoreInfo]:
                """Raise for the cached bytes only; defer to the real parser otherwise.

                Args:
                    data: The zip bytes being parsed.

                Returns:
                    The real parser's result, for any input other than `bad_zip`.

                Raises:
                    zlib.error: When `data` is the cached zip under test.
                """
                if data == bad_zip:
                    raise zlib.error("bad deflate stream")
                return real_parse_info_zip(data)

            monkeypatch.setattr(rc, "parse_info_zip", flaky)

        rc.load_startup_catalog(tmp_path, tmp_path, PLATFORMS)  # must not raise

        assert rc.catalog() == rc.load_bundled_catalog(frozenset())

    def test_load_startup_catalog_is_quiet_about_no_cache(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No cache files yet is the default configuration, not a failure."""
        with caplog.at_level(logging.WARNING):
            rc.load_startup_catalog(tmp_path, tmp_path, PLATFORMS)
        assert not caplog.records
        assert rc.catalog() == rc.load_bundled_catalog(frozenset())

    async def test_refresh_forever_skips_a_fresh_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cache written moments ago is not stale; refresh_once must not run yet."""
        (tmp_path / rc.CACHE_ZIP).write_bytes(b"x")
        calls: list[object] = []

        class Sentinel(Exception):
            """Stops the infinite loop once the first sleep is reached."""

        def fake_refresh_once(*args: object, **kwargs: object) -> bool:
            """Record that it ran; it must never be called in this test.

            Args:
                args: Unused.
                kwargs: Unused.

            Returns:
                False.
            """
            calls.append((args, kwargs))
            return False

        async def fake_sleep(seconds: float) -> None:
            """Stand in for `anyio.sleep`, ending the loop instead of waiting.

            Args:
                seconds: Unused.

            Raises:
                Sentinel: Always, once control reaches the loop's sleep.
            """
            raise Sentinel

        monkeypatch.setattr(rc, "refresh_once", fake_refresh_once)
        monkeypatch.setattr(rc.anyio, "sleep", fake_sleep)

        with pytest.raises(Sentinel):
            await rc.refresh_forever(tmp_path, tmp_path, PLATFORMS)

        assert calls == []

    async def test_refresh_forever_survives_a_crash_and_keeps_looping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception escaping refresh_once must not end the loop."""
        calls: list[float] = []

        class Sentinel(Exception):
            """Stops the infinite loop once the second sleep is reached."""

        def fake_refresh_once(*args: object, **kwargs: object) -> bool:
            """Simulate a bug that escapes refresh_once's own error handling.

            Args:
                args: Unused.
                kwargs: Unused.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("boom")

        async def fake_sleep(seconds: float) -> None:
            """Record each call, ending the loop on the second one.

            Args:
                seconds: The seconds `refresh_forever` asked to sleep.

            Raises:
                Sentinel: Once this is the second call.
            """
            calls.append(seconds)
            if len(calls) >= 2:
                raise Sentinel

        monkeypatch.setattr(rc, "refresh_once", fake_refresh_once)
        monkeypatch.setattr(rc.anyio, "sleep", fake_sleep)

        with pytest.raises(Sentinel):
            await rc.refresh_forever(tmp_path, tmp_path, PLATFORMS)

        assert len(calls) == 2

    @pytest.mark.parametrize(("refreshed", "wait"), [(False, rc.REFRESH_RETRY), (True, rc.REFRESH_EVERY)])
    async def test_refresh_forever_retries_a_failure_within_the_hour(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refreshed: bool, wait: int
    ) -> None:
        """A failed refresh is retried after `REFRESH_RETRY`, not a full 7 days later."""
        sleeps: list[float] = []

        class Sentinel(Exception):
            """Stops the infinite loop once the first sleep is reached."""

        def fake_refresh_once(*args: object, **kwargs: object) -> bool:
            """Report the parametrized outcome.

            Args:
                args: Unused.
                kwargs: Unused.

            Returns:
                Whether the refresh succeeded.
            """
            return refreshed

        async def fake_sleep(seconds: float) -> None:
            """Record the wait and end the loop.

            Args:
                seconds: The seconds `refresh_forever` asked to sleep.

            Raises:
                Sentinel: Always.
            """
            sleeps.append(seconds)
            raise Sentinel

        monkeypatch.setattr(rc, "refresh_once", fake_refresh_once)
        monkeypatch.setattr(rc.anyio, "sleep", fake_sleep)

        with pytest.raises(Sentinel):
            await rc.refresh_forever(tmp_path, tmp_path, PLATFORMS)

        assert sleeps == [wait]

    def test_http_fetch_enforces_the_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A response over `cap` bytes is cut off before it is fully buffered."""

        class FakeResponse:
            """Stands in for `httpx.Response`."""

            def raise_for_status(self) -> None:
                """Do nothing; the fake response is never an error."""

            def iter_bytes(self) -> Iterator[bytes]:
                """Yield a single chunk over the cap under test.

                Yields:
                    One 10-byte chunk.
                """
                yield b"x" * 10

        class FakeStream:
            """Stands in for the context manager `httpx.stream` returns."""

            def __enter__(self) -> "FakeResponse":
                """Return the fake response.

                Returns:
                    The fake response.
                """
                return FakeResponse()

            def __exit__(self, *exc: object) -> None:
                """Do nothing; the fake stream needs no cleanup.

                Args:
                    exc: Unused.
                """

        monkeypatch.setattr(rc.httpx, "stream", lambda *a, **k: FakeStream())

        with pytest.raises(ValueError, match="over"):
            rc._http_fetch("https://example.invalid/x", 5)

    def test_http_fetch_enforces_the_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`timeout=60` is per read, so a trickling server needs a total deadline too."""
        times = iter([0.0, 0.0, rc.FETCH_DEADLINE + 1])
        monkeypatch.setattr(rc.time, "monotonic", lambda: next(times))

        class FakeResponse:
            """Stands in for `httpx.Response`, trickling one byte at a time."""

            def raise_for_status(self) -> None:
                """Do nothing; the fake response is never an error."""

            def iter_bytes(self) -> Iterator[bytes]:
                """Yield chunks well under the cap, so only the deadline can stop this.

                Yields:
                    Two 1-byte chunks.
                """
                yield b"a"
                yield b"b"

        class FakeStream:
            """Stands in for the context manager `httpx.stream` returns."""

            def __enter__(self) -> "FakeResponse":
                """Return the fake response.

                Returns:
                    The fake response.
                """
                return FakeResponse()

            def __exit__(self, *exc: object) -> None:
                """Do nothing; the fake stream needs no cleanup.

                Args:
                    exc: Unused.
                """

        monkeypatch.setattr(rc.httpx, "stream", lambda *a, **k: FakeStream())

        with pytest.raises(ValueError, match="took over"):
            rc._http_fetch("https://example.invalid/x", 1_000_000)

    def test_refresh_does_not_change_a_running_profile(self) -> None:
        """A resolved profile is a snapshot.

        This cannot fail as written, because `resolve` builds its
        profile from the module-level `CATALOG` fixture it is passed
        explicitly, not from `rc.catalog()`. Kept anyway, alongside the test
        below that drives the same scenario through a real `Retroarch`
        session, which does read `rc.catalog()`.
        """
        rc.set_catalog(CATALOG)
        profile = resolve("snes", "beetle_snes", experimental=True)
        rc.set_catalog(rc.build_catalog(info_zip({}), ""))
        assert profile["core"] == "beetle_snes" and profile["extensions"] == (".sfc", ".smc")

    def test_refresh_does_not_change_a_running_retroarch_session(self) -> None:
        """Review Focus 5, through a real session: a catalog swap never touches a resolved profile.

        `Retroarch._profile` resolves once per (platform, core) pair and
        caches the result on the instance, so a refresh swapping `rc.catalog()`
        out from under a session already playing must not move it.
        """
        rc.set_catalog(CATALOG)
        emu = retroarch.Retroarch()
        emu.platform = "snes"
        emu.core = "beetle_snes"
        emu.experimental_cores = True
        emu.select_core()
        profile = emu._profile()
        core = emu.archive_core()

        rc.set_catalog(rc.build_catalog(info_zip({}), ""))

        assert emu._profile() is profile
        assert emu.archive_core() == core == "beetle_snes"


def test_lifespan_starts_the_refresh_task_when_opted_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RETROARCH_CORE_INFO_REFRESH starts the background loop, which runs once at once.

    The app still shuts down cleanly: the lifespan's task group cancels the
    loop rather than waiting out its 7 day sleep.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
        tmp_path: The per-test temporary directory.
    """
    monkeypatch.setattr(settings, "RETROARCH_CORE_INFO_REFRESH", True)
    monkeypatch.setattr(retroarch, "RA_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(retroarch, "CORES_DIR", tmp_path / "cores")

    ran = threading.Event()
    calls: list[Path] = []

    def fake_refresh_once(
        cache_dir: Path, cores_dir: Path, *, fetch: Callable[[str, int], bytes], platforms: Mapping[str, Any]
    ) -> bool:
        """Record the call instead of touching the network.

        Args:
            cache_dir: The cache dir `refresh_forever` was given.
            cores_dir: The cores dir `refresh_forever` was given.
            fetch: Unused; `refresh_forever` always passes `_http_fetch`.
            platforms: Unused; `refresh_forever` always passes the real platform table.

        Returns:
            False, so no catalog swap is attempted.
        """
        del fetch, platforms
        calls.append(cache_dir)
        ran.set()
        return False

    monkeypatch.setattr(rc, "refresh_once", fake_refresh_once)
    monkeypatch.setattr(settings, "BROKER_SECRET", "")
    monkeypatch.setattr(settings, "DEV_MODE", True)
    app = create_app()
    monkeypatch.setattr(settings, "DEV_MODE", False)
    with TestClient(app):
        assert ran.wait(5), "refresh_forever never called refresh_once"
    assert calls == [tmp_path / "data"]
