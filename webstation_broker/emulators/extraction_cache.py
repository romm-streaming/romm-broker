"""Shared, opt-in extraction cache for emulator modules that boot from an archive or package.

Every tunable a consumer supplies is a zero-argument callable, never a
snapshotted value: consumer modules read their own config from module-level
globals (e.g. `rpcs3.CACHE_DIR`) that existing tests monkeypatch at call
time, and a captured value here would make that monkeypatching a silent
no-op.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .base import Emulator

log = logging.getLogger(__name__)

_GB = 1024**3
"""Bytes per GB, the unit max_gb and the space guard are expressed in."""

_ARCHIVE_EXTS = (".7z", ".zip", ".rar")
"""Archive formats the class's own default safe-extract stage understands."""

_LAST_ACCESSED_MARKER = ".last_accessed"
"""Marker file inside a cache entry, touched on every hit, read to pick an LRU victim."""

_SCRATCH_DIR_NAME = ".scratch"
"""Subdirectory of a cache dir every staged extraction lives under until renamed into place."""


def _cache_key(rom: Path) -> str:
    """Cache dir name for rom: its stem plus a short hash of the file's identity.

    A bare stem collides two ROMs that share a name but differ in extension,
    and survives a same-named re-upload with different content, either of
    which would otherwise serve up whatever is sitting in the old cache dir
    as if it were the new ROM. The hash covers the resolved path, the size,
    and the nanosecond mtime: same-second rewrites are exactly how a library
    sync replaces a dump, so second granularity would let a replacement keep
    the old key.

    Args:
        rom: The archive or package being extracted.

    Returns:
        The cache directory name for this ROM.

    Raises:
        RuntimeError: If the file cannot be read. Falling back to the bare
            name here would hand back the collision-prone key this function
            exists to avoid, and the extraction that follows would fail on
            the same unreadable file anyway.
    """
    try:
        st = rom.stat()
        fingerprint = f"{rom.resolve()}:{st.st_size}:{st.st_mtime_ns}"
    except OSError as exc:
        log.error("extraction cache: could not read %s to key its extraction: %s", rom, exc)
        raise RuntimeError(f"could not read {rom.name} to key its extraction: {exc}") from exc
    digest = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    return f"{rom.stem}-{digest}"


def _dir_size(path: Path) -> int:
    """Sum all file sizes under path, skipping the last-accessed marker.

    The marker file is skipped so the LRU eviction logic doesn't count it
    toward the cache size, which would pollute the accounting with a file
    that exists only for bookkeeping.

    Args:
        path: The directory to measure.

    Returns:
        The total size in bytes of all files under path, excluding the marker.
    """
    total = 0
    for f in path.rglob("*"):
        if f.name == _LAST_ACCESSED_MARKER:
            continue
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError as exc:
            log.debug("extraction cache: skipping unreadable %s while sizing %s: %s", f, path, exc)
            continue
    return total


def _touch_last_accessed(game_dir: Path) -> None:
    """Write a marker file in game_dir with the current Unix timestamp.

    The marker is used by LRU eviction to identify which cache entries have
    been recently accessed; touching it on each hit provides the eviction
    logic with a mtime-based candidate list.

    Args:
        game_dir: The cache entry directory to mark as accessed now.
    """
    try:
        (game_dir / _LAST_ACCESSED_MARKER).write_text(str(time.time()))
    except OSError as exc:
        log.warning("extraction cache: could not update last-accessed marker for %s: %s", game_dir, exc)


