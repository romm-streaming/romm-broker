"""Tests for the shared, opt-in archive/pkg extraction cache."""
from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Optional

import pytest

from webstation_broker.emulators import extraction_cache
from webstation_broker.emulators.base import Emulator
from webstation_broker.emulators.extraction_cache import ExtractionCache


def _make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _touch(path: Path, mtime: Optional[float] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 5)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class _FakeEmulator(Emulator):
    """Minimal Emulator stand-in exposing only extraction_phase."""


def _find_eboot(root: Path) -> Optional[Path]:
    for candidate in root.rglob("EBOOT.BIN"):
        return candidate
    return None


def _cache(tmp_path: Path, *, enabled: bool = True, max_gb: float = 8 / 1024**3,
           on_evict: Optional[Callable[[Path], None]] = None) -> ExtractionCache:
    cache_dir = tmp_path / "cache"
    return ExtractionCache(
        name="test", cache_dir=lambda: cache_dir, enabled=lambda: enabled,
        max_gb=lambda: max_gb, find_boot_target=_find_eboot, on_evict=on_evict,
    )


def test_root_returns_the_configured_cache_dir(tmp_path: Path) -> None:
    """root() reflects the cache_dir callable, read live rather than snapshotted."""
    cache_dir = tmp_path / "cache"
    cache = ExtractionCache(
        name="test",
        cache_dir=lambda: cache_dir,
        enabled=lambda: True,
        max_gb=lambda: 10.0,
        find_boot_target=_find_eboot,
    )
    assert cache.root() == cache_dir


def test_root_reflects_a_live_change_to_the_cache_dir_callable(tmp_path: Path) -> None:
    """A constructor callable is re-read on every call, not captured once at construction."""
    current = {"dir": tmp_path / "first"}
    cache = ExtractionCache(
        name="test",
        cache_dir=lambda: current["dir"],
        enabled=lambda: True,
        max_gb=lambda: 10.0,
        find_boot_target=_find_eboot,
    )
    assert cache.root() == tmp_path / "first"
    current["dir"] = tmp_path / "second"
    assert cache.root() == tmp_path / "second"


def test_cache_key_combines_stem_and_a_content_fingerprint(tmp_path: Path) -> None:
    """The key is the stem plus a short hash of the resolved path, size, and mtime."""
    rom = tmp_path / "Game.zip"
    rom.write_bytes(b"data")
    key = extraction_cache._cache_key(rom)
    assert key.startswith("Game-")
    assert len(key) == len("Game-") + 12


def test_cache_key_differs_for_files_sharing_a_stem_but_not_an_extension(tmp_path: Path) -> None:
    """Two archives that share a stem but differ in extension never collide."""
    a = tmp_path / "Game.zip"
    b = tmp_path / "Game.7z"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    assert extraction_cache._cache_key(a) != extraction_cache._cache_key(b)


def test_cache_key_changes_when_a_same_named_file_is_replaced(tmp_path: Path) -> None:
    """A same-named re-upload with different content never reuses the old cache entry."""
    rom = tmp_path / "Game.zip"
    rom.write_bytes(b"original")
    first = extraction_cache._cache_key(rom)
    rom.write_bytes(b"replaced, different size")
    second = extraction_cache._cache_key(rom)
    assert first != second


def test_cache_key_differs_for_same_named_files_in_different_folders(tmp_path: Path) -> None:
    """Two files sharing a filename, size, and second-granularity mtime still key apart.

    A library holds one title per folder, so same-named dumps sitting side
    by side is ordinary; keying off the bare filename would hand them a
    single cache dir and boot whichever extraction landed there first.
    """
    first = tmp_path / "USA" / "Game.pkg"
    second = tmp_path / "EUR" / "Game.pkg"
    _touch(first, mtime=1000)
    _touch(second, mtime=1000)

    assert extraction_cache._cache_key(first) != extraction_cache._cache_key(second)


