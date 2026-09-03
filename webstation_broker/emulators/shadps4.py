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
later launch. Those formats are only bootable at all with `CACHE_ENABLED`,
since a multi-GB extraction thrown away on every launch buys nothing; with
the cache off `resolve_rom_file` refuses them and only natively bootable
formats work. An archive is unpacked to a scratch dir first to locate the
`.pkg` it holds; only pkg_extractor's own output lands in the game dir, so
the cache key is taken from the archive itself rather than the throwaway
scratch extraction.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from .. import settings
from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

XDG_DATA_HOME = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
"""The XDG data root, `~/.local/share` when `XDG_DATA_HOME` is unset."""
VERSIONS_DIR = Path(
    os.environ.get(
        "SHADPS4_VERSIONS_DIR",
        str(Path.home() / ".local/share/shadPS4QtLauncher/versions"),
    )
)
"""Where the launcher downloads builds, one folder per release (env `SHADPS4_VERSIONS_DIR`).

Defaults to `~/.local/share/shadPS4QtLauncher/versions`.
"""
DATA_DIR = Path(os.environ.get("SHADPS4_DATA_DIR", str(Path(XDG_DATA_HOME) / "shadPS4")))
"""shadPS4's data root holding save data (env `SHADPS4_DATA_DIR`, default `$XDG_DATA_HOME/shadPS4`)."""
SHADPS4_LOG_PATH = Path(os.environ.get("SHADPS4_LOG_PATH", "/config/shadps4.log"))
"""The emulator log file (env `SHADPS4_LOG_PATH`, default `/config/shadps4.log`)."""

SAVEDATA_SUBTREE = "home/1000/savedata"
"""Save data under the default PS4 user, relative to `DATA_DIR`; the whole save archive."""
_MOUNT_MARKER_DIR = "sce_sys"
"""Per-save metadata directory shadPS4 keeps the mount marker in."""
_MOUNT_MARKER_NAME = "corrupted"
"""File shadPS4 drops into `sce_sys` while a save is mounted read-write, removed on unmount."""

SHADPS4_CONFIG_PATH = Path(
    os.environ.get("SHADPS4_CONFIG_PATH", str(DATA_DIR / "config.json"))
)
"""shadPS4's own config file (env `SHADPS4_CONFIG_PATH`, default `<data>/config.json`)."""

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

_GB = 1024**3
"""Bytes per GB, the unit CACHE_MAX_GB and the space checks are expressed in."""

_ARCHIVE_EXTS = (".7z", ".zip", ".rar")
"""Archive formats that may hold a `.pkg`, extracted before pkg_extractor ever sees it."""

ROM_EXTENSIONS = (".zar", ".bin", ".pkg") + _ARCHIVE_EXTS
"""Bootable formats: a game folder (eboot.bin inside it), a .zar archive, a raw
.pkg, or a .7z/.zip/.rar archive holding one."""


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


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
# CACHE_DIR and reused on every later launch. CACHE_ENABLED therefore gates
# whether .pkg/archive ROMs are bootable at all, not just whether the
# extraction is kept. Mirrors rpcs3's identically-named archive cache.
CACHE_DIR = Path(os.environ.get("SHADPS4_CACHE_DIR", str(DATA_DIR / "extracted")))
CACHE_ENABLED = _truthy(os.environ.get("SHADPS4_CACHE_ENABLED", "false"))
CACHE_MAX_GB = float(os.environ.get("SHADPS4_CACHE_MAX_GB", "30"))
_LAST_ACCESSED_MARKER = ".last_accessed"
_SCRATCH_DIR_NAME = ".scratch"
"""Subdirectory of CACHE_DIR every archive scratch extraction lives under.

Keeping scratch dirs out of CACHE_DIR's top level means a cache entry and a
scratch dir can never be confused by name, so eviction and the startup sweep
both work off location rather than guessing from a filename.
"""

# Serializes cache-dir mutation: eviction picking a victim, extraction of a
# new one, and the boot-target lookup that follows all touch the same
# CACHE_DIR tree, so one launch's eviction can't rmtree a directory another
# launch is mid-extracting into or about to boot from.
_CACHE_LOCK = Lock()

_CACHE_LOCK_WAIT = float(os.environ.get("SHADPS4_CACHE_LOCK_WAIT", "120"))
"""Seconds a caller waits for `_CACHE_LOCK` before giving up (env `SHADPS4_CACHE_LOCK_WAIT`).

The lock is held for a whole extraction, which is bounded only by
`PKG_EXTRACT_TIMEOUT` (1800 s by default). An untimed acquire would park a
second launch's request thread for that long with nothing to show for it, so
it gives up and says why instead.
"""