def _run_extractor(cmd: list[str], what: str, timeout: float) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("extraction cache: %s failed to run: %s", what, exc)
        raise RuntimeError(f"{what} failed to run: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{what} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
"""Matches a control character in an archive member name."""
_7Z_SEPARATOR_RE = re.compile(r"^-{5,}\s*$")
"""Matches the dashed line `7z l -slt` puts between the archive header and its members."""
_7Z_PATH_PREFIX = "Path = "
"""Prefix of the `7z l -slt` line carrying one member's full path."""


def reject_unsafe_members(dest: Path, members: list[str]) -> None:
    """Reject any archive member whose path would land outside dest.

    A `../` (or absolute) member path can escape dest on extraction (Zip
    Slip); this is checked before anything is written. A name carrying a
    control character is refused as well: the .rar/.7z member lists are read
    back out of a line-based text listing, so a name holding a newline (or
    anything else that does not survive that round trip) cannot be checked as
    the path the archive really holds.

    Args:
        dest: The directory the extraction must stay under.
        members: Member paths as the archive names them.

    Raises:
        RuntimeError: On the first member that escapes dest or carries a
            control character.
    """
    dest_real = dest.resolve()
    for member in members:
        if _CONTROL_CHAR_RE.search(member):
            log.error("extraction cache: archive member name holds a control character: %r", member)
            raise RuntimeError(f"archive member name holds a control character: {member!r}")
        target = (dest / member).resolve()
        if target != dest_real and dest_real not in target.parents:
            raise RuntimeError(f"archive member escapes extraction dir: {member}")


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract `zf` into dest after rejecting any Zip Slip member."""
    reject_unsafe_members(dest, zf.namelist())
    zf.extractall(dest)


def _7z_listing_body(archive: Path, what: str, timeout: float) -> list[str]:
    """The member section of `7z l -slt`, the only mode giving a full untruncated path.

    Everything before the dashed separator line describes the archive itself,
    not its contents.

    Args:
        archive: The archive to list.
        what: What the listing is for, named in errors.
        timeout: Seconds `7z` gets before it is considered hung.

    Returns:
        The listing's lines after the separator.

    Raises:
        RuntimeError: When 7z fails or the listing carries no separator line.
    """
    listing = _run_extractor(["7z", "l", "-slt", str(archive)], f"{what} ({archive.name})", timeout)
    lines = listing.splitlines()
    for i, line in enumerate(lines):
        if _7Z_SEPARATOR_RE.match(line):
            return lines[i + 1 :]
    raise RuntimeError(f"7z listing of {archive.name} has no member section")


def list_members(archive: Path, timeout: float) -> list[str]:
    """List the member paths of a .zip, .rar or .7z archive.

    A .zip is read in process, a .rar through `unrar lb`, and anything else
    through `7z l -slt`, which also covers the other formats 7z can identify.
    An external listing that names no member is an error rather than an empty
    list: `reject_unsafe_members` checks exactly this list, so an empty parse
    (another tool build, a localized listing) would wave the whole archive
    through unchecked.

    Args:
        archive: The archive to list.
        timeout: Seconds `unrar` or `7z` gets before it is considered hung.

    Returns:
        One path per member, as the archive names it.

    Raises:
        RuntimeError: When the archive cannot be read or listed, or the
            external listing names no member.
    """
    ext = archive.suffix.lower()
    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive) as zf:
                return zf.namelist()
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError(f"could not list zip {archive.name}: {exc}") from exc
    if ext == ".rar":
        listing = _run_extractor(["unrar", "lb", "-y", str(archive)], f"unrar list ({archive.name})", timeout)
        members = [line for line in listing.splitlines() if line.strip()]
        tool = "unrar"
    else:
        body = _7z_listing_body(archive, "7z list", timeout)
        members = [line[len(_7Z_PATH_PREFIX) :] for line in body if line.startswith(_7Z_PATH_PREFIX)]
        tool = "7z"
    if not members:
        raise RuntimeError(f"{tool} listed no members in {archive.name}")
    return members


def _reject_escaped_tree(dest: Path) -> None:
    """Post-extraction safety net: any real symlink or entry resolving outside dest is fatal.

    unrar/7z extraction is trusted to confine writes under dest, but the
    pre-extraction member-name check parses each tool's own text listing,
    and a name holding a raw control character can render differently there
    than in the archive's real central directory. This walks the real
    result instead of trusting the listing as a proxy for it.
    """
    dest_real = dest.resolve()
    for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
        base = Path(dirpath)
        for name in dirnames + filenames:
            p = base / name
            try:
                target_real = p.resolve()
            except OSError as exc:
                log.error(
                    "extraction cache: could not resolve extracted member %s under %s: %s",
                    p, dest, exc
                )
                raise RuntimeError(f"could not resolve extracted member {p}: {exc}") from exc
            if target_real != dest_real and dest_real not in target_real.parents:
                raise RuntimeError(f"extracted member escapes cache dir: {p}")


def _extract_archive(archive: Path, dest: Path, timeout: float) -> None:
    ext = archive.suffix.lower()
    log.info("extraction cache: extracting %s (%s)", archive.name, ext)
    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive) as zf:
                _safe_extract_zip(zf, dest)
        except (zipfile.BadZipFile, OSError) as exc:
            log.error("extraction cache: zip extraction of %s failed: %s", archive.name, exc)
            raise RuntimeError(f"zip extraction of {archive.name} failed: {exc}") from exc
    else:
        reject_unsafe_members(dest, list_members(archive, timeout))
        if ext == ".rar":
            _run_extractor(["unrar", "x", "-y", str(archive), f"{dest}/"], f"unrar ({archive.name})", timeout)
        else:
            _run_extractor(["7z", "x", "-y", str(archive), f"-o{dest}"], f"7z ({archive.name})", timeout)
        _reject_escaped_tree(dest)


def _sum_listed_sizes(lines: Iterable[str], prefix: str) -> Optional[int]:
    """Total the integers on every `prefix` line of an extractor's listing."""
    total = 0
    found = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix) :].strip()
        if value.isdigit():
            total += int(value)
            found = True
    return total if found else None


