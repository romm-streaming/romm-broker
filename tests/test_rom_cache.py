"""Tests for the opt-in local ROM copy cache."""

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import NoReturn, Optional

import pytest

from webstation_broker import settings
from webstation_broker.emulators import REGISTRY, duckstation, flycast, retroarch, rom_cache
from webstation_broker.emulators.base import Emulator

_DAY = 86400.0
"""Seconds per day, the unit `ROM_CACHE_MAX_AGE_DAYS` is expressed in."""


class _Cacheable(Emulator):
    """A launcher that opts in to the ROM cache and never spawns anything."""

    name = "cacheable"
    rom_cacheable = True


class _NotCacheable(Emulator):
    """A launcher that keeps the default opt-out."""

    name = "uncacheable"


@pytest.fixture
def roms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ROM_ROOT at a temp library and turn the cache on in blocking mode.

    Blocking is the default here because it finishes inside the call, which
    keeps most tests free of thread joins. Every limit starts off so a test
    only sees the one it sets.

    Args:
        monkeypatch: Pytest's attribute patcher.
        tmp_path: The test's temp directory.

    Returns:
        The library root.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(settings, "ROM_ROOT", root)
    monkeypatch.setattr(settings, "ROM_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "ROM_CACHE_MODE", "blocking")
    monkeypatch.setattr(settings, "ROM_CACHE_MAX_GB", 0.0)
    monkeypatch.setattr(settings, "ROM_CACHE_MAX_COUNT", 0)
    monkeypatch.setattr(settings, "ROM_CACHE_MAX_AGE_DAYS", 0.0)
    monkeypatch.setattr(settings, "ROM_CACHE_COPY_MBPS", 0.0)
    monkeypatch.setattr(settings, "ROM_CACHE_COPY_TIMEOUT", 60.0)
    return root.resolve()


@pytest.fixture
def cache_root(roms: Path) -> Path:
    """The redirected cache directory the autouse conftest fixture set up."""
    return settings.ROM_CACHE_DIR


def _write(path: Path, content: bytes = b"rom", mtime: Optional[float] = None) -> Path:
    """Write a file, creating its parents, with an optional mtime.

    Args:
        path: Where the file goes.
        content: Its bytes.
        mtime: Access and modify time to set, or None to leave the current one.

    Returns:
        The written path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _entries(cache_root: Path) -> list[str]:
    """Names of the cache entries currently in place, scratch excluded."""
    if not cache_root.is_dir():
        return []
    return sorted(p.name for p in cache_root.iterdir() if p.is_dir() and p.name != ".scratch")


def _cache(rom: Path, emulator: Optional[Emulator] = None) -> Path:
    """Run `boot_path` for a single-file ROM, the shape most tests need."""
    return rom_cache.boot_path(rom, rom, emulator or _Cacheable())


def _set_last_launched(cached: Path, when: float) -> None:
    """Backdate the last launch of the entry `cached` lives in."""
    entry = rom_cache.entry_of(cached)
    assert entry is not None
    marker = entry / rom_cache.LAST_ACCESSED_MARKER
    os.utime(marker, (when, when))


_OPTED_IN = {
    "azahar", "cemu", "dolphin", "duckstation", "eden", "flycast", "pcsx2", "retroarch", "xemu", "xenia",
}
"""The launchers checked against the `Emulator.rom_cacheable` checklist."""


def test_the_launchers_that_opt_in_are_the_checked_ones() -> None:
    """A launcher that turns the cache on fails here until it has been checked.

    Opting in without the `Emulator.rom_cacheable` checklist breaks quietly: a
    resume state that never matches, a disc swap refused, a missing BIOS set.
    """
    opted_in = {name for name, cls in REGISTRY.items() if cls.rom_cacheable is not False}
    assert opted_in == _OPTED_IN


class TestOffByDefault:
    """With the cache off or the launcher opted out, a launch is exactly what it was."""

    def test_disabled_returns_the_library_path_and_writes_nothing(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROM_CACHE_ENABLED unset boots from ROM_ROOT without creating the cache dir."""
        monkeypatch.setattr(settings, "ROM_CACHE_ENABLED", False)
        rom = _write(roms / "ps2" / "game.iso")

        assert _cache(rom) == rom
        assert not cache_root.exists()

    def test_a_launcher_that_did_not_opt_in_boots_from_the_library(
        self, roms: Path, cache_root: Path
    ) -> None:
        """An emulator without rom_cacheable is never cached, even with the cache on."""
        rom = _write(roms / "ps2" / "game.iso")

        assert _cache(rom, _NotCacheable()) == rom
        assert _entries(cache_root) == []

    def test_startup_with_the_cache_off_touches_nothing(
        self, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The startup sweep is a no-op when the cache is off."""
        monkeypatch.setattr(settings, "ROM_CACHE_ENABLED", False)

        rom_cache.startup()

        assert not cache_root.exists()

    @pytest.mark.parametrize(
        "module",
        [
            "azahar",
            "cemu",
            "dolphin",
            "duckstation",
            "eden",
            "flycast",
            "pcsx2",
            "xemu",
            "xenia",
        ],
    )
    def test_launchers_without_their_own_cache_opt_in(self, module: str) -> None:
        """Every launcher with no extraction cache of its own is cacheable."""
        from webstation_broker import emulators

        assert emulators.REGISTRY[module].rom_cacheable is True

    @pytest.mark.parametrize("platform", [None, "snes", "psx", "n64"])
    def test_retroarch_opts_in_for_cores_that_read_only_the_rom(self, platform: Optional[str]) -> None:
        """RetroArch is cacheable for a core that boots the one file it is handed."""
        ra = retroarch.Retroarch()
        ra.platform = platform

        assert ra.rom_cacheable is True

    @pytest.mark.parametrize("platform", ["arcade", "cps2", "neogeoaes", "neogeomvs", "model3"])
    def test_retroarch_stays_out_for_cores_that_load_romsets_beside_the_game(self, platform: str) -> None:
        """FBNeo and Supermodel load parent and BIOS zips from the game's folder, which a copy lacks."""
        ra = retroarch.Retroarch()
        ra.platform = platform

        assert ra.rom_cacheable is False

    def test_retroarch_stays_out_for_an_arcade_core_override(self) -> None:
        """A `core:` override to a MAME core is judged by the core, not the platform's default."""
        ra = retroarch.Retroarch()
        ra.platform = "arcade"
        ra.core = "mame2003_plus"

        assert ra.rom_cacheable is False

    @pytest.mark.parametrize("module", ["rpcs3", "shadps4", "ppsspp", "scummvm", "desktop"])
    def test_launchers_with_their_own_cache_stay_out(self, module: str) -> None:
        """Launchers with an extraction cache, or no rom, stay opted out."""
        from webstation_broker import emulators

        assert emulators.REGISTRY[module].rom_cacheable is False


class TestHitAndMiss:
    """Copying on a miss and reusing the copy afterwards."""

    def test_a_blocking_miss_copies_and_boots_the_copy(self, roms: Path, cache_root: Path) -> None:
        """The copy keeps the ROM's path under ROM_ROOT, so parent folder names survive."""
        rom = _write(roms / "library" / "ps2" / "game.iso", b"disc")

        cached = _cache(rom)

        assert cached != rom
        assert cached.is_relative_to(cache_root)
        assert cached.read_bytes() == b"disc"
        assert cached.relative_to(rom_cache.entry_of(cached)) == Path("library/ps2/game.iso")

    def test_a_hit_reuses_the_copy_without_copying_again(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second launch of an unchanged ROM boots the existing copy."""
        rom = _write(roms / "ps2" / "game.iso")
        first = _cache(rom)

        def no_copy(*_args: object, **_kwargs: object) -> NoReturn:
            raise AssertionError("a hit must not copy")

        monkeypatch.setattr(rom_cache, "_populate", no_copy)

        assert _cache(rom) == first

    def test_a_hit_refreshes_the_last_launched_time(self, roms: Path) -> None:
        """Reusing an entry counts as a launch for age and LRU eviction."""
        rom = _write(roms / "ps2" / "game.iso")
        cached = _cache(rom)
        _set_last_launched(cached, time.time() - 10 * _DAY)

        _cache(rom)

        entry = rom_cache.entry_of(cached)
        assert entry is not None
        age = time.time() - (entry / rom_cache.LAST_ACCESSED_MARKER).stat().st_mtime
        assert age < 60

    def test_a_background_miss_boots_the_library_and_serves_the_next_launch(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Background mode never makes the first launch wait."""
        monkeypatch.setattr(settings, "ROM_CACHE_MODE", "background")
        rom = _write(roms / "ps2" / "game.iso", b"disc")

        assert _cache(rom) == rom
        rom_cache.join_background_copy(10.0)

        cached = _cache(rom)
        assert cached.is_relative_to(cache_root)
        assert cached.read_bytes() == b"disc"

    def test_a_replaced_rom_is_copied_fresh(self, roms: Path) -> None:
        """A dump swapped in the library never boots the old copy."""
        rom = _write(roms / "ps2" / "game.iso", b"old", mtime=1_600_000_000)
        _cache(rom)
        _write(rom, b"new dump", mtime=1_700_000_000)

        cached = _cache(rom)

        assert cached.read_bytes() == b"new dump"

    def test_a_stale_copy_is_not_booted_while_a_background_refresh_runs(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In background mode a stale entry falls back to the library, never the old copy."""
        rom = _write(roms / "ps2" / "game.iso", b"old", mtime=1_600_000_000)
        _cache(rom)
        monkeypatch.setattr(settings, "ROM_CACHE_MODE", "background")
        _write(rom, b"new dump", mtime=1_700_000_000)

        assert _cache(rom) == rom
        rom_cache.join_background_copy(10.0)
        assert _cache(rom).read_bytes() == b"new dump"

    def test_a_multi_file_folder_is_copied_whole(self, roms: Path) -> None:
        """A cue/bin set in a folder copies every file, layout kept."""
        folder = roms / "psx" / "Game (USA)"
        cue = _write(folder / "Game.cue", b"FILE track.bin")
        _write(folder / "Game (Track 1).bin", b"one")
        _write(folder / "extras" / "Game (Track 2).bin", b"two")

        cached = rom_cache.boot_path(folder, cue, _Cacheable())

        assert cached.name == "Game.cue"
        assert (cached.parent / "Game (Track 1).bin").read_bytes() == b"one"
        assert (cached.parent / "extras" / "Game (Track 2).bin").read_bytes() == b"two"

    def test_the_manifest_names_the_library_path(self, roms: Path) -> None:
        """Each entry records its source, so a cache dir can be audited by hand."""
        rom = _write(roms / "ps2" / "game.iso")
        cached = _cache(rom)
        entry = rom_cache.entry_of(cached)
        assert entry is not None

        manifest = json.loads((entry / rom_cache.MANIFEST).read_text())

        assert manifest["source"] == str(rom)


class TestSkipped:
    """ROMs the cache leaves on the library, booted exactly as before."""

    @pytest.mark.parametrize("name", ["game.cue", "game.m3u", "game.gdi", "game.ccd", "game.toc", "game.mds"])
    def test_a_lone_sheet_file_is_not_cached(self, roms: Path, cache_root: Path, name: str) -> None:
        """A sheet RomM handed over as a single file names tracks the copy would not have."""
        rom = _write(roms / "psx" / name)

        assert _cache(rom) == rom
        assert _entries(cache_root) == []

    def test_a_folder_holding_a_symlink_is_not_cached(
        self, roms: Path, cache_root: Path, tmp_path: Path
    ) -> None:
        """Copying through a link could pull in a file from outside the library."""
        outside = _write(tmp_path / "elsewhere" / "secret.bin")
        folder = roms / "psx" / "Game"
        cue = _write(folder / "Game.cue")
        (folder / "track.bin").symlink_to(outside)

        assert rom_cache.boot_path(folder, cue, _Cacheable()) == cue
        assert _entries(cache_root) == []

    def test_a_boot_file_outside_the_rom_entry_is_not_cached(
        self, roms: Path, cache_root: Path, tmp_path: Path
    ) -> None:
        """A file a launcher already staged elsewhere (an extraction) is booted as is."""
        rom = _write(roms / "psp" / "game.zip")
        staged = _write(tmp_path / "extracted" / "game.iso")

        assert rom_cache.boot_path(rom, staged, _Cacheable()) == staged
        assert _entries(cache_root) == []

    def test_a_rom_bigger_than_the_size_cap_is_not_cached(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A game that could never fit is booted from the library, without evicting anything."""
        keep = _cache(_write(roms / "a.iso", b"a"))
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_GB", 1e-9)
        rom = _write(roms / "big.iso", b"x" * 4096)

        assert _cache(rom) == rom
        assert keep.exists()


class TestFailuresFallBack:
    """Whatever goes wrong with a copy, the game still launches from the library."""

    def test_no_free_space_boots_the_library(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A copy that would eat into the free-space reserve is skipped and logged."""
        rom = _write(roms / "ps2" / "game.iso", b"x" * 1024)
        monkeypatch.setattr(
            rom_cache.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(1000, 999, 1)  # type: ignore[attr-defined]
        )

        assert _cache(rom) == rom
        assert _entries(cache_root) == []
        assert "free" in caplog.text

    def test_a_copy_that_fails_partway_leaves_nothing_behind(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An I/O error mid-copy boots the library and clears the scratch copy."""
        rom = _write(roms / "ps2" / "game.iso", b"x" * (3 * 1024))
        monkeypatch.setattr(rom_cache, "_CHUNK", 1024)
        real_write = rom_cache._write_chunk
        calls = []

        def flaky(fo: object, chunk: bytes) -> None:
            calls.append(len(chunk))
            if len(calls) == 2:
                raise OSError(5, "Input/output error")
            real_write(fo, chunk)  # type: ignore[arg-type]

        monkeypatch.setattr(rom_cache, "_write_chunk", flaky)

        assert _cache(rom) == rom
        assert _entries(cache_root) == []
        assert not any((cache_root / ".scratch").iterdir())
        assert "Input/output error" in caplog.text

    def test_a_blocking_copy_past_its_timeout_boots_the_library(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ROM_CACHE_COPY_TIMEOUT bounds how long a blocking launch waits."""
        # A nanosecond: past the deadline by the first chunk, whatever the disk.
        monkeypatch.setattr(settings, "ROM_CACHE_COPY_TIMEOUT", 1e-9)
        rom = _write(roms / "ps2" / "game.iso")

        assert _cache(rom) == rom
        assert _entries(cache_root) == []
        assert "timed out" in caplog.text

    def test_a_copy_stalled_inside_a_read_still_boots_the_library_at_the_timeout(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read hung on a stalled NFS mount must not hold the launch past the timeout."""
        monkeypatch.setattr(settings, "ROM_CACHE_COPY_TIMEOUT", 0.2)
        rom = _write(roms / "ps2" / "game.iso")
        stalled = threading.Event()
        unstall = threading.Event()

        def hang(fo: object, chunk: bytes) -> None:
            stalled.set()
            unstall.wait(10)

        monkeypatch.setattr(rom_cache, "_write_chunk", hang)
        result: list[Path] = []
        launch = threading.Thread(target=lambda: result.append(_cache(rom)))
        launch.start()
        launch.join(3)
        returned = not launch.is_alive()
        unstall.set()
        launch.join(5)
        rom_cache.join_background_copy(5)

        assert stalled.is_set()
        assert returned, "the launch waited out the stalled read instead of the timeout"
        assert result == [rom]
        assert _entries(cache_root) == []

    def test_a_rom_changed_during_the_copy_is_discarded(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A source rewritten mid-copy must not be kept as if it were whole."""
        rom = _write(roms / "ps2" / "game.iso", b"x" * 2048, mtime=1_600_000_000)
        monkeypatch.setattr(rom_cache, "_CHUNK", 1024)
        real_write = rom_cache._write_chunk

        def rewrite_source(fo: object, chunk: bytes) -> None:
            real_write(fo, chunk)  # type: ignore[arg-type]
            os.utime(rom, (1_700_000_000, 1_700_000_000))

        monkeypatch.setattr(rom_cache, "_write_chunk", rewrite_source)

        assert _cache(rom) == rom
        assert _entries(cache_root) == []

    def test_an_unexpected_error_never_fails_the_launch(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """boot_path swallows any error and falls back to the library."""

        def boom(*_args: object, **_kwargs: object) -> NoReturn:
            raise ValueError("unexpected")

        monkeypatch.setattr(rom_cache, "_fingerprint", boom)
        rom = _write(roms / "ps2" / "game.iso")

        assert _cache(rom) == rom


class TestConcurrency:
    """One copy at a time, and never two copies of one game."""

    def test_a_launch_during_another_copy_boots_the_library(
        self, roms: Path, cache_root: Path
    ) -> None:
        """While a copy holds the copy slot, a second launch does not wait or start its own."""
        rom = _write(roms / "ps2" / "game.iso")
        with rom_cache._copy_slot:
            assert _cache(rom) == rom
        assert _entries(cache_root) == []

    def test_a_background_copy_sets_no_launch_phase(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a blocking copy holds the launch up, so only it reports a phase."""
        monkeypatch.setattr(settings, "ROM_CACHE_MODE", "background")
        emulator = _Cacheable()
        _cache(_write(roms / "ps2" / "game.iso"), emulator)

        assert emulator.extraction_phase is None

    def test_a_blocking_copy_reports_its_phase_and_clears_it(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RomM polls extraction_phase during activate, so a blocking copy shows as copying_rom."""
        emulator = _Cacheable()
        seen = []
        real_write = rom_cache._write_chunk

        def watch(fo: object, chunk: bytes) -> None:
            seen.append(emulator.extraction_phase)
            real_write(fo, chunk)  # type: ignore[arg-type]

        monkeypatch.setattr(rom_cache, "_write_chunk", watch)

        _cache(_write(roms / "ps2" / "game.iso"), emulator)

        assert seen == [rom_cache.PHASE]
        assert emulator.extraction_phase is None

    def test_a_background_copy_is_rate_limited(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROM_CACHE_COPY_MBPS paces a background copy so the session's own reads keep up."""
        monkeypatch.setattr(settings, "ROM_CACHE_MODE", "background")
        monkeypatch.setattr(settings, "ROM_CACHE_COPY_MBPS", 1.0)
        monkeypatch.setattr(rom_cache, "_CHUNK", 1_000_000)
        slept = []
        monkeypatch.setattr(rom_cache, "_sleep", lambda s: slept.append(s))

        _cache(_write(roms / "ps2" / "game.iso", b"x" * 2_000_000))
        rom_cache.join_background_copy(10.0)

        assert sum(slept) > 1.5

    def test_a_blocking_copy_is_never_rate_limited(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The player is waiting on a blocking copy, so the cap does not apply."""
        monkeypatch.setattr(settings, "ROM_CACHE_COPY_MBPS", 0.001)
        slept = []
        monkeypatch.setattr(rom_cache, "_sleep", lambda s: slept.append(s))

        _cache(_write(roms / "ps2" / "game.iso", b"x" * 4096))

        assert slept == []

    def test_concurrent_launches_of_one_rom_copy_it_once(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two threads racing on one ROM leave a single, whole entry."""
        rom = _write(roms / "ps2" / "game.iso", b"x" * 4096)
        monkeypatch.setattr(rom_cache, "_CHUNK", 512)
        results: list[Path] = []
        barrier = threading.Barrier(2)

        def launch() -> None:
            barrier.wait()
            results.append(_cache(rom))

        threads = [threading.Thread(target=launch) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(_entries(cache_root)) == 1
        assert all(r == rom or r.read_bytes() == b"x" * 4096 for r in results)


class TestEviction:
    """TIME, COUNT and FILESIZE, alone and together."""

    def test_age_evicts_games_not_launched_within_the_limit(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROM_CACHE_MAX_AGE_DAYS drops entries idle longer than the limit."""
        old = _cache(_write(roms / "old.iso"))
        fresh = _cache(_write(roms / "fresh.iso"))
        _set_last_launched(old, time.time() - 40 * _DAY)
        _set_last_launched(fresh, time.time() - 5 * _DAY)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_AGE_DAYS", 30.0)

        rom_cache.evict()

        assert not old.exists()
        assert fresh.exists()

    def test_count_keeps_the_most_recently_launched(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROM_CACHE_MAX_COUNT drops the least recently launched first."""
        now = time.time()
        cached = [_cache(_write(roms / f"g{i}.iso")) for i in range(3)]
        for i, c in enumerate(cached):
            _set_last_launched(c, now - (3 - i) * 60)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_COUNT", 2)

        rom_cache.evict()

        assert [c.exists() for c in cached] == [False, True, True]

    def test_an_eviction_cut_short_leaves_no_entry_to_boot(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry is moved into scratch before it is deleted, so a delete cut short is never booted."""
        stale = _cache(_write(roms / "old.iso"))
        _set_last_launched(stale, time.time() - 60)
        kept = rom_cache.entry_of(_cache(_write(roms / "new.iso")))
        assert kept is not None
        real_rmtree = shutil.rmtree

        def fails(*_args: object, **_kwargs: object) -> NoReturn:
            raise OSError("device or resource busy")

        monkeypatch.setattr(shutil, "rmtree", fails)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_COUNT", 1)

        rom_cache.evict()

        assert _entries(cache_root) == [kept.name]
        monkeypatch.setattr(shutil, "rmtree", real_rmtree)
        rom_cache.startup()
        assert list((cache_root / ".scratch").iterdir()) == []

    def test_size_evicts_least_recently_launched_until_under_the_cap(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROM_CACHE_MAX_GB drops the least recently launched until the total fits."""
        now = time.time()
        cached = [_cache(_write(roms / f"g{i}.iso", b"x" * 1000)) for i in range(3)]
        for i, c in enumerate(cached):
            _set_last_launched(c, now - (3 - i) * 60)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_GB", 2500 / rom_cache._GB)

        rom_cache.evict()

        assert [c.exists() for c in cached] == [False, True, True]

    def test_the_strictest_limit_wins(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With all three set, whichever is tightest decides what goes."""
        now = time.time()
        cached = [_cache(_write(roms / f"g{i}.iso", b"x" * 1000)) for i in range(4)]
        for i, c in enumerate(cached):
            _set_last_launched(c, now - (4 - i) * 60)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_AGE_DAYS", 30.0)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_COUNT", 3)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_GB", 1500 / rom_cache._GB)

        rom_cache.evict()

        assert [c.exists() for c in cached] == [False, False, False, True]

    def test_a_copy_makes_room_for_itself_first(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The count limit counts the game about to be copied."""
        first = _cache(_write(roms / "a.iso"))
        _set_last_launched(first, time.time() - 60)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_COUNT", 1)

        second = _cache(_write(roms / "b.iso"))

        assert not first.exists()
        assert second.exists()

    def test_recopying_a_changed_rom_does_not_count_its_old_copy(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A changed ROM replaces its own entry, so the count limit has no room to make."""
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_COUNT", 2)
        other = _cache(_write(roms / "a.iso", b"a", mtime=1_600_000_000))
        rom = _write(roms / "b.iso", b"b", mtime=1_600_000_000)
        _cache(rom)
        _set_last_launched(other, time.time() - 60)
        _write(rom, b"b2", mtime=1_700_000_000)

        recopied = _cache(rom)

        assert other.exists()
        assert recopied.read_bytes() == b"b2"

    def test_recopying_a_changed_rom_does_not_count_its_old_bytes(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size limit weighs the new copy in place of the old one, not on top of it."""
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_GB", 2500 / rom_cache._GB)
        other = _cache(_write(roms / "a.iso", b"x" * 1000, mtime=1_600_000_000))
        rom = _write(roms / "b.iso", b"y" * 1000, mtime=1_600_000_000)
        _cache(rom)
        _set_last_launched(other, time.time() - 60)
        _write(rom, b"z" * 1000, mtime=1_700_000_000)

        recopied = _cache(rom)

        assert other.exists()
        assert recopied.read_bytes() == b"z" * 1000

    def test_the_game_being_played_is_never_evicted(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The entry the last launch booted from survives even when it breaks every limit."""
        playing = _cache(_write(roms / "a.iso"))
        _set_last_launched(playing, time.time() - 400 * _DAY)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_AGE_DAYS", 30.0)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_COUNT", 1)

        rom_cache.evict()

        assert playing.exists()

    def test_a_limit_of_zero_is_off(self, roms: Path, cache_root: Path) -> None:
        """No limit set means nothing is evicted, however old or many."""
        cached = [_cache(_write(roms / f"g{i}.iso")) for i in range(3)]
        for c in cached:
            _set_last_launched(c, time.time() - 1000 * _DAY)

        rom_cache.evict()

        assert all(c.exists() for c in cached)

    def test_startup_clears_orphaned_scratch_and_evicts(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copy cut short by a restart is swept, and the limits apply before any launch."""
        old = _cache(_write(roms / "old.iso"))
        _set_last_launched(old, time.time() - 40 * _DAY)
        orphan = _write(cache_root / ".scratch" / "game-dead" / "ps2" / "game.iso")
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_AGE_DAYS", 30.0)
        monkeypatch.setattr(rom_cache, "_active", None)

        rom_cache.startup()

        assert not orphan.exists()
        assert not old.exists()


class TestMisconfiguration:
    """A cache dir pointed somewhere it should not be costs caching, never data."""

    def test_eviction_never_touches_a_folder_it_did_not_make(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only folders holding a cache manifest are entries, so a shared dir keeps its own data."""
        foreign = _write(cache_root / "emulator-data" / "settings.ini", mtime=time.time() - 400 * _DAY)
        os.utime(foreign.parent, (time.time() - 400 * _DAY,) * 2)
        _cache(_write(roms / "game.iso"))
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_AGE_DAYS", 30.0)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_COUNT", 1)

        rom_cache.evict()
        rom_cache.startup()

        assert foreign.read_bytes() == b"rom"

    def test_the_library_as_cache_dir_is_refused_and_left_alone(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROM_CACHE_DIR=ROM_ROOT boots from the library and never evicts a game folder."""
        game = _write(roms / "ps2" / "game.iso", mtime=time.time() - 400 * _DAY)
        os.utime(game.parent, (time.time() - 400 * _DAY,) * 2)
        monkeypatch.setattr(settings, "ROM_CACHE_DIR", roms)
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_AGE_DAYS", 30.0)

        rom_cache.startup()
        booted = rom_cache.boot_path(game, game, _Cacheable())

        assert booted == game
        assert game.read_bytes() == b"rom"
        assert sorted(p.name for p in roms.iterdir()) == ["ps2"]

    def test_a_cache_dir_inside_the_library_is_refused(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cache on the library mount would copy the slow disk onto itself."""
        game = _write(roms / "game.iso")
        monkeypatch.setattr(settings, "ROM_CACHE_DIR", roms / "cache")

        assert rom_cache.boot_path(game, game, _Cacheable()) == game
        assert not (roms / "cache").exists()

    def test_a_cache_dir_holding_the_library_is_refused(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cache dir above ROM_ROOT would count the library's folders as entries."""
        game = _write(roms / "game.iso")
        monkeypatch.setattr(settings, "ROM_CACHE_DIR", roms.parent)

        assert rom_cache.boot_path(game, game, _Cacheable()) == game
        assert not (roms.parent / ".scratch").exists()

    @pytest.mark.parametrize("timeout", [0.0, -5.0])
    def test_a_timeout_of_zero_or_less_uses_the_default(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch, timeout: float
    ) -> None:
        """Read literally, a timeout at or below 0 would fail every blocking copy on its first chunk."""
        game = _write(roms / "game.iso")
        monkeypatch.setattr(settings, "ROM_CACHE_COPY_TIMEOUT", timeout)

        assert rom_cache.entry_of(rom_cache.boot_path(game, game, _Cacheable())) is not None


class TestDamagedAndOddState:
    """Whatever is on disk or in the library, a launch still boots."""

    @pytest.mark.parametrize("manifest", ["{not json", "[1, 2]", '{"source": "x"}'])
    def test_a_damaged_manifest_is_copied_over(self, roms: Path, cache_root: Path, manifest: str) -> None:
        """An entry whose manifest cannot vouch for it is a miss, and the copy replaces it."""
        rom = _write(roms / "game.iso")
        entry = rom_cache.entry_of(_cache(rom))
        assert entry is not None
        (entry / rom_cache.MANIFEST).write_text(manifest)

        booted = _cache(rom)

        assert booted.read_bytes() == b"rom"
        assert json.loads((entry / rom_cache.MANIFEST).read_text())["source"] == str(rom)

    def test_an_entry_missing_its_boot_file_is_copied_over(self, roms: Path, cache_root: Path) -> None:
        """A copy someone pruned by hand is never handed to the emulator."""
        rom = _write(roms / "game.iso")
        _cache(rom).unlink()

        booted = _cache(rom)

        assert booted.read_bytes() == b"rom"

    def test_first_enable_starts_from_no_cache_dir(self, roms: Path, cache_root: Path) -> None:
        """Turning the cache on for the first time needs no dir prepared by hand."""
        assert not cache_root.exists()

        rom_cache.startup()
        booted = _cache(_write(roms / "game.iso"))

        assert rom_cache.entry_of(booted) is not None

    def test_a_cache_dir_that_is_a_file_boots_the_library(self, roms: Path, cache_root: Path) -> None:
        """A mistyped mount that left a file where the dir should be costs the cache, not the launch."""
        cache_root.write_bytes(b"")
        rom = _write(roms / "game.iso")

        rom_cache.startup()

        assert _cache(rom) == rom

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_a_read_only_cache_dir_boots_the_library(self, roms: Path, cache_root: Path) -> None:
        """A cache volume mounted read-only falls back to the library."""
        cache_root.mkdir()
        cache_root.chmod(0o555)
        try:
            rom = _write(roms / "game.iso")
            assert _cache(rom) == rom
        finally:
            cache_root.chmod(0o755)

    def test_a_file_replaced_by_a_folder_is_copied_fresh(self, roms: Path, cache_root: Path) -> None:
        """A library reorganised under the same name never boots the old layout."""
        rom = _write(roms / "Game")
        _cache(rom)
        rom.unlink()
        boot = _write(roms / "Game" / "game.iso", b"new")

        booted = rom_cache.boot_path(rom, boot, _Cacheable())

        assert booted.read_bytes() == b"new"
        assert rom_cache.logical(booted) == boot

    @pytest.mark.parametrize(
        "name",
        ["-rf Game.iso", "Pokémon ポケモン (Japan).iso", "x" * 200 + ".iso", "a b\tc.iso"],
    )
    def test_odd_names_get_a_safe_short_entry(self, roms: Path, cache_root: Path, name: str) -> None:
        """Any file name the library holds maps to a portable entry name and boots."""
        rom = _write(roms / name)

        booted = _cache(rom)
        entry = rom_cache.entry_of(booted)

        assert entry is not None
        assert len(entry.name) <= 53
        assert entry.name.isascii()
        assert booted.name == name

    def test_an_empty_rom_is_cached(self, roms: Path, cache_root: Path) -> None:
        """A zero-byte file is a degenerate ROM, not a reason to raise."""
        rom = _write(roms / "empty.bin", b"")

        assert _cache(rom).read_bytes() == b""

    def test_size_eviction_survives_a_manifest_without_a_size(
        self, roms: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry whose manifest lost its byte count is sized by walking it instead."""
        now = time.time()
        cached = [_cache(_write(roms / f"g{i}.iso", b"x" * 1000)) for i in range(2)]
        for i, c in enumerate(cached):
            _set_last_launched(c, now - (2 - i) * 60)
        old_entry = rom_cache.entry_of(cached[0])
        assert old_entry is not None
        manifest = json.loads((old_entry / rom_cache.MANIFEST).read_text())
        manifest["bytes"] = "lots"
        (old_entry / rom_cache.MANIFEST).write_text(json.dumps(manifest))
        monkeypatch.setattr(settings, "ROM_CACHE_MAX_GB", 1500 / rom_cache._GB)
        monkeypatch.setattr(rom_cache, "_active", None)

        rom_cache.evict()

        assert [c.exists() for c in cached] == [False, True]


class TestLogicalPath:
    """Mapping a cached path back to the ROM's identity under ROM_ROOT."""

    def test_a_cached_path_maps_back_to_the_library(self, roms: Path) -> None:
        """logical() undoes boot_path, file for file."""
        rom = _write(roms / "library" / "psx" / "game.chd")

        assert rom_cache.logical(_cache(rom)) == rom

    def test_a_library_path_is_returned_unchanged(self, roms: Path) -> None:
        """A path outside the cache passes straight through."""
        rom = _write(roms / "psx" / "game.chd")

        assert rom_cache.logical(rom) == rom

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize("where", ["library", "above"])
    def test_a_refused_cache_dir_never_remaps_a_library_path(
        self, roms: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, where: str
    ) -> None:
        """A cache dir at or above ROM_ROOT holds no entries, so a library path is never read as a copy."""
        rom = _write(roms / "library" / "psx" / "game.chd")
        monkeypatch.setattr(settings, "ROM_CACHE_ENABLED", enabled)
        monkeypatch.setattr(settings, "ROM_CACHE_DIR", roms if where == "library" else roms.parent.parent)

        assert rom_cache.logical(rom) == rom
        assert rom_cache.entry_of(rom) is None


class TestResumeIdentity:
    """Resume states saved from one boot location resume from the other."""

    @pytest.fixture
    def duckstation_states(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, roms: Path) -> Path:
        """Point DuckStation's savestate dir and ROM_ROOT into the test's temp tree."""
        sstate = tmp_path / "duckstation" / "savestates"
        sstate.mkdir(parents=True)
        monkeypatch.setattr(duckstation, "SSTATE_DIR", sstate)
        monkeypatch.setattr(duckstation, "ROM_ROOT", roms)
        return sstate

    @pytest.fixture
    def flycast_states(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, roms: Path) -> Path:
        """Point Flycast's data dir and ROM_ROOT into the test's temp tree."""
        data = tmp_path / "flycast"
        data.mkdir()
        monkeypatch.setattr(flycast, "DATA_DIR", data)
        monkeypatch.setattr(flycast, "ROM_ROOT", roms)
        return data

    def test_duckstation_markers_name_the_library_path(self, roms: Path, duckstation_states: Path) -> None:
        """A state saved while booted from the cache is marked with the ROM_ROOT path."""
        rom = _write(roms / "psx" / "game.chd")
        cached = _cache(rom)
        state = _write(duckstation_states / "SLUS-00001_resume.sav")

        duckstation._write_owner_marker(state, cached)

        assert duckstation._state_owner(state) == str(rom)

    @pytest.mark.parametrize("saved_from_cache", [True, False])
    def test_duckstation_resume_round_trips(
        self, roms: Path, duckstation_states: Path, saved_from_cache: bool
    ) -> None:
        """A resume state saved from either location resumes from the other."""
        rom = _write(roms / "psx" / "game.chd")
        cached = _cache(rom)
        saved_on, booted_on = (cached, rom) if saved_from_cache else (rom, cached)
        state = _write(duckstation_states / "SLUS-00001_resume.sav")
        _write(duckstation_states / "SLUS-00002_resume.sav")
        duckstation._write_owner_marker(state, saved_on)
        other_state = duckstation_states / "SLUS-00002_resume.sav"
        duckstation._write_owner_marker(other_state, roms / "psx" / "other.chd")

        assert duckstation._resume_state_for(booted_on) == state

    @pytest.mark.parametrize("saved_from_cache", [True, False])
    def test_flycast_resume_round_trips(
        self, roms: Path, flycast_states: Path, saved_from_cache: bool
    ) -> None:
        """A Flycast state marked from either location belongs to the ROM booted from the other."""
        rom = _write(roms / "dc" / "game.gdi")
        folder_rom = _write(roms / "dc" / "Game" / "game.gdi")
        cached = rom_cache.boot_path(folder_rom.parent, folder_rom, _Cacheable())
        assert cached != folder_rom
        saved_on, booted_on = (cached, folder_rom) if saved_from_cache else (folder_rom, cached)
        state = _write(flycast_states / "game_resume.state")
        flycast._write_owner_marker(state, saved_on)

        assert flycast._state_belongs_to(state, booted_on)
        assert not flycast._state_belongs_to(state, rom)


class TestDiscSwap:
    """A disc swap names a library disc, and must find it in a playlist booted from the cache."""

    def test_a_cached_playlist_lists_the_library_disc(self, roms: Path) -> None:
        """RetroArch maps the cached .m3u's entries back to ROM_ROOT before matching the swap target."""
        game = roms / "psx" / "Game"
        _write(game / "Game (Disc 1).chd", b"1")
        disc2 = _write(game / "Game (Disc 2).chd", b"2")
        playlist = _write(game / "Game.m3u", b"Game (Disc 1).chd\nGame (Disc 2).chd\n")
        cached = rom_cache.boot_path(game, playlist, _Cacheable())
        assert cached != playlist

        assert retroarch._m3u_index_for_path(cached, disc2) == 1
        assert retroarch._m3u_index_for_path(cached, roms / "psx" / "Other.chd") is None