_CONFIG_LOCK = Lock()
"""Serializes the read/modify/write of shadPS4's config.json across launch threads."""


@contextmanager
def _cache_lock(what: str) -> Iterator[None]:
    """Hold `_CACHE_LOCK` for the block, giving up after `_CACHE_LOCK_WAIT`.

    Args:
        what: The operation waiting for the lock, named in the log and the error.

    Yields:
        Nothing; the lock is released when the block ends.

    Raises:
        RuntimeError: When the lock is still held elsewhere after `_CACHE_LOCK_WAIT`.
    """
    if not _CACHE_LOCK.acquire(timeout=_CACHE_LOCK_WAIT):
        log.error(
            "shadps4 cache: %s gave up after waiting %.0fs for the cache lock",
            what, _CACHE_LOCK_WAIT,
        )
        raise RuntimeError(
            f"another shadps4 extraction is still running; {what} waited "
            f"{_CACHE_LOCK_WAIT:.0f}s for the extraction cache"
        )
    try:
        yield
    finally:
        _CACHE_LOCK.release()

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
    except OSError:
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


def _extracted_dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.name == _LAST_ACCESSED_MARKER:
            continue
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _cache_size_bytes() -> int:
    if not CACHE_DIR.is_dir():
        return 0
    return sum(_extracted_dir_size(d) for d in CACHE_DIR.iterdir() if d.is_dir())


def _touch_last_accessed(game_dir: Path) -> None:
    try:
        (game_dir / _LAST_ACCESSED_MARKER).write_text(str(time.time()))
    except OSError as exc:
        log.warning("shadps4 cache: could not update last-accessed marker for %s: %s", game_dir, exc)


def _evict_lru(needed_bytes: int, keep: str) -> None:
    """Evict least-recently-used extracted titles until `needed_bytes` fits within CACHE_MAX_GB.

    Args:
        needed_bytes: Additional bytes that must fit under the cache cap.
        keep: The cache key currently being (re-)extracted, so a stale
            entry for it already removed by the caller is never chosen.
    """
    if not CACHE_ENABLED or not CACHE_DIR.is_dir():
        return
    max_bytes = int(CACHE_MAX_GB * 1024**3)
    current = _cache_size_bytes()
    while current + needed_bytes > max_bytes:
        candidates = []
        for game_dir in CACHE_DIR.iterdir():
            if not game_dir.is_dir() or game_dir.name in (keep, _SCRATCH_DIR_NAME):
                continue
            marker = game_dir / _LAST_ACCESSED_MARKER
            try:
                mtime = marker.stat().st_mtime if marker.exists() else 0.0
            except OSError:
                mtime = 0.0
            candidates.append((mtime, game_dir))
        if not candidates:
            log.warning("shadps4 cache: nothing left to evict under the %.0f GB cap", CACHE_MAX_GB)
            return
        candidates.sort(key=lambda c: c[0])
        victim = candidates[0][1]
        victim_size = _extracted_dir_size(victim)
        log.info("shadps4 cache: evicting %s (least recently used)", victim.name)
        try:
            shutil.rmtree(victim)
        except OSError as exc:
            log.warning("shadps4 cache: could not evict %s: %s", victim, exc)
            return
        current -= victim_size


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


def _cache_key(rom: Path) -> str:
    """Cache dir name for rom (a .pkg or an archive).

    Its stem plus a short hash of the file's own name, size, and mtime. A
    bare stem collides two ROMs that share a name, and survives a
    same-named re-upload with different content, either of which would
    otherwise serve up whatever is sitting in the old cache dir as if it
    were the new ROM.
    """
    try:
        st = rom.stat()
        fingerprint = f"{rom.name}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        fingerprint = rom.name
    digest = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    return f"{rom.stem}-{digest}"


def _run_extractor(cmd: list[str], what: str) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PKG_EXTRACT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
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