def listed_size(archive: Path, timeout: float) -> Optional[int]:
    """Uncompressed total the archive's own member listing reports.

    Args:
        archive: The .zip, .rar or .7z to interrogate.
        timeout: Seconds `unrar` or `7z` gets before it is considered hung.

    Returns:
        The sum of the members' uncompressed sizes, or None when the listing
        could not be read or carried no sizes.
    """
    ext = archive.suffix.lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(archive) as zf:
                return sum(i.file_size for i in zf.infolist()) or None
        if ext == ".rar":
            listing = _run_extractor(
                ["unrar", "lt", "-y", str(archive)], f"unrar sizes ({archive.name})", timeout,
            )
            return _sum_listed_sizes(listing.splitlines(), "Size:")
        return _sum_listed_sizes(_7z_listing_body(archive, "7z sizes", timeout), "Size =")
    except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
        log.warning("extraction cache: could not read the member sizes of %s: %s", archive.name, exc)
        return None


class ExtractionCache:
    """A per-instance, opt-in archive/pkg extraction cache.

    One instance owns one cache directory tree and one lock; no two
    instances may ever own or nest the same directory (a convention this
    class does not itself enforce).
    """

    def __init__(
        self,
        name: str,
        cache_dir: Callable[[], Path],
        enabled: Callable[[], bool],
        max_gb: Callable[[], float],
        find_boot_target: Callable[[Path], Optional[Path]],
        *,
        budget: Optional[Callable[[Path], tuple[int, int]]] = None,
        stage: Optional[Callable[[Path, Path, Path, Emulator, int], None]] = None,
        phase_name: Callable[[Path], str] = lambda rom: "extracting_archive",
        on_evict: Optional[Callable[[Path], None]] = None,
        expansion_factor: Callable[[], float] = lambda: 4.0,
        extract_timeout: Callable[[], float] = lambda: 1800.0,
        lock_wait: Optional[Callable[[], float]] = lambda: 120.0,
        missing_target_error: str = "held no bootable target",
    ) -> None:
        """Initialize the extraction cache with callables for all configuration.

        All configuration parameters are stored as callables, never snapshotted,
        so consumer modules can monkeypatch their module-level config at test time
        and this instance will read the current values on each call.

        Args:
            name: The cache name, for logging and identification.
            cache_dir: Callable returning the cache root directory.
            enabled: Callable returning True if the cache is enabled.
            max_gb: Callable returning the maximum cache size in GB.
            find_boot_target: Callable taking a Path and returning the bootable
                target inside it, or None.
            budget: Optional callable for cache budget calculation.
            stage: Optional callable for custom extraction staging.
            phase_name: Callable returning the phase name for a ROM.
            on_evict: Optional callable run when evicting a cache entry.
            expansion_factor: Callable returning the expected extraction expansion factor.
            extract_timeout: Callable returning extraction timeout in seconds.
            lock_wait: Optional callable returning lock wait timeout in seconds.
            missing_target_error: Error message when no bootable target is found.
        """
        self._name = name
        self._cache_dir = cache_dir
        self._enabled = enabled
        self._max_gb = max_gb
        self._find_boot_target = find_boot_target
        self._budget = budget
        self._stage = stage
        self._phase_name = phase_name
        self._on_evict = on_evict
        self._expansion_factor = expansion_factor
        self._extract_timeout = extract_timeout
        self._lock_wait = lock_wait
        self._missing_target_error = missing_target_error
        self._lock = threading.Lock()

    def root(self) -> Path:
        """The configured cache directory, read live from the `cache_dir` callable."""
        return self._cache_dir()

    def _cache_size_bytes(self) -> int:
        """Sum the sizes of all cache entries, or zero if the cache dir doesn't exist yet.

        Returns:
            The total size in bytes of all files in all cache entries under root(),
                or 0 if root() is not a directory.
        """
        cache_dir = self._cache_dir()
        if not cache_dir.is_dir():
            return 0
        return sum(_dir_size(d) for d in cache_dir.iterdir() if d.is_dir())

    def _evict_lru(self, needed_bytes: int, keep: str) -> None:
        """Evict least-recently-used cache entries until `needed_bytes` fits within max_gb.

        Args:
            needed_bytes: Additional bytes that must fit under the cache cap.
            keep: The cache key currently being (re-)extracted, so a stale
                entry for it already removed by the caller is never chosen.
        """
        cache_dir = self._cache_dir()
        if not self._enabled() or not cache_dir.is_dir():
            return
        max_bytes = int(self._max_gb() * _GB)
        current = self._cache_size_bytes()
        while current + needed_bytes > max_bytes:
            candidates = []
            for game_dir in cache_dir.iterdir():
                if not game_dir.is_dir() or game_dir.name in (keep, _SCRATCH_DIR_NAME):
                    continue
                marker = game_dir / _LAST_ACCESSED_MARKER
                try:
                    mtime = marker.stat().st_mtime if marker.exists() else 0.0
                except OSError as exc:
                    log.debug(
                        "%s extraction cache: could not read last-accessed marker for %s: %s",
                        self._name, game_dir, exc,
                    )
                    mtime = 0.0
                candidates.append((mtime, game_dir))
            if not candidates:
                log.warning(
                    "%s extraction cache: nothing left to evict under the %.0f GB cap",
                    self._name, self._max_gb(),
                )
                return
            candidates.sort(key=lambda c: c[0])
            victim = candidates[0][1]
            victim_size = _dir_size(victim)
            log.info("%s extraction cache: evicting %s (least recently used)", self._name, victim.name)
            try:
                shutil.rmtree(victim)
            except OSError as exc:
                log.warning("%s extraction cache: could not evict %s: %s", self._name, victim, exc)
                return
            current -= victim_size
            if self._on_evict is not None:
                self._on_evict(victim)

    def _require_room(self, peak_bytes: int, kept_bytes: int, rom_name: str) -> None:
        """Refuse an extraction that cannot fit before any of it is written.

        Two different figures cover two different ceilings: the cache cap
        counts only what survives (`kept_bytes`), while the free-space guard
        counts what is on disk at the extraction's worst moment (`peak_bytes`),
        which can exceed what is kept when a consumer's staging needs scratch
        space alongside its final output.

        Args:
            peak_bytes: Bytes on disk at the height of the extraction.
            kept_bytes: Bytes the finished extraction leaves in the cache.
            rom_name: The ROM being extracted, named in the error.

        Raises:
            RuntimeError: If the cache cap or the filesystem cannot hold it.
        """
        max_bytes = int(self._max_gb() * _GB)
        current = self._cache_size_bytes()
        if current + kept_bytes > max_bytes:
            raise RuntimeError(
                f"{rom_name} would leave about {kept_bytes / _GB:.1f} GB cached, more than "
                f"max_gb ({self._max_gb():.0f} GB) allows with {current / _GB:.1f} GB already there"
            )
        cache_dir = self._cache_dir()
        try:
            free = shutil.disk_usage(cache_dir).free
        except OSError as exc:
            log.warning(
                "%s extraction cache: could not read free space on %s: %s", self._name, cache_dir, exc,
            )
            return
        if free < peak_bytes:
            raise RuntimeError(
                f"{rom_name} needs about {peak_bytes / _GB:.1f} GB to extract, but only "
                f"{free / _GB:.1f} GB is free on {cache_dir}"
            )

    @contextmanager
    def _locked(self, what: str) -> Iterator[None]:
        """Hold this instance's lock for the block.

        Blocks with no timeout when `lock_wait` is None; otherwise gives up
        and raises after `lock_wait()` seconds.

        Args:
            what: The operation waiting for the lock, named in the log and the error.

        Raises:
            RuntimeError: When a bounded `lock_wait` elapses before the lock is free.
        """
        timeout = -1.0 if self._lock_wait is None else self._lock_wait()
        if not self._lock.acquire(timeout=timeout):
            log.error(
                "%s extraction cache: %s gave up after waiting %.0fs for the cache lock",
                self._name, what, timeout,
            )
            raise RuntimeError(
                f"another {self._name} extraction is still running; {what} waited "
                f"{timeout:.0f}s for the extraction cache"
            )
        try:
            yield
        finally:
            self._lock.release()

    def _clear_scratch(self) -> None:
        """Remove every staged extraction under the scratch dir. Callers must hold `_locked`.

        The lock is what makes this safe: no extraction can be mid-flight
        while it is held, so anything still sitting here was orphaned by a
        process that died.
        """
        scratch_root = self._cache_dir() / _SCRATCH_DIR_NAME
        if not scratch_root.is_dir():
            return
        for entry in scratch_root.iterdir():
            log.warning("%s extraction cache: removing orphaned scratch dir %s", self._name, entry.name)
            shutil.rmtree(entry, ignore_errors=True)

    def sweep_stale_extractions(self) -> None:
        """Remove extraction scratch dirs orphaned by a crashed broker process.

        Call once at broker startup: the only other caller is an extraction,
        which a library of already-extracted (or never-archived) titles may
        never run again.
        """
        with self._locked("startup scratch sweep"):
            self._clear_scratch()

    def _default_stage(
        self, archive: Path, staged: Path, scratch: Path, emulator: Emulator, kept_bytes: int
    ) -> None:
        """The default `stage`: extract `archive` directly into `staged`."""
        _extract_archive(archive, staged, self._extract_timeout())

    def _default_budget(self, rom: Path) -> tuple[int, int]:
        """The default `budget`: the archive's own listed size, or a compressed-size fallback."""
        listed = listed_size(rom, self._extract_timeout())
        if listed is not None:
            return (listed, listed)
        try:
            compressed = rom.stat().st_size
        except OSError as exc:
            log.warning(
                "%s extraction cache: could not size %s for the space guard: %s",
                self._name, rom.name, exc,
            )
            return (0, 0)
        factor = self._expansion_factor()
        log.warning(
            "%s extraction cache: %s has no readable member listing, budgeting %.1fx its compressed size",
            self._name, rom.name, factor,
        )
        needed = int(compressed * factor)
        return (needed, needed)

    def extract(self, rom: Path, emulator: Emulator) -> Path:
        """Get `rom` booting from the cache, extracting it if not already cached.

        Reuses a prior extraction keyed by `_cache_key` when one already
        holds a bootable target. `rom` is staged under a scratch dir and only
        renamed to the persistent game_dir once a boot target is confirmed,
        so game_dir either does not exist or holds a complete extraction.

        Holds this instance's lock for the whole call: eviction, extraction,
        and the boot-target lookup all touch the same cache tree, so a
        second call racing in here must wait rather than potentially
        evicting the directory this one is mid-extracting into or about to
        boot from.

        Args:
            rom: The archive or package to extract.
            emulator: The launching emulator; `emulator.extraction_phase` is
                set while this runs, cleared again before returning or raising.

        Raises:
            RuntimeError: If `rom` cannot be read to key it, the extraction
                cannot fit in the cache or on the disk, `stage` fails, the
                extraction holds no boot target, or the finished extraction
                cannot be moved to its cache key.
            OSError: If the cache dir or a scratch dir cannot be created at all.
        """
        with self._locked(rom.name):
            key = _cache_key(rom)
            game_dir = self._cache_dir() / key

            if game_dir.is_dir():
                boot = self._find_boot_target(game_dir)
                if boot is not None:
                    log.info(
                        "%s extraction cache hit: %s (boot target: %s)", self._name, rom.name, boot.name,
                    )
                    _touch_last_accessed(game_dir)
                    return boot
                log.warning(
                    "%s extraction cache: %s has no boot target, re-extracting", self._name, rom.name,
                )
                shutil.rmtree(game_dir, ignore_errors=True)

            # Set before eviction, not after: eviction can rmtree tens of GB
            # under the lock, and a caller polling extraction_phase should
            # see that stall rather than an idle-looking None.
            emulator.extraction_phase = self._phase_name(rom)
            try:
                # Resolve budget/stage at call time for uniformity: both
                # default to instance methods only if None was passed to
                # __init__, so checking here keeps the resolution logic
                # together with its use rather than scattered across init.
                budget = self._budget if self._budget is not None else self._default_budget
                stage = self._stage if self._stage is not None else self._default_stage
                peak_bytes, kept_bytes = budget(rom)
                cache_dir = self._cache_dir()
                cache_dir.mkdir(parents=True, exist_ok=True)
                # Orphaned scratch is un-evictable but still counts toward
                # the cap, so reclaim it before sizing the cache rather than
                # letting it push real entries out.
                self._clear_scratch()
                self._evict_lru(kept_bytes, key)
                self._require_room(peak_bytes, kept_bytes, rom.name)

                scratch_root = cache_dir / _SCRATCH_DIR_NAME
                scratch_root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=f"{key}-", dir=str(scratch_root)) as scratch:
                    staged = Path(scratch) / "extracted"
                    staged.mkdir()
                    stage(rom, staged, Path(scratch), emulator, kept_bytes)
                    if self._find_boot_target(staged) is None:
                        raise RuntimeError(f"{rom.name} extracted but {self._missing_target_error}")
                    # rmtree above uses ignore_errors, so game_dir can still
                    # be sitting there non-empty and the rename then fails.
                    try:
                        staged.replace(game_dir)
                    except OSError as exc:
                        log.error(
                            "%s extraction cache: could not move the extraction of %s into %s: %s",
                            self._name, rom.name, game_dir, exc,
                        )
                        raise RuntimeError(f"could not cache the extraction of {rom.name}: {exc}") from exc
            finally:
                emulator.extraction_phase = None

            # Re-looked up under game_dir rather than carried over from
            # staged: a relative symlink resolves against wherever it now
            # sits, so a member contained inside scratch can point outside
            # this one once renamed.
            boot = self._find_boot_target(game_dir)
            if boot is None:
                raise RuntimeError(f"{rom.name} extracted but {self._missing_target_error}")
            _touch_last_accessed(game_dir)
            log.info("%s extraction cache: extracted %s, booting %s", self._name, rom.name, boot)
        return boot
