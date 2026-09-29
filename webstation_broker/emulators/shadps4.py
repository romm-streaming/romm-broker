"""shadPS4 (PlayStation 4) launcher: binary selection, ROM resolution, and IPC-driven shutdown.

shadPS4 has no save states. Persistence is the game's own save data, which
the game commits to plain host files under
`<data>/home/<user_id>/savedata/<game_serial>/<slot>/`. Save paths are keyed
by the game serial, so shipping the whole `home/1000/savedata` subtree makes
a save archive restored into a fresh container line up with the titles it
belongs to.

Control plane: shadPS4's IPC protocol (`SHADPS4_ENABLE_IPC=true`) reads
commands from stdin. We feed RUN then START so the game boots headlessly,
and STOP for a graceful quit (it pushes SDL_EVENT_QUIT, the same path as a
window close). shadPS4 registers no SIGTERM/SIGINT handler, so SIGTERM would
kill the process hard and leave read-write save mounts with their
`sce_sys/corrupted` marker in place; STOP must come first and SIGTERM is
only the escalation fallback.

shadPS4 has no PKG installer of its own; a `.pkg` ROM, or a `.7z`/`.zip`/
`.rar` archive holding one, is unpacked with the standalone `pkg_extractor`
tool into CACHE_DIR, mirroring rpcs3's archive cache
(`webstation_broker/emulators/rpcs3.py`): extracted once and reused on every
later launch. Those formats are only bootable at all with `SHADPS4_CACHE_ENABLED`,
since a multi-GB extraction thrown away on every launch buys nothing; with
the cache off `resolve_rom_file` refuses them and only natively bootable
formats work. An archive is unpacked to a scratch dir first to locate the
`.pkg` it holds; only pkg_extractor's own output lands in the game dir, so
the cache key is taken from the archive itself rather than the throwaway
scratch extraction.
"""

import functools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Optional, Union

from .. import imports, settings
from . import extraction_cache
from .base import Emulator, base_launch_env, xdg_data_dir
from .extraction_cache import ExtractionCache

log = logging.getLogger(__name__)

VERSIONS_DIR = Path(
    os.environ.get(
        "SHADPS4_VERSIONS_DIR",
        str(Path.home() / ".local/share/shadPS4QtLauncher/versions"),
    )
)
"""Where the launcher downloads builds, one folder per release (env `SHADPS4_VERSIONS_DIR`).

Defaults to `~/.local/share/shadPS4QtLauncher/versions`.
"""
DATA_DIR = xdg_data_dir("shadPS4")
"""shadPS4's data root, holding the save data the archive is built from.

Not configurable, and deliberately: nothing on shadPS4's command line names it,
so an override would move only the tree the broker dumps and restores and leave
shadPS4 writing its own. That loses saves without reporting anything. `launch`
exports the root this resolved to instead.
"""
SHADPS4_LOG_PATH = Path(os.environ.get("SHADPS4_LOG_PATH", "/config/shadps4.log"))
"""The emulator log file (env `SHADPS4_LOG_PATH`, default `/config/shadps4.log`)."""

SAVEDATA_SUBTREE = "home/1000/savedata"
"""Save data under the default PS4 user, relative to `DATA_DIR`; the whole save archive."""
_MOUNT_MARKER_DIR = "sce_sys"
"""Per-save metadata directory shadPS4 keeps the mount marker in."""
_MOUNT_MARKER_NAME = "corrupted"
"""File shadPS4 drops into `sce_sys` while a save is mounted read-write, removed on unmount."""
_EXPECTED = "<serial>/<save dir>/<file>, optionally under home/<n>/savedata/ or savedata/"
"""The shape an import member is asked to take, for refusals."""
_SERIAL = re.compile(r"[A-Za-z]{4}[0-9]{5}", re.ASCII)
"""A title serial such as `CUSA12345`, in either case."""
_USER = re.compile(r"[0-9]+", re.ASCII)
"""A PS4 user folder's name below `home`."""
_NOT_SAVES = frozenset({"config.json", "extracted", "sys_modules"})
"""Top-level names in shadPS4's data directory beside `home`, which a whole-directory export carries.

None of them is save data, so a member under one is refused as such rather than as a save with no serial.
"""
_BOOT_NAME = "eboot.bin"
"""The file a PS4 game folder boots from, and the one `resolve_rom_file` names inside a folder."""
_PARAM_SFO_REL = ("sce_sys", "param.sfo")
"""Where a PS4 game folder keeps the metadata naming its serial, relative to the folder."""
_SFO_MAGIC = b"\x00PSF"
"""The four bytes an SFO starts with."""
_SFO_HEADER_BYTES = 0x14
"""The fixed header before an SFO's index: magic, version, the two table offsets and the entry count."""
_SFO_ENTRY_BYTES = 16
"""One SFO index entry: the key offset, the value's format and length, and the value offset."""
_SFO_MAX_BYTES = 64 * 1024
"""Most of a `param.sfo` that is read. A real one is a few kilobytes; the file comes from the library."""
_SFO_KEY_MAX_BYTES = 32
"""Most of a key name that is compared, since keys are NUL-terminated rather than sized."""
_SFO_VALUE_MAX_BYTES = 64
"""Most of a value that is read: a serial is nine characters, and the length field is the file's word."""
_TITLE_ID_KEY = b"TITLE_ID"
"""The SFO key holding a PS4 game's serial, such as `CUSA12345`."""

SHADPS4_CONFIG_PATH = DATA_DIR / "config.json"
"""shadPS4's own config file, `config.json` under `DATA_DIR`.

Not configurable, for the same reason as `DATA_DIR`: shadPS4 looks for it beside
its save data and takes no flag naming it, so an override would leave the broker
pinning a GPU in a file the emulator never opens.
"""

SHADPS4_GPU_ID = os.environ.get("SHADPS4_GPU_ID", "auto")
"""Vulkan device index pinned into config.json before each launch (env `SHADPS4_GPU_ID`, default `auto`).

shadPS4's own `gpu_id: -1` (auto-select) can land on a CPU-rendered Vulkan
device (llvmpipe, swiftshader, ...) instead of a real GPU: the game keeps
running (audio, playtime counter) but every frame is presented black, since
software rendering never keeps up with the presentation deadline. `auto`
picks a real device via `_detect_gpu_id`, vendor-agnostic; an integer pins
that index directly for a host `vulkaninfo` cannot read; `-1` or `KEEP`
leaves config.json alone. The choice persists in config.json, so it is
pinned before each launch regardless of which one shadPS4 wrote last.
"""

VULKANINFO_BIN = os.environ.get("SHADPS4_VULKANINFO_BIN", "vulkaninfo")
"""The `vulkaninfo` binary used by `_detect_gpu_id` (env `SHADPS4_VULKANINFO_BIN`)."""

_GPU_TYPE_PRIORITY = {
    "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU": 0,
    "PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU": 1,
    "PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU": 2,
}
"""vulkaninfo `deviceType` values worth pinning to, best first.

`PHYSICAL_DEVICE_TYPE_CPU` (llvmpipe, swiftshader, ...) and anything not
listed here are never picked: this is the same vendor-neutral field
regardless of whether the real device is AMD, NVIDIA, Intel, or virtio-gpu.
"""

_GPU_BLOCK_RE = re.compile(r"^GPU(\d+):\s*$", re.MULTILINE)
"""Matches a `GPUn:` device header in `vulkaninfo --summary` output."""
_DEVICE_TYPE_RE = re.compile(r"^\s*deviceType\s*=\s*(\S+)\s*$", re.MULTILINE)
"""Matches the `deviceType = ...` line within one device's block."""

_VULKANINFO_TIMEOUT = 10
"""Seconds one `vulkaninfo --summary` probe may take before it is abandoned."""