def test_cache_key_changes_for_a_same_second_replacement_of_the_same_size(
    tmp_path: Path,
) -> None:
    """A rewrite within the same second still changes the key.

    A library sync replaces a dump in place, so the old and new file can
    share a size and a whole-second mtime; a key truncated to seconds would
    keep serving the previous extraction as if it were the new ROM.
    """
    rom = tmp_path / "Game.pkg"
    rom.write_bytes(b"original")
    os.utime(rom, ns=(1_000_000_000_000, 1_000_000_000_000))
    original_key = extraction_cache._cache_key(rom)

    rom.write_bytes(b"replaced")
    os.utime(rom, ns=(1_000_000_000_000 + 250_000_000, 1_000_000_000_000 + 250_000_000))

    assert int(rom.stat().st_mtime) == 1000
    assert extraction_cache._cache_key(rom) != original_key


def test_cache_key_raises_when_the_file_cannot_be_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable file raises and logs rather than falling back to the collision-prone bare stem."""
    missing = tmp_path / "Missing.zip"
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="could not read Missing.zip to key its extraction"):
            extraction_cache._cache_key(missing)

    assert "could not read" in caplog.text


def test_dir_size_sums_files_and_skips_the_marker(tmp_path: Path) -> None:
    """Dir size sums files and skips the last-accessed marker."""
    game_dir = tmp_path / "Game"
    _touch(game_dir / "eboot.bin")
    _touch(game_dir / "sub" / "data.bin")
    _touch(game_dir / extraction_cache._LAST_ACCESSED_MARKER)
    assert extraction_cache._dir_size(game_dir) == 10


def test_touch_last_accessed_writes_a_marker_file(tmp_path: Path) -> None:
    """Touch last accessed writes a marker file game_dir did not have before."""
    game_dir = tmp_path / "Game"
    _touch(game_dir / "eboot.bin")
    extraction_cache._touch_last_accessed(game_dir)
    assert (game_dir / extraction_cache._LAST_ACCESSED_MARKER).exists()


def test_cache_size_bytes_sums_across_every_game_dir(tmp_path: Path) -> None:
    """Cache size bytes sums across every game dir under root()."""
    cache_dir = tmp_path / "cache"
    _touch(cache_dir / "GameA" / "eboot.bin")
    _touch(cache_dir / "GameB" / "eboot.bin")
    cache = ExtractionCache(
        name="test", cache_dir=lambda: cache_dir, enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=_find_eboot,
    )
    assert cache._cache_size_bytes() == 10


def test_cache_size_bytes_is_zero_without_a_cache_dir(tmp_path: Path) -> None:
    """Cache size bytes is zero when the configured cache dir does not exist yet."""
    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "never-created", enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=_find_eboot,
    )
    assert cache._cache_size_bytes() == 0


def test_evict_lru_is_a_noop_when_disabled(tmp_path: Path) -> None:
    """Evict LRU is a no-op when the cache is disabled."""
    cache = _cache(tmp_path, enabled=False)
    game_dir = cache.root() / "GameA"
    _touch(game_dir / "eboot.bin")
    cache._evict_lru(10**9, "SomethingElse")
    assert game_dir.exists()


def test_evict_lru_removes_the_least_recently_used_entry_first(tmp_path: Path) -> None:
    """Evict LRU removes the least recently used entry first."""
    cache = _cache(tmp_path)
    old = cache.root() / "Old"
    new = cache.root() / "New"
    _touch(old / "eboot.bin")
    _touch(new / "eboot.bin")
    _touch(old / extraction_cache._LAST_ACCESSED_MARKER, mtime=1000)
    _touch(new / extraction_cache._LAST_ACCESSED_MARKER, mtime=2000)
    cache._evict_lru(2, "Incoming")
    assert not old.exists()
    assert new.exists()


def test_evict_lru_never_removes_the_entry_being_extracted(tmp_path: Path) -> None:
    """Evict LRU never removes the entry currently being (re-)extracted."""
    cache = _cache(tmp_path, max_gb=1 / 1024**3)
    keep = cache.root() / "Incoming"
    _touch(keep / "eboot.bin")
    _touch(keep / extraction_cache._LAST_ACCESSED_MARKER, mtime=1)
    cache._evict_lru(50, "Incoming")
    assert keep.exists()


def test_evict_lru_calls_on_evict_once_per_evicted_dir(tmp_path: Path) -> None:
    """on_evict fires once per successfully evicted dir, after the rmtree."""
    evicted: list[Path] = []
    cache = _cache(tmp_path, max_gb=6 / 1024**3, on_evict=evicted.append)
    old = cache.root() / "Old"
    _touch(old / "eboot.bin")
    _touch(old / extraction_cache._LAST_ACCESSED_MARKER, mtime=1)
    cache._evict_lru(2, "Incoming")
    assert evicted == [old]
    assert not old.exists()


def test_require_room_refuses_when_the_cache_cap_would_be_exceeded(tmp_path: Path) -> None:
    """require_room refuses an extraction that would push the cache past max_gb."""
    cache = _cache(tmp_path, max_gb=1 / 1024**3)
    with pytest.raises(RuntimeError, match="max_gb"):
        cache._require_room(2, 2, "Game.zip")


def test_require_room_refuses_when_free_disk_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """require_room refuses an extraction whose peak would not fit on the free disk."""
    cache = _cache(tmp_path, max_gb=100.0)
    cache.root().mkdir(parents=True)
    monkeypatch.setattr(
        extraction_cache.shutil, "disk_usage",
        lambda path: type("U", (), {"free": 1})(),
    )
    with pytest.raises(RuntimeError, match="free on"):
        cache._require_room(10**9, 1, "Game.zip")


def test_require_room_charges_the_cap_on_kept_and_the_disk_on_peak(tmp_path: Path) -> None:
    """A budget where peak and kept differ charges each guard its own figure."""
    cache = _cache(tmp_path, max_gb=100.0)
    cache.root().mkdir(parents=True)
    # kept (1 byte) fits the cap; peak (huge) must still be checked against
    # free disk space rather than being ignored because kept passed.
    free = shutil.disk_usage(str(cache.root())).free
    cache._require_room(1, 1, "Game.zip")  # both tiny: passes without raising
    with pytest.raises(RuntimeError, match="needs about"):
        cache._require_room(free + 10**12, 1, "Game.zip")


def test_locked_serializes_a_second_call_on_the_same_instance(tmp_path: Path) -> None:
    """A second _locked() call on the same instance blocks until the first releases."""
    import threading as _threading
    cache = _cache(tmp_path)
    entered = _threading.Event()
    release = _threading.Event()
    order: list[str] = []

    def first() -> None:
        with cache._locked("first"):
            order.append("first-enter")
            entered.set()
            release.wait(timeout=5)
        order.append("first-exit")

    t = _threading.Thread(target=first)
    t.start()
    assert entered.wait(timeout=5)
    with cache._locked("second"):
        order.append("second-enter")
    release.set()
    t.join(timeout=5)
    assert order == ["first-enter", "first-exit", "second-enter"]


def test_locked_on_a_different_instance_does_not_block(tmp_path: Path) -> None:
    """A second, independent ExtractionCache instance never waits on the first one's lock."""
    import threading as _threading
    first_cache = _cache(tmp_path / "one")
    second_cache = _cache(tmp_path / "two")
    entered = _threading.Event()
    release = _threading.Event()

    def hold_first() -> None:
        with first_cache._locked("first"):
            entered.set()
            release.wait(timeout=5)

    t = _threading.Thread(target=hold_first)
    t.start()
    assert entered.wait(timeout=5)
    with second_cache._locked("second"):
        pass  # must not block
    release.set()
    t.join(timeout=5)


