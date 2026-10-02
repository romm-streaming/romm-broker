"""Opt-in local copy cache for ROMs that live on a slow library mount.

With `ROM_CACHE_ENABLED` on, a launch of a launcher that sets
`Emulator.rom_cacheable` boots from a copy of the ROM on local disk instead of
from `ROM_ROOT`. The copy is made either before the boot (`blocking`) or
alongside a boot from the library, for the next launch (`background`).

Only `launch()` ever sees a cached path. Resolving the boot file, the session's
`rom_file`, save restores and the activate response all stay on the `ROM_ROOT`
path, so the many `ROM_ROOT` containment checks in the launchers keep working
unchanged. The launchers that write the booted path into save data (resume
state owner markers) map it back with `logical()`.

An entry mirrors the ROM's path under `ROM_ROOT`, so
`<ROM_CACHE_DIR>/<key>/library/ps2/game.iso` is the copy of
`<ROM_ROOT>/library/ps2/game.iso`. Launchers that read a title id from the
ROM's parent folders (Xenia, RPCS3) see the same folder names either way, and
mapping a cached path back is pure path arithmetic.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Callable, NamedTuple, Optional

from .. import settings
from .base import Emulator

log = logging.getLogger(__name__)

PHASE = "copying_rom"
"""The `extraction_phase` a blocking copy reports while the launch waits on it."""

MANIFEST = ".rom-cache.json"
"""File inside an entry naming its source and the fingerprint the copy was taken at."""

LAST_ACCESSED_MARKER = ".last_accessed"
"""File inside an entry, touched on every launch, whose mtime drives age and LRU eviction."""

_SCRATCH_DIR_NAME = ".scratch"
"""Subdirectory of the cache dir copies are made in before they are renamed into place."""

_GB = 1024**3
"""Bytes per GB, the unit `ROM_CACHE_MAX_GB` is expressed in."""

_DAY = 86400.0
"""Seconds per day, the unit `ROM_CACHE_MAX_AGE_DAYS` is expressed in."""

_CHUNK = 4 * 1024 * 1024
"""Bytes read and written per step of a copy, and the granularity of its rate cap and timeout."""

_FALLBACK_COPY_TIMEOUT = 300.0
"""The documented `ROM_CACHE_COPY_TIMEOUT` default, used when it is set to 0 or less."""

_FREE_RESERVE_FRACTION = 0.05
"""Share of the cache filesystem a copy must leave free, so the cache never fills the disk."""

_SHEET_SUFFIXES = (".cue", ".m3u", ".gdi", ".ccd", ".toc", ".mds")
"""Single-file ROMs that name other files: copied alone, they would boot without their tracks."""

_KEY_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
"""Characters replaced in the readable half of an entry name."""

_state_lock = threading.Lock()
"""Guards the entries on disk and `_active` against a background copy evicting or replacing them."""

_copy_slot = threading.Lock()
"""Held for the whole of a copy. Taken without waiting, so only one copy ever runs at a time."""

_active: Optional[str] = None
"""The key of the entry the most recent launch booted from, which eviction always keeps."""

_background_thread: Optional[threading.Thread] = None
"""The most recent background copy, so a caller (a test) can wait for it."""

_sleep = time.sleep
"""How a rate-capped copy waits; a module attribute so a test can stub it without touching `time`."""


class _Plan(NamedTuple):
    """What one ROM's cache entry is made of and where it boots from."""

    key: str
    """The entry's directory name under the cache dir."""
    rel_source: Path
    """The ROM path RomM handed over, relative to `ROM_ROOT`, as copied into the entry."""
    rel_boot: Path
    """The resolved boot file, relative to `ROM_ROOT`, as it sits inside the entry."""
    fingerprint: str
    """Sizes and nanosecond mtimes of everything copied; a mismatch means the copy is stale."""
    size: int
    """Total bytes the copy holds."""


class _NoRoom(Exception):
    """A copy that would not fit under the size cap or in the disk's free space."""