_GPU_DETECT_LOCK = Lock()
"""Guards the detection memo and its attempt counter, both read/written from launch threads."""

_DETECTED_GPU_ID: Optional[int] = None
"""Memoized successful `_detect_gpu_id` result; None means "not detected yet"."""

_GPU_DETECT_ATTEMPTS = 0
"""Failed probes so far, counted against `_MAX_GPU_DETECT_ATTEMPTS`."""

_MAX_GPU_DETECT_ATTEMPTS = 3
"""Failures after which detection stops retrying, so a vulkaninfo that hangs
rather than exits costs `_VULKANINFO_TIMEOUT` a few times instead of on every
launch for the broker's lifetime."""

_GB = extraction_cache._GB
"""Bytes per GB, the unit CACHE_MAX_GB and the space checks are expressed in."""

_ARCHIVE_EXTS = extraction_cache._ARCHIVE_EXTS
"""Archive formats that may hold a `.pkg`, extracted before pkg_extractor ever sees it."""

ROM_EXTENSIONS = (".zar", ".bin", ".pkg") + _ARCHIVE_EXTS
"""Bootable formats: a game folder (eboot.bin inside it), a .zar archive, a raw
.pkg, or a .7z/.zip/.rar archive holding one."""


PKG_EXTRACTOR_BIN = os.environ.get("SHADPS4_PKG_EXTRACTOR_BIN", "pkg_extractor")
"""The `pkg_extractor` binary (env `SHADPS4_PKG_EXTRACTOR_BIN`, default `pkg_extractor` on PATH)."""
PKG_EXTRACT_TIMEOUT = float(os.environ.get("SHADPS4_PKG_EXTRACT_TIMEOUT", "1800"))
"""Seconds a pkg_extractor run gets before it is considered hung (env `SHADPS4_PKG_EXTRACT_TIMEOUT`)."""

PKG_EXPANSION_FACTOR = float(os.environ.get("SHADPS4_PKG_EXPANSION_FACTOR", "1.1"))
"""Assumed extracted-size to .pkg-size ratio (env `SHADPS4_PKG_EXPANSION_FACTOR`, default 1.1).

A PS4 .pkg is already compressed per-file, so pkg_extractor's output lands
close to the package's own size; the margin covers the filesystem overhead of
many small files. It is only an estimate, so `_check_expansion` reports a
title that outgrows it instead of letting the space guards look like they
held.
"""
ARCHIVE_PEAK_FACTOR = float(os.environ.get("SHADPS4_ARCHIVE_PEAK_FACTOR", "2.2"))
"""Assumed peak-on-disk to archive-size ratio (env `SHADPS4_ARCHIVE_PEAK_FACTOR`, default 2.2).

An archive holds its unpacked .pkg and pkg_extractor's output at the same
moment, so the disk carries roughly twice `PKG_EXPANSION_FACTOR` at the height
of the run even though only the output survives.
"""

# PS4 titles run several GB decrypted, so a .pkg is extracted once into
# CACHE_DIR and reused on every later launch. SHADPS4_CACHE_ENABLED therefore gates
# whether .pkg/archive ROMs are bootable at all, not just whether the
# extraction is kept. Mirrors rpcs3's identically-named archive cache.
CACHE_DIR = Path(os.environ.get("SHADPS4_CACHE_DIR", str(DATA_DIR / "extracted")))
CACHE_MAX_GB = float(os.environ.get("SHADPS4_CACHE_MAX_GB", "30"))
_LAST_ACCESSED_MARKER = extraction_cache._LAST_ACCESSED_MARKER
_SCRATCH_DIR_NAME = extraction_cache._SCRATCH_DIR_NAME
"""Subdirectory of CACHE_DIR every archive scratch extraction lives under.

Keeping scratch dirs out of CACHE_DIR's top level means a cache entry and a
scratch dir can never be confused by name, so eviction and the startup sweep
both work off location rather than guessing from a filename.
"""

_CACHE_LOCK_WAIT = float(os.environ.get("SHADPS4_CACHE_LOCK_WAIT", "120"))
"""Seconds a caller waits for `_CACHE_LOCK` before giving up (env `SHADPS4_CACHE_LOCK_WAIT`).

The lock is held for a whole extraction, which is bounded only by
`PKG_EXTRACT_TIMEOUT` (1800 s by default). An untimed acquire would park a
second launch's request thread for that long with nothing to show for it, so
it gives up and says why instead.
"""

_CONFIG_LOCK = Lock()
"""Serializes the read/modify/write of shadPS4's config.json across launch threads."""

BIN_NAME = os.environ.get("SHADPS4_BIN_NAME", "Shadps4-sdl.AppImage")
"""The binary looked for inside a release folder (env `SHADPS4_BIN_NAME`, default `Shadps4-sdl.AppImage`).

Release folders look like `v0.17.0 - Garbage Collector's Edition - 2026-07-30`.
The `Pre-release` folder always carries the newest build and trumps all.
"""
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
"""Parses the semver prefix of a release folder name."""
_PRE_RELEASE_DIR = "pre-release"
"""Lowercased name of the pre-release folder, skipped by the semver scan."""


def _find_binary_in(folder: Path) -> Optional[Path]:
    """The shadPS4 binary inside one release folder.

    Args:
        folder: A release folder under `VERSIONS_DIR`.

    Returns:
        `BIN_NAME` when present, else the first `*.AppImage` by name, else None.
    """
    candidate = folder / BIN_NAME
    if candidate.is_file():
        return candidate
    for p in sorted(folder.glob("*.AppImage")):
        if p.is_file():
            return p
    return None


def _resolve_binary() -> Optional[Path]:
    """Latest shadps4 binary.

    The explicit `SHADPS4_BIN` override, else the Pre-release build if
    present, else the newest semver release folder.

    Returns:
        The binary path, or None (logged) when no usable build exists.
    """
    override = os.environ.get("SHADPS4_BIN")
    if override:
        return Path(override)
    if not VERSIONS_DIR.is_dir():
        log.warning("shadps4 versions dir not found: %s", VERSIONS_DIR)
        return None

    pre = _find_binary_in(VERSIONS_DIR / "Pre-release")
    if pre is not None:
        log.info("shadps4: using pre-release build %s", pre)
        return pre

    best: Optional[tuple[tuple[int, int, int], Path]] = None
    for folder in VERSIONS_DIR.iterdir():
        if not folder.is_dir() or folder.name.lower() == _PRE_RELEASE_DIR:
            continue
        binary = _find_binary_in(folder)
        if binary is None:
            continue
        m = _VERSION_RE.match(folder.name)
        if m is None:
            continue
        version = (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))
        if best is None or version > best[0]:
            best = (version, binary)
    if best is not None:
        log.info("shadps4: using release %s (%s)", best[0], best[1])
        return best[1]
    log.warning("shadps4: no usable binary under %s", VERSIONS_DIR)
    return None


def _is_safe_extracted_member(candidate: Path, root_real: Path) -> bool:
    """Check that `candidate` is safe to use as a boot target or extraction input.

    False for anything but a regular file resolving inside root_real, so a
    symlink planted by the archive or pkg cannot point shadps4 (or
    pkg_extractor) at a path elsewhere on the host.
    """
    try:
        return candidate.is_file() and candidate.resolve().is_relative_to(root_real)
    except OSError as exc:
        log.debug("shadps4: could not resolve %s to check extraction safety: %s", candidate, exc)
        return False


def _extracted_boot_target(root: Path) -> Optional[Path]:
    """The eboot.bin pkg_extractor produced inside its `<TITLE_ID>` output subfolder."""
    root_real = root.resolve()
    for eboot in root.rglob("eboot.bin"):
        if _is_safe_extracted_member(eboot, root_real):
            return eboot
    return None


