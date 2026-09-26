"""Offline checks on the bundled RetroArch catalog data (§10.2); no network."""

import hashlib
import json

from webstation_broker.emulators import retroarch
from webstation_broker.emulators import retroarch_cores as rc

_SOURCE_KEYS = {"url", "index_url", "sha256", "commit", "date", "x86_64_cores"}


def test_source_file_has_exactly_the_known_keys() -> None:
    """Unknown keys are rejected so a typo cannot hide a missing field."""
    assert set(json.loads(rc.BUNDLED_SOURCE.read_text())) == _SOURCE_KEYS


def test_bundled_zip_matches_its_recorded_sha256() -> None:
    """A zip replaced without `--write` is caught here, not at startup in a container."""
    source = json.loads(rc.BUNDLED_SOURCE.read_text())
    assert hashlib.sha256(rc.BUNDLED_ZIP.read_bytes()).hexdigest() == source["sha256"]


def test_bundled_catalog_loads_and_is_not_empty() -> None:
    """The bundle parses and intersects to a real catalog."""
    catalog = rc.load_bundled_catalog()
    assert "snes9x" in catalog.cores
    assert catalog.cores["snes9x"].extensions


def test_no_default_core_is_blocked_on_its_platform() -> None:
    """§5.2: a platform's own core is `default` there."""
    for slug, info in retroarch.PLATFORMS.items():
        entry = rc.TIERS.get(info["core"])
        if entry and entry.tier == "blocked":
            assert entry.platforms is not None and slug not in entry.platforms, slug


def test_alternates_and_vetted_tiers_agree() -> None:
    """Every alternate is vetted, and every vetted core has an alternate somewhere."""
    alternates = {core for info in retroarch.PLATFORMS.values() for core in info.get("alternates", {})}
    vetted = {core for core, entry in rc.TIERS.items() if entry.tier == "vetted"}
    assert alternates == vetted


def test_library_names_match_core_info_or_a_fix() -> None:
    """A default or alternate library_name is core-info's corename, or listed in LIBRARY_NAME_FIXES."""
    catalog = rc.load_bundled_catalog()
    for info in retroarch.PLATFORMS.values():
        pairs = [(info["core"], info["library_name"])] + [
            (c, a["library_name"]) for c, a in info.get("alternates", {}).items()
        ]
        for core, lib in pairs:
            if core not in catalog.cores:
                continue  # core_source cores; see test_every_default_and_vetted_core_is_in_the_catalog
            expected = rc.LIBRARY_NAME_FIXES.get(core, catalog.cores[core].corename)
            assert lib == expected, core


def test_no_library_name_fix_is_stale() -> None:
    """A fix equal to corename is no longer needed and fails."""
    catalog = rc.load_bundled_catalog()
    for core, lib in rc.LIBRARY_NAME_FIXES.items():
        if core in catalog.cores:
            assert catalog.cores[core].corename != lib, core


def test_every_default_and_vetted_core_is_in_the_catalog() -> None:
    """Unless it has a core_source (azahar), every default and alternate is buildbot-built."""
    catalog = rc.load_bundled_catalog()
    for slug, info in retroarch.PLATFORMS.items():
        if "core_source" not in info:
            assert info["core"] in catalog.cores, slug
        for core in info.get("alternates", {}):
            assert core in catalog.cores, (slug, core)


def test_table_extensions_are_a_subset_of_the_cores_plus_extra() -> None:
    """§5.4: an entry's extensions are what the core supports plus its extra_extensions."""
    catalog = rc.load_bundled_catalog()
    for slug, info in retroarch.PLATFORMS.items():
        if info["core"] not in catalog.cores:
            continue
        allowed = set(catalog.cores[info["core"]].extensions) | set(info.get("extra_extensions", ()))
        assert set(info["extensions"]) <= allowed, (slug, sorted(set(info["extensions"]) - allowed))
        for core, alt in info.get("alternates", {}).items():
            allowed = set(catalog.cores[core].extensions) | set(alt.get("extra_extensions", ()))
            assert set(info["extensions"]) & allowed, (slug, core)