class _Budget:
    """Paces a copy to a rate cap and enforces its deadline, one chunk at a time."""

    def __init__(self, mbps: float, timeout: Optional[float]) -> None:
        """Start the clock.

        Args:
            mbps: Speed cap in MB/s, or 0 for none.
            timeout: Seconds the whole copy may take, or None for no limit.
        """
        self._start = time.monotonic()
        self._bytes = 0
        self._rate = mbps * 1_000_000 if mbps > 0 else 0.0
        self._deadline = None if timeout is None else self._start + timeout
        self._timeout = timeout

    def spend(self, n: int) -> None:
        """Account for `n` more bytes copied, sleeping when ahead of the cap.

        Args:
            n: Bytes just written.

        Raises:
            TimeoutError: When the copy has run past its deadline.
        """
        self._bytes += n
        now = time.monotonic()
        if self._deadline is not None and now >= self._deadline:
            raise TimeoutError(f"timed out after {self._timeout:.0f}s")
        if self._rate:
            ahead = self._bytes / self._rate - (now - self._start)
            if ahead > 0:
                _sleep(ahead)


def _misplaced() -> Optional[str]:
    """Why the cache dir cannot be used, or None when it can.

    A cache dir that is, or sits inside, `ROM_ROOT` copies the slow mount onto
    itself, and RomM may scan the copies as new games. One that holds `ROM_ROOT`
    would take the library's folders for entries. Either way eviction would be
    deleting folders the cache never made, so the cache stays off instead.
    """
    try:
        root = settings.ROM_CACHE_DIR.resolve()
        lib = settings.rom_root()
    except OSError as exc:
        return f"it could not be resolved: {exc}"
    if root.is_relative_to(lib):
        return f"it is inside ROM_ROOT ({lib})"
    if lib.is_relative_to(root):
        return f"it holds ROM_ROOT ({lib})"
    return None


def entry_of(path: Path) -> Optional[Path]:
    """The cache entry a cached path lives in.

    Args:
        path: Any path.

    Returns:
        The entry directory, or None when `path` is not inside a cache entry.
    """
    split = _split(path)
    return None if split is None else split[0]


def _split(path: Path) -> Optional[tuple[Path, Path]]:
    """Split a cached path into its entry and its path relative to `ROM_ROOT`.

    Args:
        path: Any path.

    Returns:
        The entry directory and the path inside it, or None when `path` is not
        inside a cache entry, or when the cache dir is one `_misplaced` refuses.
    """
    # A refused cache dir holds no entries: at or above ROM_ROOT, every library
    # path would read as cached and logical() would map it to a different game.
    if _misplaced() is not None:
        return None
    try:
        root = settings.ROM_CACHE_DIR.resolve()
        real = path.resolve()
    except OSError:
        return None
    if not real.is_relative_to(root):
        return None
    parts = real.relative_to(root).parts
    if len(parts) < 2 or parts[0] == _SCRATCH_DIR_NAME:
        return None
    return root / parts[0], Path(*parts[1:])


def logical(path: Path) -> Path:
    """Map a cached path back to the ROM it is a copy of.

    Anything that records the booted path in data that outlives the session
    (the owner markers DuckStation and Flycast write beside a resume state,
    which travel to RomM in the save archive) has to go through this, or a
    state saved from the cache would never resume from the library and the
    other way round.

    Args:
        path: A path a launcher was handed to boot.

    Returns:
        The matching path under `ROM_ROOT` for a cached path, else `path` unchanged.
    """
    split = _split(path)
    if split is None:
        return path
    return settings.rom_root() / split[1]


def _fingerprint(rom_path: Path) -> Optional[tuple[str, int]]:
    """Fingerprint a ROM file or folder by its files' sizes and nanosecond mtimes.

    Nanoseconds because a library sync replaces a dump within the same second
    often enough that second granularity would keep a stale copy.

    Args:
        rom_path: The ROM file or folder.

    Returns:
        The fingerprint and the total size in bytes, or None when the folder
        holds a symlink or anything other than plain files and folders, which
        the cache refuses: a link could pull a file from outside the library
        into the copy.

    Raises:
        OSError: When the ROM cannot be read.
    """
    st = rom_path.stat()
    if stat.S_ISREG(st.st_mode):
        return f"{st.st_size}:{st.st_mtime_ns}", st.st_size
    rows = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(rom_path):
        here = Path(dirpath)
        for name in dirnames + filenames:
            p = here / name
            pst = p.lstat()
            if stat.S_ISLNK(pst.st_mode):
                log.info("rom cache: not caching %s, it holds a symlink at %s", rom_path, p)
                return None
            if stat.S_ISREG(pst.st_mode):
                rows.append(f"{p.relative_to(rom_path).as_posix()}:{pst.st_size}:{pst.st_mtime_ns}")
                total += pst.st_size
            elif not stat.S_ISDIR(pst.st_mode):
                log.info("rom cache: not caching %s, %s is not a regular file", rom_path, p)
                return None
    digest = hashlib.sha1("\n".join(sorted(rows)).encode()).hexdigest()
    return digest, total