def _archive_pkg_member(root: Path) -> Optional[Path]:
    """The first `.pkg` file inside an extracted archive tree."""
    root_real = root.resolve()
    for pkg in root.rglob("*.pkg"):
        if _is_safe_extracted_member(pkg, root_real):
            return pkg
    return None


def _require_room(peak_bytes: int, kept_bytes: int, rom_name: str) -> None:
    """Refuse an extraction that cannot fit before any of it is written.

    Eviction leaves two ceilings standing: an empty cache still cannot hold a
    title larger than CACHE_MAX_GB, and the cap counts only the cache's own
    contents, not the free space on the filesystem it shares with the rest of
    /config. Without this the unpack starts anyway, spends minutes filling the
    disk, and dies on a write error from inside the extractor, having taken
    the free space every other service on that filesystem needs with it.

    The two ceilings take different numbers. An archive holds its unpacked
    .pkg and pkg_extractor's output at once but keeps only the output, so
    charging the cap for that transient peak would refuse titles that sit
    well under it once extracted. The cap gets what survives; the disk gets
    what is on it at the worst moment.

    Args:
        peak_bytes: Bytes on disk at the height of the extraction.
        kept_bytes: Bytes the finished extraction leaves in the cache.
        rom_name: The ROM being extracted, named in the error.

    Raises:
        RuntimeError: If the cache cap or the filesystem cannot hold it.
    """
    max_bytes = int(CACHE_MAX_GB * _GB)
    current = _cache_size_bytes()
    if current + kept_bytes > max_bytes:
        raise RuntimeError(
            f"{rom_name} would leave about {kept_bytes / _GB:.1f} GB cached, more than "
            f"SHADPS4_CACHE_MAX_GB ({CACHE_MAX_GB:.0f} GB) allows with "
            f"{current / _GB:.1f} GB already there"
        )
    try:
        free = shutil.disk_usage(CACHE_DIR).free
    except OSError as exc:
        log.warning("shadps4 cache: could not read free space on %s: %s", CACHE_DIR, exc)
        return
    if free < peak_bytes:
        raise RuntimeError(
            f"{rom_name} needs about {peak_bytes / _GB:.1f} GB to extract, but only "
            f"{free / _GB:.1f} GB is free on {CACHE_DIR}"
        )


def _check_expansion(actual_bytes: int, reserved_bytes: int, rom_name: str) -> None:
    """Report an extraction that outgrew the room reserved for it, refusing an unkeepable one.

    `_require_room` sizes both space guards from `PKG_EXPANSION_FACTOR`, an
    assumption about how far a .pkg expands rather than a measurement. A title
    that expands further has already slipped past the free-space guard by the
    time the extraction finishes, so the mismatch is named here instead of
    passing for a normal run, and a result too big for the cap is refused
    rather than cached over it.

    Args:
        actual_bytes: What the finished extraction occupies.
        reserved_bytes: What `_require_room` charged the cache cap for it.
        rom_name: The ROM being extracted, named in the log and the error.

    Raises:
        RuntimeError: If the finished extraction is larger than CACHE_MAX_GB.
    """
    if actual_bytes <= reserved_bytes:
        return
    log.error(
        "shadps4 cache: %s extracted to %.2f GB, past the %.2f GB reserved for it; "
        "SHADPS4_PKG_EXPANSION_FACTOR (%.2f) is too low for this title, so the "
        "free-space guard was sized short",
        rom_name, actual_bytes / _GB, reserved_bytes / _GB, PKG_EXPANSION_FACTOR,
    )
    max_bytes = int(CACHE_MAX_GB * _GB)
    if actual_bytes > max_bytes:
        raise RuntimeError(
            f"{rom_name} extracted to about {actual_bytes / _GB:.1f} GB, more than "
            f"SHADPS4_CACHE_MAX_GB ({CACHE_MAX_GB:.0f} GB) allows"
        )


def _purge_extraction(dest: Path, what: str) -> None:
    """Empty an extraction directory whose contents failed the escape check.

    Only what sits under dest can be reclaimed. A member the tool wrote
    through an escaping symlink landed on a host path that already belonged to
    something else, and deleting that would finish what the archive started,
    so those are named in the log for an operator to judge instead.

    Args:
        dest: The extraction directory to empty.
        what: The archive being extracted, named in the log.
    """
    log.error("shadps4: discarding the unsafe extraction of %s under %s", what, dest)
    shutil.rmtree(dest, ignore_errors=True)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("shadps4: could not recreate the extraction dir %s: %s", dest, exc)


def _extract_archive(archive: Path, dest: Path) -> None:
    """Extract an archive holding a .pkg into dest, bounded by PKG_EXTRACT_TIMEOUT per tool run.

    unrar/7z have already written by the time an escaped entry is found, so
    what they left is purged rather than staying for a later launch to boot.
    """
    extraction_cache.extract_archive(archive, dest, PKG_EXTRACT_TIMEOUT, on_escape=_purge_extraction)


