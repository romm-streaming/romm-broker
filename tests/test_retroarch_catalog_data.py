"""Offline checks on the bundled RetroArch catalog data (§10.2); no network."""

import ctypes
import hashlib
import importlib.util
import json
import re
import types
from pathlib import Path

import pytest

from webstation_broker.emulators import retroarch
from webstation_broker.emulators import retroarch_cores as rc

_SOURCE_KEYS = {"url", "index_url", "sha256", "commit", "date", "x86_64_cores"}
_ROOT = Path(__file__).resolve().parent.parent


def _sync_module() -> types.ModuleType:
    """Load scripts/sync_core_info.py as a module.

    Returns:
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location("sync_core_info", _ROOT / "scripts" / "sync_core_info.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_melonds_is_blocked_on_every_ds_platform() -> None:
    """M3: the tiers file blocks melonds, which the docs say crashes on launch."""
    catalog = rc.load_bundled_catalog()
    entry = rc.TIERS["melonds"]
    assert entry.tier == "blocked" and entry.reason and entry.platforms is None
    for slug in ("nds", "nintendo-dsi"):
        assert rc.tier_of(retroarch.PLATFORMS, slug, "melonds", catalog, rc.TIERS) == "blocked"


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


def test_docs_tier_table_is_current() -> None:
    """Run `scripts/sync_core_info.py --docs` after changing the table or tiers."""
    sync = _sync_module()
    doc = (_ROOT / "docs/content/docs/emulators/retroarch-cores.mdx").read_text()
    start, end = "{/* core-tiers:start */}", "{/* core-tiers:end */}"
    current = doc.split(start, 1)[1].split(end, 1)[0].strip()
    assert current == sync.render_tier_table(retroarch.PLATFORMS, rc.load_bundled_catalog(), rc.TIERS).strip()


def _expected_probe_cores() -> dict[str, str]:
    """Every default and alternate core `probe()` should check, mapped to its table `library_name`.

    Returns:
        Core name to the `library_name` the platform table records for it.
    """
    reported: dict[str, str] = {}
    for info in retroarch.PLATFORMS.values():
        reported[info["core"]] = info["library_name"]
        for core, alt in info.get("alternates", {}).items():
            reported[core] = alt["library_name"]
    return reported


class _FakeLib:
    """Stands in for `ctypes.CDLL(so)`; reports whatever `reported` says for that core."""

    def __init__(self, sync: types.ModuleType, reported: dict[str, str], path: str) -> None:
        """Remember which core this fake `.so` path is for.

        Args:
            sync: The loaded `sync_core_info` module, for its `_SystemInfo` struct.
            reported: Core name to the `library_name` it should report.
            path: The `.so` path `probe()` passed to `ctypes.CDLL`.
        """
        self._sync = sync
        self._reported = reported
        self._core = Path(path).name.removesuffix("_libretro.so")

    def retro_get_system_info(self, ptr: object) -> None:
        """Write `self._reported[self._core]` into the struct `ptr` points at.

        Args:
            ptr: The `ctypes.byref(sysinfo)` `probe()` called with.
        """
        info = ctypes.cast(ptr, ctypes.POINTER(self._sync._SystemInfo)).contents
        info.library_name = self._reported[self._core].encode()


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch, sync: types.ModuleType, reported: dict[str, str], downloaded: list[str]
) -> None:
    """Monkeypatch every network/native call `probe()` makes.

    Args:
        monkeypatch: The fixture.
        sync: The loaded `sync_core_info` module.
        reported: Core name to the `library_name` the fake core reports.
        downloaded: Appended with each core name `_download_core_bytes` is called for.
    """

    def fake_download(core: str, url: str) -> bytes:
        downloaded.append(core)
        return b"not a real shared object"

    def fake_core_url(core: str, source: object) -> str:
        return f"https://example.invalid/{core}"

    def fake_cdll(path: str) -> _FakeLib:
        return _FakeLib(sync, reported, path)

    monkeypatch.setattr(sync.ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(sync.retroarch, "_download_core_bytes", fake_download)
    monkeypatch.setattr(sync.retroarch, "_core_url", fake_core_url)


def test_probe_downloads_each_core_once_and_aggregates_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe() covers every default and alternate, dedups a shared core, and keeps going past a mismatch."""
    sync = _sync_module()
    reported = _expected_probe_cores()
    # A core used by several platforms (e.g. genesis_plus_gx, fbneo) proves the dedup: 71
    # (slug, core) pairs across the table resolve to only 49 distinct cores.
    assert len(reported) < sum(
        1 + len(info.get("alternates", {})) for info in retroarch.PLATFORMS.values()
    )
    mismatched = set(list(reported)[:2])
    for core in mismatched:
        reported[core] = "wrong-library-name"

    downloaded: list[str] = []
    _patch_probe(monkeypatch, sync, reported, downloaded)

    assert sync.probe() == 1

    # Every default and alternate was checked, and each unique core downloaded exactly once
    # even though several platforms share a core.
    assert set(downloaded) == set(reported)
    assert len(downloaded) == len(reported)

    out = capsys.readouterr().out
    for core in mismatched:
        assert re.search(rf"FAIL \S+/{re.escape(core)}:", out), (core, out)
    ok_core = next(iter(set(reported) - mismatched))
    assert re.search(rf"ok\s+\S+/{re.escape(ok_core)}:", out), (ok_core, out)


def test_probe_returns_0_when_every_core_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clean path: no mismatch anywhere, exit code 0."""
    sync = _sync_module()
    reported = _expected_probe_cores()
    _patch_probe(monkeypatch, sync, reported, [])

    assert sync.probe() == 0


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