def _plan(rom_path: Path, rom_file: Path) -> Optional[_Plan]:
    """Work out whether and how a ROM is cached.

    Args:
        rom_path: The validated ROM file or folder RomM handed over.
        rom_file: The file the launcher resolved to boot.

    Returns:
        The plan, or None when this ROM is booted from the library as is.
    """
    lib = settings.rom_root()
    if not rom_path.is_relative_to(lib):
        return None
    if rom_file != rom_path and not rom_file.is_relative_to(rom_path):
        # The launcher already staged the boot file somewhere of its own,
        # such as an extraction cache; copying the source would store it twice.
        log.debug("rom cache: %s boots %s from outside it, not caching", rom_path, rom_file)
        return None
    if rom_path.is_file() and rom_path.suffix.lower() in _SHEET_SUFFIXES:
        log.info("rom cache: not caching %s, a lone %s names files beside it", rom_path, rom_path.suffix)
        return None
    fp = _fingerprint(rom_path)
    if fp is None:
        return None
    readable = _KEY_UNSAFE_RE.sub("_", rom_path.name)[:40]
    digest = hashlib.sha1(str(rom_path).encode()).hexdigest()[:12]
    return _Plan(
        key=f"{readable}-{digest}",
        rel_source=rom_path.relative_to(lib),
        rel_boot=rom_file.relative_to(lib),
        fingerprint=fp[0],
        size=fp[1],
    )