def _run_pkg_extractor(pkg: Path, dest: Path) -> None:
    """Run pkg_extractor on pkg, writing its `<TITLE_ID>` output folder under dest.

    pkg_extractor prompts for a keypress once done; an empty line on stdin
    satisfies that with no live terminal attached.
    """
    try:
        result = subprocess.run(
            [PKG_EXTRACTOR_BIN, str(pkg), str(dest)],
            input="\n",
            capture_output=True,
            text=True,
            timeout=PKG_EXTRACT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("shadps4: pkg_extractor failed to run on %s: %s", pkg.name, exc)
        raise RuntimeError(f"pkg_extractor failed to run on {pkg.name}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"pkg_extractor exited {result.returncode} on {pkg.name}: {result.stderr.strip()}"
        )


def _stage_pkg(rom: Path, staged: Path, scratch: Path, emulator: Emulator, kept_bytes: int) -> None:
    """Get rom (a .pkg, or an archive holding one) staged and confirmed bootable.

    An archive is unpacked to a second scratch dir first to locate the .pkg
    it holds; only pkg_extractor's own output is kept, so relaunching the
    same archive still hits the cache even though its scratch extraction is
    discarded every time.

    Args:
        rom: The .pkg or archive to extract.
        staged: The directory pkg_extractor's output must land in.
        scratch: The scratch dir `staged` sits under, used for the archive's
            own throwaway unpack when `rom` is an archive.
        emulator: The launching emulator; `extraction_phase` is flipped to
            `extracting_pkg` mid-run for the archive-then-pkg case.
        kept_bytes: Bytes `_require_room` reserved in the cache for this
            extraction, checked against what it actually produced.

    Raises:
        RuntimeError: If `rom` is an archive holding no .pkg, or the staged
            extraction holds no eboot.bin.
    """
    is_archive = rom.suffix.lower() in _ARCHIVE_EXTS
    if is_archive:
        unpacked = scratch / "archive"
        unpacked.mkdir()
        _extract_archive(rom, unpacked)
        pkg = _archive_pkg_member(unpacked)
        if pkg is None:
            raise RuntimeError(f"{rom.name} extracted but held no .pkg")
        emulator.extraction_phase = "extracting_pkg"
        _run_pkg_extractor(pkg, staged)
    else:
        _run_pkg_extractor(rom, staged)
    if _extracted_boot_target(staged) is None:
        raise RuntimeError(f"{rom.name} extracted but held no eboot.bin")
    _check_expansion(_extracted_dir_size(staged), kept_bytes, rom.name)


def _budget_pkg(rom: Path) -> tuple[int, int]:
    """Bytes to reserve for extracting rom: (peak bytes on disk, bytes kept in the cache).

    An archive needs its scratch extraction and pkg_extractor's staged
    output living under CACHE_DIR at the same time; only the output
    survives, so the two figures differ for an archive and coincide for a
    bare .pkg.

    Args:
        rom: The .pkg or archive about to be extracted.

    Returns:
        The (peak_bytes, kept_bytes) pair `_require_room` and `_evict_lru` budget against.
    """
    is_archive = rom.suffix.lower() in _ARCHIVE_EXTS
    try:
        size = rom.stat().st_size
    except OSError as exc:
        log.debug("shadps4 cache: could not stat %s, treating size as 0: %s", rom, exc)
        size = 0
    kept = int(size * PKG_EXPANSION_FACTOR)
    peak = int(size * ARCHIVE_PEAK_FACTOR) if is_archive else kept
    return (peak, kept)


def _phase_for(rom: Path) -> str:
    """The `extraction_phase` value to report while rom is first staged."""
    return "extracting_archive" if rom.suffix.lower() in _ARCHIVE_EXTS else "extracting_pkg"


_CACHE = ExtractionCache(
    name="shadps4",
    cache_dir=lambda: CACHE_DIR,
    enabled=lambda: settings.SHADPS4_CACHE_ENABLED,
    max_gb=lambda: CACHE_MAX_GB,
    find_boot_target=_extracted_boot_target,
    lock_wait=lambda: _CACHE_LOCK_WAIT,
)
"""Owns shadps4's cache-dir lock and its size/eviction bookkeeping.

`_extract_and_cache_pkg` orchestrates the extraction itself rather than
calling `_CACHE.extract()`: shadps4's over-cap error names
`SHADPS4_CACHE_MAX_GB` specifically (the shared class's own message is
generic), and `tests/test_shadps4.py` monkeypatches `_evict_lru` and
`_require_room` by module attribute to assert the orchestration order,
which only works when the orchestrating code looks those names up from
this module rather than from inside `ExtractionCache.extract`'s own
method body.

Because of that, `_CACHE` is never given `budget`/`stage`/`phase_name`/
`missing_target_error` here, and nothing in this module calls
`_CACHE.extract()`. Calling it directly is unsupported: it would fall back
to the shared class's generic defaults (member-listing-based size
budgeting, a plain extract-and-check stage that does not know how to run
pkg_extractor or unpack an archive-holding-a-pkg) instead of this module's
own `_check_expansion`/`_require_room` accounting, and its error text
would name `max_gb` rather than `SHADPS4_CACHE_MAX_GB`.
"""

_cache_key = extraction_cache._cache_key
_extracted_dir_size = extraction_cache._dir_size
_touch_last_accessed = extraction_cache._touch_last_accessed
_cache_size_bytes = _CACHE._cache_size_bytes
_evict_lru = _CACHE._evict_lru
_clear_scratch = _CACHE._clear_scratch


@contextmanager
def _cache_lock(what: str) -> Iterator[None]:
    """Hold the shared cache lock for the block, giving up after `_CACHE_LOCK_WAIT`.

    Args:
        what: The operation waiting for the lock, named in the log and the error.

    Yields:
        Nothing; the lock is released when the block ends.

    Raises:
        RuntimeError: When the lock is still held elsewhere after `_CACHE_LOCK_WAIT`.
    """
    with _CACHE._locked(what):
        yield


_CACHE_LOCK = _CACHE._lock
"""The lock `_cache_lock` holds, exposed for tests that assert on it directly."""


def sweep_stale_extractions() -> None:
    """Remove extraction scratch dirs orphaned by a crashed broker process.

    `tempfile.TemporaryDirectory` cleans up on normal exit, but a killed
    process leaves its scratch dir behind forever. Call once at broker
    startup so the space is reclaimed before the first launch rather than
    only when the next extraction happens to run.
    """
    try:
        _CACHE.sweep_stale_extractions()
    except RuntimeError as exc:
        log.warning("shadps4 cache: startup scratch sweep skipped: %s", exc)


def _extract_and_cache_pkg(rom: Path, emulator: Emulator) -> Path:
    """Get rom (a .pkg, or an archive holding one) booting from CACHE_DIR.

    Reuses a prior extraction keyed by `_cache_key` when one already holds
    a bootable eboot.bin. Everything is unpacked under `_SCRATCH_DIR_NAME`
    and only renamed to the persistent game_dir once a boot target is
    confirmed, so game_dir either does not exist or holds a complete
    extraction: a process killed mid-run leaves scratch to be reclaimed
    rather than a truncated eboot.bin the next launch would cache-hit on
    forever.

    Holds the shared cache lock for the whole call: eviction, extraction,
    and the boot-target lookup all touch the same CACHE_DIR tree, so a
    second launch racing in here must wait rather than potentially evicting
    the directory this one is mid-extracting into or about to boot from.
    The wait is bounded by `_CACHE_LOCK_WAIT`, since the lock is held for as
    long as an extraction takes.

    Args:
        rom: The .pkg or archive to extract.
        emulator: The launching emulator; `emulator.extraction_phase` is set
            while this runs so RomM can poll it, and cleared again before
            returning or raising.

    Raises:
        RuntimeError: If another extraction still holds the cache lock, the
            ROM cannot be read to key it, the extraction cannot fit in the
            cache or on the disk, archive extraction or pkg_extractor fails,
            the archive holds no .pkg, the extraction holds no eboot.bin, it
            outgrew the whole cache cap, or it cannot be moved to its cache
            key.
        OSError: If CACHE_DIR or a scratch dir cannot be created at all.
    """
    with _cache_lock(rom.name):
        key = _cache_key(rom)
        game_dir = CACHE_DIR / key

        if game_dir.is_dir():
            boot = _extracted_boot_target(game_dir)
            if boot is not None:
                log.info("shadps4 cache hit: %s (boot target: %s)", rom.name, boot)
                _touch_last_accessed(game_dir)
                return boot
            log.warning("shadps4 cache: %s has no boot target, re-extracting", rom.name)
            shutil.rmtree(game_dir, ignore_errors=True)

        # Set before eviction, not after: eviction can rmtree tens of GB
        # under the lock, and a caller polling extraction_phase should see
        # that stall rather than an idle-looking None.
        emulator.extraction_phase = _phase_for(rom)
        try:
            peak, kept = _budget_pkg(rom)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            # Orphaned scratch is un-evictable but still counts toward the
            # cap, so reclaim it before sizing the cache rather than letting
            # it push real entries out.
            _clear_scratch()
            _evict_lru(kept, key)
            _require_room(peak, kept, rom.name)

            # Scratch lives under CACHE_DIR so the staged output shares a
            # filesystem with game_dir: the rename below is then atomic
            # rather than a cross-device copy, and a big archive is never
            # unpacked into a smaller /tmp.
            scratch_root = CACHE_DIR / _SCRATCH_DIR_NAME
            scratch_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f"{key}-", dir=str(scratch_root)) as scratch:
                staged = Path(scratch) / "extracted"
                staged.mkdir()
                _stage_pkg(rom, staged, Path(scratch), emulator, kept)
                # The rmtree above uses ignore_errors, so game_dir can still
                # be sitting there non-empty and the rename then fails.
                try:
                    staged.replace(game_dir)
                except OSError as exc:
                    log.error(
                        "shadps4 cache: could not move the extraction of %s into %s: %s",
                        rom.name, game_dir, exc,
                    )
                    raise RuntimeError(
                        f"could not cache the extraction of {rom.name}: {exc}"
                    ) from exc
        finally:
            emulator.extraction_phase = None

        # Re-looked up under game_dir rather than carried over from staged: a
        # relative symlink resolves against wherever it now sits, so a member
        # contained inside the scratch tree can point outside this one.
        boot = _extracted_boot_target(game_dir)
        if boot is None:
            raise RuntimeError(f"{rom.name} extracted but held no eboot.bin")
        _touch_last_accessed(game_dir)
        log.info("shadps4: extracted %s, booting %s", rom.name, boot)
    return boot