def test_locked_with_a_bounded_wait_raises_on_timeout(tmp_path: Path) -> None:
    """A bounded lock_wait raises rather than parking the caller forever."""
    import threading as _threading
    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=_find_eboot, lock_wait=lambda: 0.05,
    )
    entered = _threading.Event()
    release = _threading.Event()

    def hold() -> None:
        with cache._locked("first"):
            entered.set()
            release.wait(timeout=5)

    t = _threading.Thread(target=hold)
    t.start()
    assert entered.wait(timeout=5)
    with pytest.raises(RuntimeError, match="still running"):
        with cache._locked("second"):
            pass
    release.set()
    t.join(timeout=5)


def test_locked_with_no_wait_configured_blocks_until_available(tmp_path: Path) -> None:
    """lock_wait=None blocks with no timeout, matching rpcs3's original bare `with lock:`."""
    import threading as _threading
    import time
    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=_find_eboot, lock_wait=None,
    )
    entered = _threading.Event()
    order: list[str] = []

    def hold() -> None:
        with cache._locked("first"):
            entered.set()
            time.sleep(0.2)
            order.append("first-exit")

    t = _threading.Thread(target=hold)
    t.start()
    assert entered.wait(timeout=5)
    with cache._locked("second"):
        order.append("second-enter")
    t.join(timeout=5)
    assert order == ["first-exit", "second-enter"]