def _reject_unsafe_members(dest: Path, members: list[str]) -> None:
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
            log.error("shadps4: archive member name holds a control character: %r", member)
            raise RuntimeError(f"archive member name holds a control character: {member!r}")
        target = (dest / member).resolve()
        if target != dest_real and dest_real not in target.parents:
            raise RuntimeError(f"archive member escapes extraction dir: {member}")


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract `zf` into dest after rejecting any Zip Slip member.

    zf.extractall() writes a member's path verbatim, so a `../` name in
    the archive can escape dest; reject any member that would first.
    """
    _reject_unsafe_members(dest, zf.namelist())
    zf.extractall(dest)


def _rar_member_paths(archive: Path) -> list[str]:
    """List member paths from an archive.

    Bare paths from `unrar lb`, one per line, no header or column
    formatting to parse around.

    Args:
        archive: The .rar to list.

    Returns:
        One path per member.

    Raises:
        RuntimeError: When the listing names no member. `_reject_unsafe_members`
            checks exactly this list, so an empty parse would wave the whole
            archive through unchecked and has to stop the extraction instead.
    """
    listing = _run_extractor(["unrar", "lb", "-y", str(archive)], f"unrar list ({archive.name})")
    members = [line for line in listing.splitlines() if line.strip()]
    if not members:
        log.error("shadps4: unrar listed no members in %s", archive.name)
        raise RuntimeError(f"unrar listed no members in {archive.name}")
    return members


def _7z_member_paths(archive: Path) -> list[str]:
    """List member paths from an archive.

    Parsed from `7z l -slt`, the only 7z listing mode that gives a full
    untruncated path per entry. Everything before the dashed separator line
    describes the archive itself, not its contents.

    Args:
        archive: The .7z, or any other archive 7z can identify, to list.

    Returns:
        One path per member.

    Raises:
        RuntimeError: When the listing carries no separator line or names no
            member. `_reject_unsafe_members` checks exactly this list, so a
            listing shaped differently than expected (another 7z build, a
            localized one) has to stop the extraction rather than wave every
            member through unchecked.
    """
    listing = _run_extractor(["7z", "l", "-slt", str(archive)], f"7z list ({archive.name})")
    lines = listing.splitlines()
    body: Optional[list[str]] = None
    for i, line in enumerate(lines):
        if _7Z_SEPARATOR_RE.match(line):
            body = lines[i + 1:]
            break
    if body is None:
        log.error("shadps4: 7z listing of %s has no member section", archive.name)
        raise RuntimeError(f"7z listing of {archive.name} has no member section")
    members = [line[len(_7Z_PATH_PREFIX):] for line in body if line.startswith(_7Z_PATH_PREFIX)]
    if not members:
        log.error("shadps4: 7z listed no members in %s", archive.name)
        raise RuntimeError(f"7z listed no members in {archive.name}")
    return members


def _reject_escaped_tree(dest: Path) -> None:
    """Post-extraction safety net for the .rar/.7z paths.

    unrar/7z extraction is trusted to confine writes under dest, but the
    pre-extraction member-name check above parses each tool's own text
    listing to decide what's safe before anything is written, and a member
    name holding a raw control character can render differently in that
    listing than in the archive's real central directory. Rather than trust
    the listing as a proxy for what actually landed on disk, walk the real
    result: any symlink whose target resolves outside dest, or any entry
    not contained under dest at all, means the extractor's own traversal
    protection didn't hold.

    Every offender is logged with the host path it points at, because that is
    where the extractor may have written and it is the one thing the caller
    cannot clean up on its own judgement.

    Args:
        dest: The directory the extraction was confined to.

    Raises:
        RuntimeError: If any entry resolves outside dest or cannot be resolved.
    """
    dest_real = dest.resolve()
    escaped: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
        base = Path(dirpath)
        for name in dirnames + filenames:
            p = base / name
            try:
                target_real = p.resolve()
            except OSError as exc:
                raise RuntimeError(f"could not resolve extracted member {p}: {exc}") from exc
            if target_real != dest_real and dest_real not in target_real.parents:
                log.error("shadps4: extracted member %s points outside %s, at %s", p, dest, target_real)
                escaped.append(p)
    if escaped:
        raise RuntimeError(f"extracted member escapes cache dir: {escaped[0]}")


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
    ext = archive.suffix.lower()
    log.info("shadps4: extracting %s (%s)", archive.name, ext)
    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive) as zf:
                _safe_extract_zip(zf, dest)
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError(f"zip extraction of {archive.name} failed: {exc}") from exc
    elif ext == ".rar":
        _reject_unsafe_members(dest, _rar_member_paths(archive))
        _run_extractor(["unrar", "x", "-y", str(archive), f"{dest}/"], f"unrar ({archive.name})")
    else:
        # .7z, plus a fallback attempt for any other archive format 7z can
        # identify.
        _reject_unsafe_members(dest, _7z_member_paths(archive))
        _run_extractor(["7z", "x", "-y", str(archive), f"-o{dest}"], f"7z ({archive.name})")
    if ext != ".zip":
        try:
            _reject_escaped_tree(dest)
        except RuntimeError:
            # unrar/7z have already written by the time this runs, so what
            # they left goes rather than staying for a later launch to boot.
            _purge_extraction(dest, archive.name)
            raise


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
        raise RuntimeError(f"pkg_extractor failed to run on {pkg.name}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"pkg_extractor exited {result.returncode} on {pkg.name}: {result.stderr.strip()}"
        )


def _clear_scratch() -> None:
    """Remove every scratch dir under CACHE_DIR. Callers must hold _CACHE_LOCK.

    Everything under `_SCRATCH_DIR_NAME` is scratch by construction, so the
    whole subtree goes without inspecting names or contents. Guessing from
    either would be unsound in both directions: a real cache dir's key can
    contain any substring the ROM's own filename does, and a scratch dir
    holds whatever the archive held, up to and including a file named
    eboot.bin.

    The lock is what makes this safe: no extraction can be mid-flight while
    it is held, so anything still sitting here was orphaned by a process
    that died.
    """
    scratch_root = CACHE_DIR / _SCRATCH_DIR_NAME
    if not scratch_root.is_dir():
        return
    for entry in scratch_root.iterdir():
        log.warning("shadps4 cache: removing orphaned scratch dir %s", entry.name)
        shutil.rmtree(entry, ignore_errors=True)


def sweep_stale_extractions() -> None:
    """Remove extraction scratch dirs orphaned by a crashed broker process.

    `tempfile.TemporaryDirectory` cleans up on normal exit, but a killed
    process leaves its scratch dir behind forever. Call once at broker
    startup so the space is reclaimed before the first launch rather than
    only when the next extraction happens to run.
    """
    try:
        with _cache_lock("startup scratch sweep"):
            _clear_scratch()
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
    forever. An archive is unpacked to a second scratch dir first to locate
    the .pkg it holds; only pkg_extractor's own output is kept, so
    relaunching the same archive still hits the cache even though its
    scratch extraction is discarded every time.

    Holds _CACHE_LOCK for the whole call: eviction, extraction, and the
    boot-target lookup all touch the same CACHE_DIR tree, so a second
    launch racing in here must wait rather than potentially evicting the
    directory this one is mid-extracting into or about to boot from. The
    wait is bounded by `_CACHE_LOCK_WAIT`, since the lock is held for as
    long as an extraction takes.

    Args:
        rom: The .pkg or archive to extract.
        emulator: The launching emulator; `emulator.extraction_phase` is set
            while this runs so RomM can poll it, and cleared again before
            returning or raising.

    Raises:
        RuntimeError: If another extraction still holds the cache lock, the
            extraction cannot fit in the cache or on the disk, archive
            extraction or pkg_extractor fails, the archive holds no .pkg, the
            extraction holds no eboot.bin, it outgrew the whole cache cap, or
            it cannot be moved to its cache key.
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

        is_archive = rom.suffix.lower() in _ARCHIVE_EXTS
        # Set before eviction, not after: eviction can rmtree tens of GB
        # under the lock, and a caller polling extraction_phase should see
        # that stall rather than an idle-looking None.
        emulator.extraction_phase = "extracting_archive" if is_archive else "extracting_pkg"
        try:
            try:
                size = rom.stat().st_size
            except OSError:
                size = 0
            # An archive needs its scratch extraction and pkg_extractor's
            # staged output living under CACHE_DIR at the same time; only the
            # output survives, so the two figures differ for an archive and
            # coincide for a bare .pkg.
            kept = int(size * PKG_EXPANSION_FACTOR)
            peak = int(size * ARCHIVE_PEAK_FACTOR) if is_archive else kept
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
                if is_archive:
                    unpacked = Path(scratch) / "archive"
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
                _check_expansion(_extracted_dir_size(staged), kept, rom.name)
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
        log_path: The emulator log file.
        term_timeout: Seconds STOP gets before SIGTERM (env `SHADPS4_STOP_WAIT`, default 20).
    """

    name = "shadps4"
    display_name = "shadPS4"
    save_root = DATA_DIR
    save_subtrees = (SAVEDATA_SUBTREE,)
    """Save data plus its per-title param.sfo, under the default PS4 user."""
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
        if CACHE_ENABLED:
            return ROM_EXTENSIONS
        return tuple(e for e in ROM_EXTENSIONS if e != ".pkg" and e not in _ARCHIVE_EXTS)

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
            if not CACHE_ENABLED and path.suffix.lower() in (".pkg",) + _ARCHIVE_EXTS:
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
        except OSError:
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
        log.info("launching shadps4 (rom=%s, boot=%s, binary=%s)", rom_path, boot, binary)
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