def _probe_gpu_id() -> Optional[int]:
    """The Vulkan device index of the best real GPU on this host, vendor-agnostic.

    Runs `vulkaninfo --summary` with `DISPLAY` cleared: with a live X/Wayland
    connection vulkaninfo also probes surface creation, which can fail and
    abort before the device list ever prints, and the device list is all
    this needs. Devices are ranked by their `deviceType`
    (`_GPU_TYPE_PRIORITY`), a field Vulkan itself defines the same way for
    every vendor, so this needs no AMD/NVIDIA/Intel-specific logic; a
    software rasterizer (llvmpipe, swiftshader, ...) reports as
    `PHYSICAL_DEVICE_TYPE_CPU` and is never picked. Ties go to the
    lowest-numbered device.

    Returns:
        The device index to pin, or None when vulkaninfo is missing, fails,
        times out, or every enumerated device is a CPU/unrecognized type.
    """
    env = dict(os.environ)
    env["DISPLAY"] = ""
    try:
        result = subprocess.run(
            [VULKANINFO_BIN, "--summary"],
            capture_output=True,
            text=True,
            env=env,
            timeout=_VULKANINFO_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("shadps4: could not run %s to detect a GPU (%s)", VULKANINFO_BIN, exc)
        return None
    if result.returncode != 0:
        log.warning(
            "shadps4: %s exited %d, cannot auto-detect a GPU: %s",
            VULKANINFO_BIN, result.returncode, result.stderr.strip(),
        )
        return None

    blocks = list(_GPU_BLOCK_RE.finditer(result.stdout))
    best: Optional[tuple[int, int]] = None  # (priority, device index)
    for i, block in enumerate(blocks):
        index = int(block.group(1))
        start = block.end()
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(result.stdout)
        type_match = _DEVICE_TYPE_RE.search(result.stdout, start, end)
        if type_match is None:
            continue
        priority = _GPU_TYPE_PRIORITY.get(type_match.group(1))
        if priority is None:
            continue
        if best is None or priority < best[0]:
            best = (priority, index)

    if best is None:
        log.warning("shadps4: %s found no usable GPU device, leaving gpu_id on auto-select", VULKANINFO_BIN)
        return None
    log.info("shadps4: detected GPU index %d for gpu_id pin (%s)", best[1], VULKANINFO_BIN)
    return best[1]


def _detect_gpu_id() -> Optional[int]:
    """`_probe_gpu_id`, memoized and bounded, for the launch path to call.

    A success is kept for the process lifetime, since the host's GPU set does
    not change between launches. A failure is retried: a probe that fails
    while the container is still coming up would otherwise disable the pin
    for the broker's whole lifetime, and every later launch would silently
    keep shadPS4's black-screen `-1`. Retries stop after
    `_MAX_GPU_DETECT_ATTEMPTS` so a vulkaninfo that hangs instead of exiting
    cannot cost every future launch a `_VULKANINFO_TIMEOUT` stall.

    The lock guards both globals and doubles as a guarantee that two launches
    racing here run one probe between them rather than two.

    Returns:
        The device index to pin, or None while no probe has succeeded.
    """
    global _DETECTED_GPU_ID, _GPU_DETECT_ATTEMPTS
    with _GPU_DETECT_LOCK:
        if _DETECTED_GPU_ID is not None:
            return _DETECTED_GPU_ID
        if _GPU_DETECT_ATTEMPTS >= _MAX_GPU_DETECT_ATTEMPTS:
            log.warning(
                "shadps4: not retrying GPU detection, %s failed %d times",
                VULKANINFO_BIN, _GPU_DETECT_ATTEMPTS,
            )
            return None
        detected = _probe_gpu_id()
        if detected is None:
            _GPU_DETECT_ATTEMPTS += 1
            return None
        _DETECTED_GPU_ID = detected
        return _DETECTED_GPU_ID


def _write_config(cfg: dict, mode: int) -> bool:
    """Replace config.json with `cfg`, through a temp file in the same directory.

    A truncating in-place write that fails part way (full disk, killed
    process) would leave shadPS4 a half-written config.json and lose every
    setting in it. The temp file is uniquely named rather than a fixed `.tmp`
    sibling, so two brokers pinning at once cannot write the same scratch path
    and rename half of each other's file into place.

    Args:
        cfg: The config to serialize.
        mode: Permission bits to give the replacement, normally the ones the
            config already had; mkstemp creates it owner-only.

    Returns:
        True when config.json now holds `cfg`, False when it was left alone.
    """
    parent = SHADPS4_CONFIG_PATH.parent
    try:
        fd, name = tempfile.mkstemp(dir=str(parent), prefix=f".{SHADPS4_CONFIG_PATH.name}.", suffix=".tmp")
    except OSError as exc:
        log.warning("shadps4: could not stage a config rewrite in %s (%s)", parent, exc)
        return False
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cfg, indent=2))
        os.chmod(tmp, mode)
        os.replace(tmp, SHADPS4_CONFIG_PATH)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        log.warning("shadps4: could not rewrite %s (%s)", SHADPS4_CONFIG_PATH, exc)
        return False
    return True


def _pin_gpu_id() -> None:
    """Force `Vulkan.gpu_id` in config.json to a real GPU before launch.

    A missing config is left for shadPS4 to create; its own compiled default
    is `-1`, the auto-select that can land on a software device, so a brand
    new container can still hit the bug on its very first launch, same as
    xemu's renderer pin leaves a missing xemu.toml alone.

    `_CONFIG_LOCK` is held across the read and the write so two launches
    pinning at once cannot each write the config they read before the other's
    edit landed.
    """
    with _CONFIG_LOCK:
        _pin_gpu_id_locked()


def _pin_gpu_id_locked() -> None:
    """The body of `_pin_gpu_id`; callers must hold `_CONFIG_LOCK`."""
    setting = SHADPS4_GPU_ID.strip()
    if setting.upper() in ("", "KEEP", "-1"):
        log.debug("shadps4 gpu_id pin disabled (SHADPS4_GPU_ID=%r)", SHADPS4_GPU_ID)
        return

    # Read and parse the config before detection: a missing/corrupt file
    # means there's nothing to pin regardless of what detection would say,
    # and it saves the vulkaninfo subprocess on that path entirely.
    try:
        text = SHADPS4_CONFIG_PATH.read_text(encoding="utf-8")
        mode = SHADPS4_CONFIG_PATH.stat().st_mode & 0o777
    except OSError as exc:
        log.debug("could not read %s to pin gpu_id (%s)", SHADPS4_CONFIG_PATH, exc)
        return
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("shadps4: %s is not valid JSON, leaving gpu_id alone (%s)", SHADPS4_CONFIG_PATH, exc)
        return
    if not isinstance(cfg, dict):
        log.warning("shadps4: %s is not a JSON object, leaving gpu_id alone", SHADPS4_CONFIG_PATH)
        return

    if setting.lower() == "auto":
        gpu_id = _detect_gpu_id()
        if gpu_id is None:
            return
    else:
        try:
            gpu_id = int(setting)
        except ValueError:
            log.warning(
                "shadps4: SHADPS4_GPU_ID=%r is not 'auto', 'KEEP', or an integer, "
                "leaving config.json alone", SHADPS4_GPU_ID,
            )
            return

    vulkan = cfg.setdefault("Vulkan", {})
    if not isinstance(vulkan, dict):
        log.warning("shadps4: %s Vulkan is not a JSON object, leaving gpu_id alone", SHADPS4_CONFIG_PATH)
        return
    if vulkan.get("gpu_id") == gpu_id:
        return
    vulkan["gpu_id"] = gpu_id
    if not _write_config(cfg, mode):
        log.warning("shadps4: gpu_id %d was NOT pinned into %s", gpu_id, SHADPS4_CONFIG_PATH)
        return
    log.info("shadps4: pinned Vulkan.gpu_id=%d in %s", gpu_id, SHADPS4_CONFIG_PATH)