def test_locked_releases_when_the_block_raises(tmp_path: Path) -> None:
    """A failed block inside _locked does not leave the lock held forever."""
    cache = _cache(tmp_path)
    with pytest.raises(ValueError):
        with cache._locked("boom"):
            raise ValueError("extraction failed")
    assert cache._lock.acquire(timeout=0.1)
    cache._lock.release()


def test_clear_scratch_removes_every_entry_under_the_scratch_dir(tmp_path: Path) -> None:
    """_clear_scratch removes everything under .scratch, leaving real entries alone."""
    cache = _cache(tmp_path)
    _touch(cache.root() / extraction_cache._SCRATCH_DIR_NAME / "orphaned" / "extracted" / "eboot.bin")
    _touch(cache.root() / "RealEntry" / "eboot.bin")
    cache._clear_scratch()
    assert not (cache.root() / extraction_cache._SCRATCH_DIR_NAME / "orphaned").exists()
    assert (cache.root() / "RealEntry").exists()


def test_sweep_stale_extractions_is_a_noop_without_a_cache_dir(tmp_path: Path) -> None:
    """sweep_stale_extractions does nothing when the cache dir was never created."""
    cache = _cache(tmp_path)
    cache.sweep_stale_extractions()  # must not raise


def test_sweep_stale_extractions_removes_orphaned_scratch_dirs(tmp_path: Path) -> None:
    """sweep_stale_extractions removes orphaned scratch but keeps real cache entries."""
    cache = _cache(tmp_path)
    _touch(cache.root() / extraction_cache._SCRATCH_DIR_NAME / "orphaned" / "extracted" / "eboot.bin")
    _touch(cache.root() / "RealEntry" / "eboot.bin")
    cache.sweep_stale_extractions()
    assert not (cache.root() / extraction_cache._SCRATCH_DIR_NAME / "orphaned").exists()
    assert (cache.root() / "RealEntry").exists()


def test_reject_unsafe_members_rejects_a_traversal_path(tmp_path: Path) -> None:
    """A `../` member path is rejected before anything is written."""
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(RuntimeError, match="escapes"):
        extraction_cache._reject_unsafe_members(dest, ["../outside.txt"])


def test_reject_unsafe_members_allows_normal_paths(tmp_path: Path) -> None:
    """Ordinary relative member paths are accepted."""
    dest = tmp_path / "dest"
    dest.mkdir()
    extraction_cache._reject_unsafe_members(dest, ["a/b/c.txt", "top.txt"])