def _read_manifest(entry: Path) -> Optional[dict]:
    """An entry's manifest, or None when it is missing or unreadable."""
    try:
        data = json.loads((entry / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _touch(entry: Path) -> None:
    """Record a launch of `entry` now, for age and LRU eviction."""
    try:
        (entry / LAST_ACCESSED_MARKER).write_text(str(time.time()))
    except OSError as exc:
        log.warning("rom cache: could not record a launch of %s: %s", entry.name, exc)


def boot_path(rom_path: Path, rom_file: Path, emulator: Emulator) -> Path:
    """The path to hand `launch()`: a local copy of the ROM when there is a usable one.

    Never fails a launch. Whatever goes wrong with the cache, the answer is
    `rom_file`, and the game boots from the library as it would with the
    cache off.

    Args:
        rom_path: The validated ROM file or folder RomM handed over.
        rom_file: The file the launcher resolved to boot, under `rom_path`.
        emulator: The launching emulator. It opts in through `rom_cacheable`,
            and a blocking copy reports itself through its `extraction_phase`.

    Returns:
        The cached copy of `rom_file`, or `rom_file` itself.
    """
    try:
        if not settings.ROM_CACHE_ENABLED or not emulator.rom_cacheable:
            return rom_file
        problem = _misplaced()
        if problem is not None:
            log.warning(
                "rom cache: ROM_CACHE_DIR %s is unusable, %s; booting %s from the library",
                settings.ROM_CACHE_DIR,
                problem,
                rom_path,
            )
            return rom_file
        return _boot_path(rom_path, rom_file, emulator)
    except Exception:
        log.warning(
            "rom cache: could not use the cache for %s, booting it from the library", rom_path, exc_info=True
        )
        return rom_file


def _boot_path(rom_path: Path, rom_file: Path, emulator: Emulator) -> Path:
    """`boot_path` without its catch-all, so every failure lands in one place."""
    global _active
    plan = _plan(rom_path, rom_file)
    if plan is None:
        with _state_lock:
            _active = None
        return rom_file
    entry = settings.ROM_CACHE_DIR / plan.key
    cached = entry / plan.rel_boot
    with _state_lock:
        manifest = _read_manifest(entry)
        if manifest is not None and manifest.get("fingerprint") == plan.fingerprint and cached.is_file():
            _active = plan.key
            _touch(entry)
            log.info("rom cache: booting %s from its local copy", rom_path)
            return cached
        _active = None
    if not _copy_slot.acquire(blocking=False):
        log.info("rom cache: another copy is running, booting %s from the library", rom_path)
        return rom_file
    if manifest is not None and manifest.get("fingerprint") != plan.fingerprint:
        log.info("rom cache: %s changed in the library since it was copied, copying it again", rom_path)
    if settings.ROM_CACHE_MODE != "blocking":
        _start_copy(_copy_in_background, rom_path, plan)
        return rom_file
    timeout = settings.ROM_CACHE_COPY_TIMEOUT
    if timeout <= 0:
        timeout = _FALLBACK_COPY_TIMEOUT
    outcome: list[bool] = []

    def copy(rom_path: Path, plan: _Plan) -> None:
        try:
            outcome.append(_populate(rom_path, plan, mbps=0.0, timeout=timeout))
        except Exception:
            log.warning("rom cache: blocking copy of %s failed", rom_path, exc_info=True)
        finally:
            _copy_slot.release()

    # Its own thread, because the budget only checks the deadline between chunks: a read
    # hung on a stalled NFS mount would otherwise hold activate and the session lock.
    emulator.extraction_phase = PHASE
    try:
        thread = _start_copy(copy, rom_path, plan)
        thread.join(timeout)
    finally:
        emulator.extraction_phase = None
    if thread.is_alive():
        log.warning(
            "rom cache: copying %s timed out after %gs, booting it from the library", rom_path, timeout
        )
        return rom_file
    if not outcome or not outcome[0]:
        return rom_file
    with _state_lock:
        _active = plan.key
    return cached


def _start_copy(target: Callable[[Path, _Plan], None], rom_path: Path, plan: _Plan) -> threading.Thread:
    """Run `target` on a copy thread; it owns the copy slot `_boot_path` took and must free it.

    Args:
        target: The copy to run, called with `rom_path` and `plan`.
        rom_path: The ROM file or folder.
        plan: Its entry.

    Returns:
        The started thread, also kept in `_background_thread`.
    """
    global _background_thread
    try:
        thread = threading.Thread(target=target, args=(rom_path, plan), name="rom-cache-copy", daemon=True)
        thread.start()
    except BaseException:
        _copy_slot.release()
        raise
    _background_thread = thread
    return thread


def _copy_in_background(rom_path: Path, plan: _Plan) -> None:
    """Copy a ROM for its next launch, then free the copy slot `_boot_path` took for it."""
    try:
        _populate(rom_path, plan, mbps=settings.ROM_CACHE_COPY_MBPS, timeout=None)
    except Exception:
        log.warning("rom cache: background copy of %s failed", rom_path, exc_info=True)
    finally:
        _copy_slot.release()


def join_background_copy(timeout: float) -> None:
    """Wait for the most recent background copy to finish.

    Args:
        timeout: Seconds to wait at most.
    """
    thread = _background_thread
    if thread is not None:
        thread.join(timeout)


def _write_chunk(fo: BinaryIO, chunk: bytes) -> None:
    """Write one chunk of a copy; its own function so a test can fail a copy partway."""
    fo.write(chunk)


def _copy_file(src: Path, dst: Path, budget: _Budget) -> None:
    """Copy one file, refusing to follow a symlink swapped in since the fingerprint.

    Args:
        src: The library file.
        dst: Where the copy goes; must not exist.
        budget: The copy's rate cap and deadline.
    """
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fi, open(dst, "xb") as fo:
        while True:
            chunk = fi.read(_CHUNK)
            if not chunk:
                break
            _write_chunk(fo, chunk)
            budget.spend(len(chunk))
    shutil.copymode(src, dst)


def _copy(src: Path, dst: Path, budget: _Budget) -> None:
    """Copy a ROM file or folder to `dst`, layout kept.

    Args:
        src: The library file or folder.
        dst: Where it goes; its parent is created.
        budget: The copy's rate cap and deadline.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        _copy_file(src, dst, budget)
        return
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        here = Path(dirpath)
        out = dst / here.relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for name in sorted(filenames):
            _copy_file(here / name, out / name, budget)


def _require_room(plan: _Plan) -> None:
    """Refuse a copy the disk cannot hold while keeping its free-space reserve.

    Args:
        plan: The copy about to be made.

    Raises:
        _NoRoom: When the copy would eat into the reserve.
    """
    root = settings.ROM_CACHE_DIR
    usage = shutil.disk_usage(root)
    reserve = usage.total * _FREE_RESERVE_FRACTION
    if usage.free - plan.size < reserve:
        raise _NoRoom(
            f"it needs {plan.size / _GB:.1f} GB and only {usage.free / _GB:.1f} GB is free on {root},"
            f" of which {reserve / _GB:.1f} GB is kept spare"
        )


def _populate(rom_path: Path, plan: _Plan, *, mbps: float, timeout: Optional[float]) -> bool:
    """Copy a ROM into a scratch dir and rename it into place once it is whole.

    The rename is what keeps a copy cut short (a restart, a full disk, a
    timeout) from ever being booted: until it happens there is no entry, and
    the startup sweep clears what was left in scratch.

    Args:
        rom_path: The ROM file or folder.
        plan: Its entry.
        mbps: Speed cap in MB/s, or 0 for none.
        timeout: Seconds the copy may take, or None for no limit.

    Returns:
        True when the entry is in place.
    """
    root = settings.ROM_CACHE_DIR
    cap = settings.ROM_CACHE_MAX_GB
    if cap > 0 and plan.size > cap * _GB:
        log.warning(
            "rom cache: not caching %s, it is %.1f GB and ROM_CACHE_MAX_GB is %g",
            rom_path, plan.size / _GB, cap,
        )
        return False
    scratch = root / _SCRATCH_DIR_NAME / f"{plan.key}-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    try:
        scratch.mkdir(parents=True)
        with _state_lock:
            _evict_locked(keep=plan.key, incoming_bytes=plan.size, incoming_count=1)
            _require_room(plan)
        _copy(rom_path, scratch / plan.rel_source, _Budget(mbps, timeout))
        after = _fingerprint(rom_path)
        if after is None or after[0] != plan.fingerprint:
            raise RuntimeError("it changed in the library while it was being copied")
        (scratch / MANIFEST).write_text(
            json.dumps({"source": str(rom_path), "fingerprint": plan.fingerprint, "bytes": plan.size}) + "\n",
            encoding="utf-8",
        )
        with _state_lock:
            entry = root / plan.key
            if entry.exists():
                _discard(entry)
            os.replace(scratch, entry)
            _touch(entry)
    except TimeoutError as exc:
        log.warning("rom cache: copying %s %s, booting it from the library", rom_path, exc)
        return False
    except _NoRoom as exc:
        log.warning("rom cache: not caching %s: %s", rom_path, exc)
        return False
    except (OSError, RuntimeError) as exc:
        log.warning("rom cache: could not copy %s: %s", rom_path, exc)
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    log.info(
        "rom cache: copied %s (%.1f MB) in %.1fs",
        rom_path, plan.size / 1_000_000, time.monotonic() - started,
    )
    return True


def _last_launched(entry: Path) -> float:
    """When `entry` was last launched, falling back to its own mtime."""
    for p in (entry / LAST_ACCESSED_MARKER, entry):
        try:
            return p.stat().st_mtime
        except OSError:
            continue
    return 0.0


def _entry_size(entry: Path) -> int:
    """Bytes `entry` holds, from its manifest, else by walking it."""
    manifest = _read_manifest(entry)
    if manifest is not None and isinstance(manifest.get("bytes"), int):
        return manifest["bytes"]
    total = 0
    for p in entry.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError as exc:
            log.debug("rom cache: skipping unreadable %s while sizing %s: %s", p, entry.name, exc)
    return total


def _discard(entry: Path) -> None:
    """Delete an entry by renaming it into scratch first, so it is never seen half deleted.

    `rmtree` deletes one file at a time. Cut short (a crash, a file it cannot
    remove), it would leave an entry still holding its manifest and boot file
    but missing a disc or a track, and the hit check would boot it. Renamed
    into scratch, it stops being an entry at once, and the startup sweep
    finishes whatever the delete leaves behind.

    Args:
        entry: The entry directory.

    Raises:
        OSError: When the entry cannot be moved aside.
    """
    aside = settings.ROM_CACHE_DIR / _SCRATCH_DIR_NAME / f"{entry.name}-{uuid.uuid4().hex[:8]}-discarded"
    aside.parent.mkdir(parents=True, exist_ok=True)
    os.replace(entry, aside)
    try:
        shutil.rmtree(aside)
    except OSError as exc:
        log.warning("rom cache: could not delete %s, the next startup will: %s", aside.name, exc)


def _remove(entry: Path, why: str) -> None:
    """Evict one entry, logging why."""
    log.info("rom cache: evicting %s (%s)", entry.name, why)
    try:
        _discard(entry)
    except OSError as exc:
        log.warning("rom cache: could not evict %s: %s", entry.name, exc)


def _evict_locked(keep: Optional[str], incoming_bytes: int, incoming_count: int) -> None:
    """Apply the age, count and size limits in one pass. Callers hold `_state_lock`.

    Each limit is independent and off at 0, and an entry goes as soon as it
    breaks any limit that is set, so the strictest one decides. The entry the
    last launch booted from and the one being copied are never evicted.

    Args:
        keep: The key of an entry about to be (re)written, or None. Its old copy
            is neither evicted nor counted, since the incoming copy replaces it.
        incoming_bytes: Bytes a copy about to be made will add.
        incoming_count: Entries a copy about to be made will add (0 or 1).
    """
    root = settings.ROM_CACHE_DIR
    if not root.is_dir():
        return
    protected = {k for k in (keep, _active) if k}
    # The manifest is written before an entry is renamed into place, so a
    # folder without one is something else sharing the dir, never ours to delete.
    # `keep` is left out entirely: the incoming copy replaces it, so counting
    # both would evict another game to make room the cache never needs.
    entries = [
        (_last_launched(d), d)
        for d in root.iterdir()
        if d.is_dir() and d.name not in (_SCRATCH_DIR_NAME, keep) and (d / MANIFEST).is_file()
    ]
    entries.sort(key=lambda e: e[0])

    max_age = settings.ROM_CACHE_MAX_AGE_DAYS
    if max_age > 0:
        cutoff = time.time() - max_age * _DAY
        survivors = []
        for last, d in entries:
            if last < cutoff and d.name not in protected:
                _remove(d, f"not launched in {max_age:g} days")
            else:
                survivors.append((last, d))
        entries = survivors

    def drop_oldest(why: str) -> bool:
        for i, (_last, d) in enumerate(entries):
            if d.name not in protected:
                _remove(d, why)
                del entries[i]
                return True
        return False

    max_count = settings.ROM_CACHE_MAX_COUNT
    if max_count > 0:
        while len(entries) + incoming_count > max_count:
            if not drop_oldest(f"over ROM_CACHE_MAX_COUNT={max_count}"):
                break

    max_gb = settings.ROM_CACHE_MAX_GB
    if max_gb > 0:
        sizes = {d: _entry_size(d) for _last, d in entries}
        cap = max_gb * _GB
        while sum(sizes[d] for _last, d in entries) + incoming_bytes > cap:
            if not drop_oldest(f"over ROM_CACHE_MAX_GB={max_gb:g}"):
                break


def evict() -> None:
    """Apply the age, count and size limits now."""
    if not settings.ROM_CACHE_ENABLED or _misplaced() is not None:
        return
    with _state_lock:
        _evict_locked(keep=None, incoming_bytes=0, incoming_count=0)


def startup() -> None:
    """Clear copies a dead broker left in scratch, then apply the limits.

    Call once at broker startup, before any launch: nothing can be copying
    yet, so anything in scratch is an orphan.
    """
    if not settings.ROM_CACHE_ENABLED:
        return
    problem = _misplaced()
    if problem is not None:
        log.error(
            "rom cache: ROM_CACHE_DIR %s is unusable, %s; the cache stays off",
            settings.ROM_CACHE_DIR,
            problem,
        )
        return
    scratch = settings.ROM_CACHE_DIR / _SCRATCH_DIR_NAME
    try:
        if scratch.is_dir():
            for orphan in scratch.iterdir():
                log.warning("rom cache: removing a copy cut short by a restart: %s", orphan.name)
                shutil.rmtree(orphan, ignore_errors=True)
        evict()
    except OSError as exc:
        log.warning("rom cache: startup sweep of %s failed: %s", settings.ROM_CACHE_DIR, exc)