def _unmounted_saves(savedata_root: Path) -> list[Path]:
    """Save directories shadPS4 was still holding mounted when it died.

    shadPS4 drops `sce_sys/corrupted` into a save while it has it mounted
    read-write and removes it again on unmount, so a marker still on disk once
    the process is gone names a save whose last write was never flushed
    through a clean unmount. `saves.py` strips the marker itself out of the
    dump, which means the archive that ships those saves looks clean; naming
    them here is the only trace an operator gets.

    Args:
        savedata_root: The savedata subtree to scan.

    Returns:
        The per-save directories still carrying the marker, sorted. Empty when
        the subtree is missing or cannot be walked.
    """
    if not savedata_root.is_dir():
        return []
    found: list[Path] = []
    try:
        for marker in sorted(savedata_root.rglob(_MOUNT_MARKER_NAME)):
            if marker.parent.name == _MOUNT_MARKER_DIR and marker.is_file():
                found.append(marker.parent.parent)
    except OSError as exc:
        log.warning("shadps4: could not scan %s for unmounted saves: %s", savedata_root, exc)
    return found


def _clear_stale_save_data(savedata_root: Path) -> None:
    """Empty the savedata subtree before an archive restore.

    A restore only writes the members the incoming archive happens to name, so
    an earlier session's save dirs otherwise survive into this one: readable by
    the next player, and swept into that player's exit dump, which ships the
    whole subtree rather than the titles this session booted.

    Every per-serial dir goes, not just the incoming title's: a dir named for
    another title holds another player's data just the same, and shadPS4 keys
    its save paths by game serial, so the next player booting that title would
    mount the previous one's slots. Nothing outside the subtree is touched, so
    installed titles keep their game data: pkg_extractor's output lives in
    CACHE_DIR, a sibling of the save tree rather than a part of it.

    A save shadPS4 never unmounted is named before it goes. `save_and_exit`
    reports those at the end of the session that stranded them, but a broker
    that died before dumping leaves them here with no other trace.

    Args:
        savedata_root: The savedata subtree to empty.
    """
    if not savedata_root.is_dir():
        return
    stranded = _unmounted_saves(savedata_root)
    if stranded:
        log.warning(
            "shadps4: dropping %d save(s) an earlier session left mounted, "
            "never archived: %s",
            len(stranded), ", ".join(str(p) for p in stranded),
        )
    try:
        entries = list(savedata_root.iterdir())
    except OSError as exc:
        log.warning("shadps4: could not list %s to clear stale save data: %s", savedata_root, exc)
        return
    cleared = 0
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            log.warning("shadps4: could not clear stale save data %s: %s", entry, exc)
        else:
            cleared += 1
            log.debug("shadps4: cleared stale save data %s", entry)
    if cleared:
        log.info("shadps4: cleared %d stale save entries before the restore", cleared)