def test_safe_extract_zip_extracts_normal_members(tmp_path: Path) -> None:
    """A normal zip extracts its members under dest."""
    archive = _make_zip(tmp_path / "Game.zip", {"PS_GAME/EBOOT.BIN": b"boot"})
    dest = tmp_path / "Game"
    dest.mkdir()
    with zipfile.ZipFile(archive) as zf:
        extraction_cache._safe_extract_zip(zf, dest)
    assert (dest / "PS_GAME" / "EBOOT.BIN").read_bytes() == b"boot"


def test_safe_extract_zip_rejects_a_member_that_escapes_the_dest(tmp_path: Path) -> None:
    """A zip-slip member is rejected instead of extracted."""
    archive = _make_zip(tmp_path / "Evil.zip", {"../../etc/passwd": b"pwned"})
    dest = tmp_path / "Evil"
    dest.mkdir()
    with zipfile.ZipFile(archive) as zf:
        with pytest.raises(RuntimeError, match="escapes"):
            extraction_cache._safe_extract_zip(zf, dest)


def test_reject_escaped_tree_allows_a_normal_extraction(tmp_path: Path) -> None:
    """A normal extraction tree with no symlinks passes."""
    dest = tmp_path / "dest"
    (dest / "sub").mkdir(parents=True)
    (dest / "sub" / "file.txt").write_bytes(b"x")
    extraction_cache._reject_escaped_tree(dest)


def test_reject_escaped_tree_rejects_a_symlink_that_resolves_outside_dest(tmp_path: Path) -> None:
    """A symlink that resolves outside dest is caught by the post-extraction walk."""
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="escapes cache dir"):
        extraction_cache._reject_escaped_tree(dest)


def test_run_extractor_raises_on_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonzero exit from the extractor binary raises with its exit code."""
    monkeypatch.setattr(
        extraction_cache.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 2, "stderr": "boom"})(),
    )
    with pytest.raises(RuntimeError, match="exited 2"):
        extraction_cache._run_extractor(["7z", "x"], "7z (Game.7z)", 30.0)


def test_run_extractor_raises_when_the_binary_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing extractor binary raises rather than propagating an OSError."""
    from typing import NoReturn
    def raise_oserror(*a: object, **k: object) -> NoReturn:
        raise OSError("not found")
    monkeypatch.setattr(extraction_cache.subprocess, "run", raise_oserror)
    with pytest.raises(RuntimeError, match="failed to run"):
        extraction_cache._run_extractor(["unrar", "x"], "unrar (Game.rar)", 30.0)


def test_default_stage_extracts_a_zip_directly_into_staged(tmp_path: Path) -> None:
    """The default stage extracts rom directly into staged, ignoring scratch/emulator/kept_bytes."""
    cache = _cache(tmp_path)
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})
    staged = tmp_path / "staged"
    staged.mkdir()
    cache._default_stage(archive, staged, tmp_path / "scratch", _FakeEmulator(), 0)
    assert (staged / "EBOOT.BIN").read_bytes() == b"boot"


def test_default_budget_reads_the_zip_members_own_sizes(tmp_path: Path) -> None:
    """The default budget sums a zip's own member sizes rather than guessing."""
    cache = _cache(tmp_path)
    archive = _make_zip(tmp_path / "Game.zip", {"a.bin": b"1234", "b.bin": b"56"})
    peak, kept = cache._default_budget(archive)
    assert peak == kept == 6


def test_default_budget_falls_back_to_the_expansion_factor_when_unreadable(tmp_path: Path) -> None:
    """An archive with no readable listing falls back to compressed_size * expansion_factor."""
    cache_dir = tmp_path / "cache"
    cache = ExtractionCache(
        name="test", cache_dir=lambda: cache_dir, enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=_find_eboot, expansion_factor=lambda: 3.0,
    )
    archive = tmp_path / "Corrupt.zip"
    archive.write_bytes(b"not actually a zip")
    peak, kept = cache._default_budget(archive)
    assert peak == kept == int(len(b"not actually a zip") * 3.0)