def _sfo_title_id(sfo: Path) -> Optional[str]:
    """Read the `TITLE_ID` out of a PS4 `param.sfo`.

    An SFO is a fixed header, then an index of fixed-size entries, then a key
    table and a value table the entries give offsets into. Only the first
    `_SFO_MAX_BYTES` are read and every offset is treated as a hint: a short
    or overlong slice reads as no id rather than as an error, since this file
    is whatever the library holds.

    Args:
        sfo: The `param.sfo` to read.

    Returns:
        The id as the file spells it, or None when the file cannot be read,
        is not an SFO, names no title id, or holds an empty one.
    """
    try:
        with open(sfo, "rb") as fh:
            data = fh.read(_SFO_MAX_BYTES)
    except OSError as exc:
        log.debug("shadps4: could not read %s for the game serial: %s", sfo, exc)
        return None
    if len(data) < _SFO_HEADER_BYTES or data[:4] != _SFO_MAGIC:
        return None
    key_start = int.from_bytes(data[0x08:0x0C], "little")
    value_start = int.from_bytes(data[0x0C:0x10], "little")
    declared = int.from_bytes(data[0x10:0x14], "little")
    for index in range(min(declared, (len(data) - _SFO_HEADER_BYTES) // _SFO_ENTRY_BYTES)):
        off = _SFO_HEADER_BYTES + index * _SFO_ENTRY_BYTES
        entry = data[off : off + _SFO_ENTRY_BYTES]
        key_off = key_start + int.from_bytes(entry[0:2], "little")
        value_len = int.from_bytes(entry[4:8], "little")
        value_off = value_start + int.from_bytes(entry[12:16], "little")
        if data[key_off : key_off + _SFO_KEY_MAX_BYTES].split(b"\0", 1)[0] != _TITLE_ID_KEY:
            continue
        value = data[value_off : value_off + min(value_len, _SFO_VALUE_MAX_BYTES)]
        # An offset past the slice leaves nothing, which is no id rather than "".
        return value.split(b"\0", 1)[0].decode("ascii", "replace") or None
    return None


def _game_serial(rom_file: Path) -> Optional[str]:
    """The serial of the game this launch boots, from the game's own metadata.

    shadPS4 names a save folder for the serial in `sce_sys/param.sfo`, so that
    file is what says which title a session's saves belong to. A `.pkg` or an
    archive is still packed at preflight and a `.zar` holds nothing readable
    from here, so those launches have no serial to offer.

    The file is held to the same rule as every other path this module opens
    off a ROM: a regular file resolving inside the library root. A library
    that carries a FIFO, a device or a symlink out of the tree would
    otherwise block the activate thread or read a host file, and either
    `sce_sys` or `param.sfo` could be the link.

    Args:
        rom_file: The boot target `resolve_rom_file` returned: a game folder,
            its `eboot.bin`, or a packed format.

    Returns:
        The serial as `param.sfo` spells it, or None.
    """
    if rom_file.is_dir():
        folder = rom_file
    elif rom_file.name.lower() == _BOOT_NAME:
        folder = rom_file.parent
    else:
        return None
    sfo = folder.joinpath(*_PARAM_SFO_REL)
    try:
        root_real = settings.rom_root()
    except OSError as exc:
        log.debug("shadps4: could not resolve the ROM root to check %s: %s", sfo, exc)
        return None
    if not _is_safe_extracted_member(sfo, root_real):
        log.debug("shadps4: %s is not a regular file inside %s, so no serial is read", sfo, root_real)
        return None
    return _sfo_title_id(sfo)


_refuse = functools.partial(imports.refuse, expected=_EXPECTED)
"""Refuse a member, naming this emulator's accepted shapes (see `imports.refuse`)."""


def _below_wrapper(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Drop a leading `home/<n>/savedata` or `savedata`, if the path starts with one.

    `imports.match_anchored` only strips literal wrappers, and the user folder is a number, so
    the hook strips this one itself.

    Args:
        parts: A member's components.

    Returns:
        The components below the wrapper, or `parts` unchanged when there is none.
    """
    if len(parts) >= 3 and parts[0] == "home" and _USER.fullmatch(parts[1]) and parts[2] == "savedata":
        return parts[3:]
    if parts and parts[0] == "savedata":
        return parts[1:]
    return parts


def _unmatched(member: imports.ImportMember, below: tuple[str, ...]) -> imports.ImportRefusal:
    """Refuse a member that does not name a serial, a save folder and a file, saying why.

    Args:
        member: The member.
        below: Its components below any wrapper.

    Returns:
        The refusal.
    """
    parts = member.parts
    if len(parts) == 1 and parts[0].lower().endswith(".srm"):
        return _refuse(
            member,
            "source_incompatible",
            "a RetroArch save; shadPS4 saves are folders keyed by the game's serial",
        )
    other_home = parts[0] == "home" and below == parts
    if other_home and len(parts) >= 3 and parts[2] == "savedata":
        return _refuse(member, "unrecognised_layout", "the user folder below home must be a number")
    if other_home or parts[0] in _NOT_SAVES:
        return _refuse(member, "unrecognised_layout", "not save data; only the savedata folder can be sent")
    if below and _SERIAL.fullmatch(below[0]):
        return _refuse(
            member, "unrecognised_layout", "a save needs a save folder and a file below the serial"
        )
    if len(below) >= 2:
        return _refuse(
            member, "identity_unknown", "no game serial in the path to say which title this save is for"
        )
    return _refuse(member, "unrecognised_layout", "not a file in a save folder under a serial")


def _place_save(
    member: imports.ImportMember, session: imports.SessionIdentity, *, max_component_bytes: int
) -> Union[imports.Placement, imports.ImportRefusal]:
    """File a save under the default user, whatever user it was collected from.

    The serial is upper-cased, since shadPS4 names its folders that way and a hand-collected
    save may not. Everything below it keeps the member's spelling: the filesystem is case
    sensitive, and so is `SAVE00`.

    A save names the title it belongs to, so it is held to the session's when
    the session has one: the exit dump ships the whole savedata subtree, and
    another title's folder placed here would leave in this rom's archive and
    come back with it. A session whose serial is unknown compares nothing, as
    it always has.

    Args:
        member: The member.
        session: The session's identity.
        max_component_bytes: The longest name the filesystem stores.

    Returns:
        The placement, or a refusal.
    """
    below = _below_wrapper(member.parts)
    found = imports.match_anchored(below, wrappers=((),), levels=(_SERIAL,), min_tail=2)
    if found is None:
        return _unmatched(member, below)
    refusal = imports.check_member_identity(
        member,
        imports.NORMALISERS["ps_serial_nodash"](found.ids[0]),
        session,
        family="ps_serial_nodash",
        policy="strict",
        expected=_EXPECTED,
    )
    if refusal is not None:
        return refusal
    dest = imports.build_dest(
        SAVEDATA_SUBTREE,
        (found.ids[0].upper(),),
        found.tail,
        member=member,
        expected=_EXPECTED,
        max_component_bytes=max_component_bytes,
    )
    if isinstance(dest, imports.ImportRefusal):
        return dest
    return imports.Placement(member, dest)


class Shadps4(Emulator):
    """PlayStation 4 via shadPS4, driven over its stdin IPC protocol.

    The binary is picked from the launcher's versions tree at launch time
    and spawned fullscreen with `SHADPS4_ENABLE_IPC=true` and a stdin pipe.
    RUN then START are written straight away so the game boots without
    waiting on the RUN deadline, and the stop writes STOP, which pushes
    SDL_EVENT_QUIT, the same path as a window close. shadPS4 registers no
    SIGTERM/SIGINT handler, so a bare SIGTERM would kill it hard and leave
    read-write save mounts with their `sce_sys/corrupted` marker in place;
    SIGTERM is only the escalation after STOP times out or the pipe breaks.

    There are no save states: persistence is the game's own save data under
    `home/1000/savedata`, keyed by game serial, which is what the archive
    carries. A resume slot is logged and ignored.

    A `.pkg` ROM, or a `.7z`/`.zip`/`.rar` archive holding one, is unpacked
    through the CACHE_DIR extraction cache before boot, and is only bootable
    at all when that cache is enabled.

    Attributes:
        name: Provider key, `shadps4`.
        display_name: Human-readable name.
        save_root: The data directory the save subtree hangs off.
        save_subtrees: Save data plus its per-title param.sfo, under the default PS4 user.
        clears_stale_saves: On; `clear_working_slot` empties the savedata subtree.
        log_path: The emulator log file.
        term_timeout: Seconds STOP gets before SIGTERM (env `SHADPS4_STOP_WAIT`, default 20).
    """

    name = "shadps4"
    display_name = "shadPS4"
    save_root = DATA_DIR
    save_subtrees = (SAVEDATA_SUBTREE,)
    """Save data plus its per-title param.sfo, under the default PS4 user."""
    clears_stale_saves = True
    """On: `clear_working_slot` empties `home/1000/savedata` before every restore.

    Safe to empty whole because the subtree holds nothing but per-player save
    slots, keyed by game serial. Installed titles are not in it: pkg_extractor
    writes its output to CACHE_DIR, and config.json sits above the subtree, so
    neither is in reach of the clear.
    """
    log_path = SHADPS4_LOG_PATH
    term_timeout = float(os.environ.get("SHADPS4_STOP_WAIT", "20"))
    """Seconds the IPC STOP gets before SIGTERM (env `SHADPS4_STOP_WAIT`, default 20).

    STOP goes through the SDL event loop into a graceful teardown; give it
    room before escalating to SIGTERM.
    """

    def __init__(self) -> None:
        """Start with the base handles and no verdict on a shutdown yet."""
        super().__init__()
        self._graceful_exit: Optional[bool] = None
        """How the last stop of a live process went, None when none has run yet.

        True only for an IPC STOP the emulator answered on its own. False once
        the SIGTERM escalation had to run, which shadPS4 has no handler for and
        so cannot flush its save mounts through.
        """

    @property
    def rom_extensions(self) -> tuple[str, ...]:
        """Bootable formats, minus the ones needing the disabled extraction cache.

        `.pkg` and the archive formats only reach a boot target by way of
        CACHE_DIR, so with the cache off they are not bootable and must not
        be advertised as accepted.
        """
        if settings.SHADPS4_CACHE_ENABLED:
            return ROM_EXTENSIONS
        return tuple(e for e in ROM_EXTENSIONS if e != ".pkg" and e not in _ARCHIVE_EXTS)

    def clear_working_slot(self, excluded: tuple[str, ...] = ()) -> None:
        """Drop the previous session's save data before this session's restore.

        There is no save state and no working slot to reset, so the whole of
        the clear is the savedata subtree (`_clear_stale_save_data`). It goes
        whole rather than scoped to the incoming serial: the exit dump ships
        the subtree, not the titles this session booted, so another title's
        leftovers would leave in this player's archive.

        Args:
            excluded: Subtrees carried by the whole-card routes. shadPS4 names
                no memory card subtree, so this is always empty.
        """
        _clear_stale_save_data(self.save_root / SAVEDATA_SUBTREE)

    def import_spec(self) -> imports.ImportSpec:
        """Declare what shadPS4 takes: one save folder per title, and never its mount marker.

        Returns:
            The spec.
        """
        shapes = (
            "<serial>/<save dir>/<file>",
            "savedata/<serial>/<save dir>/<file>",
            "home/<n>/savedata/<serial>/<save dir>/<file>",
        )
        return imports.ImportSpec(
            kinds=(imports.KindSpec("save", shapes),),
            protected=(f"*/{_MOUNT_MARKER_DIR}/{_MOUNT_MARKER_NAME}",),
        )

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """File one declared save file under the default user's savedata.

        Args:
            member: The member, already past the kind gate.
            spec: This emulator's spec.
            ctx: The launch context, for the session's serial.

        Returns:
            The placement, or a refusal.
        """
        return _place_save(
            member, imports.identity_for(self, ctx), max_component_bytes=spec.max_component_bytes
        )

    def identity_source(self) -> Optional[imports.IdentitySource]:
        """Take the session's serial from the game's `param.sfo`, then from RomM's title id.

        Returns:
            A `ps_serial_nodash` source reading the boot target's metadata,
            which is the shape RomM writes a PS4 id in as well.
        """
        return imports.IdentitySource("ps_serial_nodash", rom_reader=_game_serial)

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The path shadPS4 should boot for `path`.

        Args:
            path: A ROM file, or a game folder.

        Returns:
            The file itself, the folder's `eboot.bin`, the folder when it
            has none (shadPS4 appends eboot.bin to directory paths itself),
            or None when the path does not exist, resolves outside the ROM
            library root, or is a `.pkg`/archive with the extraction cache
            disabled.
        """
        rom_root = settings.rom_root()
        if path.is_file():
            # Defense in depth: api.py validates the activate payload's path,
            # but a symlinked ROM file would otherwise reach both shadps4 and
            # pkg_extractor on the word of whichever caller passed it in.
            try:
                if not path.resolve().is_relative_to(rom_root):
                    log.warning("shadps4: refusing %s, it resolves outside %s", path, rom_root)
                    return None
            except OSError as exc:
                log.warning("shadps4: could not resolve %s (%s)", path, exc)
                return None
            if not settings.SHADPS4_CACHE_ENABLED and path.suffix.lower() in (".pkg",) + _ARCHIVE_EXTS:
                log.warning(
                    "shadps4: refusing %s, %s needs the extraction cache "
                    "(set SHADPS4_CACHE_ENABLED=true to boot this format)",
                    path.name,
                    path.suffix.lower(),
                )
                return None
            return path
        if not path.is_dir():
            return None
        # The folder itself, not just its eboot.bin: shadps4 appends the
        # filename to a directory path on its own, so a folder symlinked out of
        # the library would otherwise boot a host path nothing here validated.
        try:
            if not path.resolve().is_relative_to(rom_root):
                log.warning("shadps4: refusing %s, it resolves outside %s", path, rom_root)
                return None
        except OSError as exc:
            log.warning("shadps4: could not resolve %s (%s)", path, exc)
            return None
        eboot = path / "eboot.bin"
        try:
            if eboot.is_file():
                if eboot.resolve().is_relative_to(rom_root):
                    return eboot
                log.warning("shadps4: refusing %s, it resolves outside %s", eboot, rom_root)
                return None
            if eboot.exists() or eboot.is_symlink():
                # present but not a regular file: dangling symlink, or a
                # symlink to a directory/device/fifo. is_file() misses these,
                # and falling through to `return path` would hand shadps4 an
                # unvalidated target via its own eboot.bin lookup.
                return None
        except OSError as exc:
            log.debug("shadps4: could not check %s while resolving the boot target: %s", eboot, exc)
            return None
        return path  # shadps4 appends eboot.bin to directory paths itself

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Spawn the newest shadPS4 with IPC enabled and boot the game.

        Args:
            rom_path: The file or folder to boot.
            resume_slot: Ignored with a log line; shadPS4 has no save states.

        Raises:
            RuntimeError: When no binary is found under `VERSIONS_DIR`.
        """
        self.stop()
        self._graceful_exit = None
        if resume_slot is not None:
            log.info(
                "shadps4 has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        binary = _resolve_binary()
        if binary is None:
            raise RuntimeError(f"no shadps4 binary found under {VERSIONS_DIR}")
        ext = rom_path.suffix.lower()
        if ext == ".pkg" or ext in _ARCHIVE_EXTS:
            boot = _extract_and_cache_pkg(rom_path, self)
        else:
            boot = rom_path
        _pin_gpu_id()
        env = base_launch_env()
        env["SHADPS4_ENABLE_IPC"] = "true"
        # Nothing on the command line names the data root, so shadPS4 resolves
        # it itself. Export the root the broker resolved so the config.json it
        # just pinned a GPU into, and the savedata the dump reads back, are the
        # ones this launch uses.
        env["XDG_DATA_HOME"] = str(DATA_DIR.parent)
        log.info(
            "launching shadps4 (rom=%s, boot=%s, binary=%s, data=%s)",
            rom_path,
            boot,
            binary,
            DATA_DIR,
        )
        self._spawn([str(binary), "-f", "true", "-g", str(boot)], env, stdin_pipe=True)
        # The IPC input thread starts with the process and stdin buffers early
        # writes; RUN then START release the run/start semaphores so the game
        # boots without waiting on the 5 s RUN deadline.
        if not self._ipc_send("RUN"):
            log.warning("shadps4 IPC RUN failed, game may not boot until the 5 s RUN deadline")
        if not self._ipc_send("START"):
            log.warning("shadps4 IPC START failed, game may not boot")

    def _ipc_send(self, cmd: str) -> bool:
        """Write one IPC command line to the emulator's stdin.

        Args:
            cmd: The command, such as `RUN` or `START`; a newline is appended.

        Returns:
            True when the line was written and flushed, False when there is
            no live process with a stdin pipe or the pipe is broken.
        """
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            log.warning("shadps4: IPC %s not sent, no running process with a stdin pipe", cmd)
            return False
        try:
            proc.stdin.write(f"{cmd}\n".encode())
            proc.stdin.flush()
            return True
        except OSError as exc:
            log.warning("shadps4: IPC %s could not be written to stdin: %s", cmd, exc)
            return False

    def stop(self) -> None:
        """Ask shadPS4 to quit over IPC, escalating to the base SIGTERM stop.

        STOP is written to stdin and the process given `term_timeout` to
        exit on its own; a broken pipe or a timeout falls through to the
        SIGTERM then SIGKILL sequence in the base class.

        Which of the two ran is recorded in `_graceful_exit` for
        `save_and_exit` to report. shadPS4 registers no SIGTERM handler, so
        the escalation kills it wherever it happens to be, save mounts
        included; the save data the dump then ships cannot be trusted the way
        a clean IPC quit's can.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None and proc.stdin is not None:
            log.info("stopping %s (pid %d) via IPC STOP", self.name, proc.pid)
            try:
                proc.stdin.write(b"STOP\n")
                proc.stdin.flush()
                proc.wait(timeout=self.term_timeout)
            except OSError as exc:
                self._graceful_exit = False
                log.warning(
                    "%s (pid %d): IPC STOP could not be delivered (%s), escalating to "
                    "SIGTERM; save data written this session may be incomplete",
                    self.name, proc.pid, exc,
                )
            except subprocess.TimeoutExpired:
                self._graceful_exit = False
                log.warning(
                    "%s (pid %d) did not exit within %.0fs of STOP, escalating to SIGTERM; "
                    "save data written this session may be incomplete",
                    self.name, proc.pid, self.term_timeout,
                )
            else:
                self._forget()
                self._graceful_exit = True
                log.info("%s exited gracefully", self.name)
                return
        super().stop()

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Stop shadPS4 and report whether the save data it leaves can be trusted.

        There are no save states, so the state fields are always None. What
        this adds over the base is the shutdown's own verdict: the caller zips
        the save tree the moment this returns, and a stop that had to escalate
        to SIGTERM can leave a save half-written, with shadPS4's own
        `sce_sys/corrupted` marker the only sign of it.

        Args:
            slot: Ignored with a log line; shadPS4 has no save states.

        Returns:
            The state fields all None (`state_saved`, `state_slot`,
            `state_file`), plus `graceful_exit` and `unmounted_saves`, the save
            directories shadPS4 never unmounted.
        """
        if slot is not None:
            log.info("shadps4 has no save states, exit slot %s ignored", slot)
        self.stop()
        stranded = [str(p) for p in _unmounted_saves(self.save_root / SAVEDATA_SUBTREE)]
        if stranded:
            log.error(
                "shadps4: %d save(s) were never unmounted and may be mid-write; "
                "they still ship in the exit dump: %s",
                len(stranded), ", ".join(stranded),
            )
        elif self._graceful_exit is False:
            log.warning(
                "shadps4: the session was force-stopped, but no save was left mounted"
            )
        return {
            "state_saved": None,
            "state_slot": None,
            "state_file": None,
            "graceful_exit": self._graceful_exit,
            "unmounted_saves": stranded,
        }