def test_extract_reuses_an_existing_bootable_extraction(tmp_path: Path) -> None:
    """A cache hit returns the existing boot target without re-extracting."""
    cache = _cache(tmp_path)
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})
    key = extraction_cache._cache_key(archive)
    game_dir = cache.root() / key
    _touch(game_dir / "EBOOT.BIN")
    before = (game_dir / "EBOOT.BIN").read_bytes()
    boot = cache.extract(archive, _FakeEmulator())
    assert boot == game_dir / "EBOOT.BIN"
    assert (game_dir / "EBOOT.BIN").read_bytes() == before
    assert (game_dir / extraction_cache._LAST_ACCESSED_MARKER).exists()


def test_extract_extracts_and_returns_the_boot_target_on_a_miss(tmp_path: Path) -> None:
    """A cache miss extracts the archive and returns its boot target."""
    cache = _cache(tmp_path, max_gb=10.0)
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})
    emulator = _FakeEmulator()
    boot = cache.extract(archive, emulator)
    assert boot.read_bytes() == b"boot"
    assert emulator.extraction_phase is None


def test_extract_sets_and_clears_the_extraction_phase(tmp_path: Path) -> None:
    """extraction_phase is set to phase_name(rom) during extraction and cleared after."""
    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=_find_eboot,
        phase_name=lambda rom: "extracting_archive",
    )
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})
    seen: list[Optional[str]] = []

    def spying_stage(rom: Path, staged: Path, scratch: Path, emulator: Emulator, kept: int) -> None:
        seen.append(emulator.extraction_phase)
        extraction_cache._extract_archive(rom, staged, 30.0)

    cache._stage = spying_stage
    emulator = _FakeEmulator()
    cache.extract(archive, emulator)
    assert seen == ["extracting_archive"]
    assert emulator.extraction_phase is None


def test_extract_clears_the_phase_when_extraction_fails(tmp_path: Path) -> None:
    """extraction_phase is cleared even when the stage callback raises."""
    cache = _cache(tmp_path, max_gb=10.0)
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})

    def failing_stage(rom: Path, staged: Path, scratch: Path, emulator: Emulator, kept: int) -> None:
        raise RuntimeError("boom")

    cache._stage = failing_stage
    emulator = _FakeEmulator()
    with pytest.raises(RuntimeError, match="boom"):
        cache.extract(archive, emulator)
    assert emulator.extraction_phase is None


def test_extract_cleans_up_and_raises_when_nothing_bootable_was_extracted(tmp_path: Path) -> None:
    """A stage that leaves no boot target raises missing_target_error and cleans up."""
    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=_find_eboot,
        missing_target_error="held no EBOOT.BIN",
    )
    archive = _make_zip(tmp_path / "Game.zip", {"readme.txt": b"nope"})

    def empty_stage(rom: Path, staged: Path, scratch: Path, emulator: Emulator, kept: int) -> None:
        extraction_cache._extract_archive(rom, staged, 30.0)

    cache._stage = empty_stage
    with pytest.raises(RuntimeError, match="held no EBOOT.BIN"):
        cache.extract(archive, _FakeEmulator())
    key = extraction_cache._cache_key(archive)
    assert not (cache.root() / key).exists()


def test_extract_does_not_reuse_a_stale_entry_with_no_boot_target(tmp_path: Path) -> None:
    """A pre-existing cache dir with no boot target is discarded and re-extracted."""
    cache = _cache(tmp_path, max_gb=10.0)
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})
    key = extraction_cache._cache_key(archive)
    stale = cache.root() / key
    _touch(stale / "readme.txt")
    boot = cache.extract(archive, _FakeEmulator())
    assert boot.read_bytes() == b"boot"


def test_extract_re_checks_the_boot_target_after_the_rename(tmp_path: Path) -> None:
    """find_boot_target is called again on game_dir after rename, not just on staged."""
    calls: list[Path] = []
    real_find = _find_eboot

    def spying_find(root: Path) -> Optional[Path]:
        calls.append(root)
        return real_find(root)

    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=spying_find,
    )
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})
    cache.extract(archive, _FakeEmulator())
    # First call is against the staged tree (pre-rename), second against game_dir (post-rename).
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_extract_evicts_before_refusing_room(tmp_path: Path) -> None:
    """Eviction runs before the space guard, so a full-but-evictable cache still accepts a new entry."""
    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 12 / 1024**3, find_boot_target=_find_eboot,
    )
    old = cache.root() / "Old"
    _touch(old / "eboot.bin", mtime=1)
    _touch(old / extraction_cache._LAST_ACCESSED_MARKER, mtime=1)
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"1234567890"})
    boot = cache.extract(archive, _FakeEmulator())
    assert boot.read_bytes() == b"1234567890"
    assert not old.exists()


def test_extract_refuses_and_raises_when_nothing_fits(tmp_path: Path) -> None:
    """require_room's failure propagates out of extract() as a RuntimeError."""
    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 1 / 1024**3, find_boot_target=_find_eboot,
    )
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"x" * 5000})
    with pytest.raises(RuntimeError, match="max_gb"):
        cache.extract(archive, _FakeEmulator())


def test_extract_still_raises_if_stage_succeeds_but_leaves_no_boot_target(tmp_path: Path) -> None:
    """Stage returning normally does NOT bypass the generic post-stage boot check.

    Even if stage() completes without raising, if find_boot_target(staged)
    finds nothing, extract() raises missing_target_error — stage's internal
    bookkeeping does not override the generic validation.
    """
    cache = _cache(tmp_path, max_gb=10.0)
    archive = _make_zip(tmp_path / "Game.zip", {"readme.txt": b"no boot here"})

    def stage_that_appears_ok(rom: Path, staged: Path, scratch: Path, emulator: Emulator, kept: int) -> None:
        # Extract the archive (readme.txt), but no EBOOT.BIN will be found
        extraction_cache._extract_archive(rom, staged, 30.0)
        # Stage could set internal flags saying "I found boot!" but that's ignored
        # The post-stage check still calls find_boot_target(staged) and finds nothing

    cache._stage = stage_that_appears_ok
    with pytest.raises(RuntimeError, match="held no bootable"):
        cache.extract(archive, _FakeEmulator())


def test_extract_releases_lock_on_unbounded_wait_even_when_stage_raises(tmp_path: Path) -> None:
    """lock_wait=None (unbounded blocking) still releases the lock after a stage exception."""
    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=_find_eboot, lock_wait=None,
    )
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})

    def failing_stage(rom: Path, staged: Path, scratch: Path, emulator: Emulator, kept: int) -> None:
        raise RuntimeError("stage explosion")

    cache._stage = failing_stage
    emulator = _FakeEmulator()
    with pytest.raises(RuntimeError, match="stage explosion"):
        cache.extract(archive, emulator)
    # Lock should be released (not stuck held) even with unbounded wait
    assert cache._lock.acquire(timeout=0.1)
    cache._lock.release()


def test_extract_propagates_uncaught_find_boot_target_exceptions(tmp_path: Path) -> None:
    """find_boot_target raising an exception is not caught; it propagates out."""
    def raising_find_boot(root: Path) -> Optional[Path]:
        raise RuntimeError("boot lookup exploded")

    cache = ExtractionCache(
        name="test", cache_dir=lambda: tmp_path / "cache", enabled=lambda: True,
        max_gb=lambda: 10.0, find_boot_target=raising_find_boot,
    )
    archive = _make_zip(tmp_path / "Game.zip", {"EBOOT.BIN": b"boot"})
    with pytest.raises(RuntimeError, match="boot lookup exploded"):
        cache.extract(archive, _FakeEmulator())
