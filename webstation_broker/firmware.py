"""Firmware sync: the BIOS files RomM holds for a platform, placed where the emulator looks.

RomM sends the firmware uploaded to the launched game's platform on activate,
each file as a path under `ROM_ROOT` with its size and hashes. Each emulator
says where a file goes through `Emulator.firmware_targets`; this module holds
the shared half: checking the paths, naming a file by its hash, and copying it
in without ever replacing a file the broker did not put there itself.

A file is copied when the target is missing. It is left alone when the target
already holds the same bytes. It is replaced only when the record at
`settings.FIRMWARE_RECORD` says the broker placed it and its bytes still match
what was placed, so a user's own dump, or a flash image an emulator has
written to since, always stays. Nothing here fails a launch: a file that cannot
be placed is logged and reported, and the emulator's own missing-BIOS message
stays the signal.
"""

import glob
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Optional

from . import settings
from .emulators import retroarch_cores

if TYPE_CHECKING:
    from .emulators.base import Emulator

log = logging.getLogger(__name__)


class OutOfTime(OSError):
    """Placement ran past `settings.FIRMWARE_PLACE_BUDGET`, or activate gave up on it."""


_budget = threading.local()
"""The running `place` call's deadline and stop flag, in the thread it runs in."""

_record_lock = threading.Lock()
"""Held while the record is read, merged and written, so two calls never lose each other's entries."""


def _tick() -> None:
    """Stop a read once the placement budget is spent or activate has given up.

    Called before every chunk any read in here takes, so the budget covers
    hashing as well as copying. Outside `place` it does nothing.

    Raises:
        OutOfTime: When the deadline has passed or the stop flag is set.
    """
    deadline = getattr(_budget, "deadline", None)
    stop = getattr(_budget, "stop", None)
    if stop is not None and stop.is_set():
        raise OutOfTime("activate stopped waiting for the firmware")
    if deadline is not None and time.monotonic() > deadline:
        raise OutOfTime(f"out of time after {getattr(_budget, 'seconds', 0.0):.0f} s")


def _stopped() -> bool:
    """Whether activate has given up on this `place` call.

    Returns:
        True once the stop flag is set; a placement abandoned this way starts
        and renames nothing more, but still records a file that landed
        before it.
    """
    stop = getattr(_budget, "stop", None)
    return stop is not None and stop.is_set()

MAX_FILE_BYTES = 512 * 1024 * 1024
"""Largest firmware file copied, 512 MiB.

Every BIOS the placing emulators take is a few MiB at most. The cap only stops
a large upload (a console's system update, say) being copied on every launch.
"""

_MTIME_BACKDATE_NS = 10 * 1_000_000_000
"""How far before now a placed file is stamped when its source is newer than that.

The exit dump ships files stamped after the launch baseline less a few
seconds of slack (`saves._BASELINE_MTIME_SLACK`). A BIOS uploaded moments
before launch, or a library on a host whose clock runs ahead, would otherwise
land inside that window.
"""

_CHUNK = 1024 * 1024
_TMP_MARK = "broker-fw-"
_TMP_RE = re.compile(r"^\.(?P<name>.+)\." + _TMP_MARK + r"[a-z0-9_]{8}\.tmp$")
"""A temp file `_copy_in` or `_save_record` makes.

`.<name>.broker-fw-`, then `tempfile`'s 8 random characters, then `.tmp`.
"""
_TMP_STALE_S = 3600.0
"""Least seconds a temp file's inode has to sit unchanged before a later call sweeps it.

`_stale_after` raises it past twice the placement budget, so a copy a call
is still writing never qualifies, only the leftovers of a killed broker.
"""
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")

KNOWN_NAMES_SUPPLEMENT: dict[str, str] = {
    "924e392ed05558ffdb115408c263dccf": "scph1001.bin",
    "1e68c231d0896b7eadcad1d7d8e76129": "scph7001.bin",
}
"""md5 to file name for BIOS that core info has no hash for.

Each one matches RomM's `known_bios_files.json` (`psx:scph1001.bin`,
`psx:scph7001.bin`).
"""


@dataclass(frozen=True)
class Source:
    """One firmware file RomM sent, checked to lie under `ROM_ROOT`.

    Attributes:
        name: The uploaded file name, a single path component.
        path: Where the file is in this container, resolved.
        md5: RomM's md5 for it, lower case, or None.
        sha1: RomM's sha1 for it, lower case, or None.
        size: RomM's size for it in bytes, or None.
    """

    name: str
    path: Path
    md5: Optional[str]
    sha1: Optional[str]
    size: Optional[int]


@dataclass(frozen=True)
class Target:
    """Where an emulator wants a firmware file.

    Attributes:
        root: The emulator's directory, which the file may never leave.
        relpath: The file's path under `root`.
    """

    root: Path
    relpath: PurePosixPath

    @property
    def dest(self) -> Path:
        """The file's full path."""
        return self.root.joinpath(*self.relpath.parts)


def resolve_source(
    file_name: str, path: str, md5: Optional[str], sha1: Optional[str], size: Optional[int]
) -> Source:
    """Check one firmware entry from RomM and turn it into a `Source`.

    Args:
        file_name: The uploaded file name.
        path: The file's path in this container.
        md5: RomM's md5, or None.
        sha1: RomM's sha1, or None.
        size: RomM's size in bytes, or None.

    Returns:
        The source. A hash that is not hex of the right length is dropped.

    Raises:
        ValueError: When `file_name` is not a single path component, or `path`
            resolves outside `ROM_ROOT`.
    """
    if retroarch_cores.safe_dir_name(file_name) is None:
        raise ValueError(f"firmware file name {file_name!r} is not a single file name")
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"firmware path {path!r} cannot be resolved: {exc}") from exc
    rom_root = settings.rom_root()
    if not resolved.is_relative_to(rom_root):
        raise ValueError(f"firmware path {path!r} resolves outside {rom_root}")
    md5 = md5.lower() if md5 else None
    sha1 = sha1.lower() if sha1 else None
    return Source(
        name=file_name,
        path=resolved,
        md5=md5 if md5 and _MD5_RE.match(md5) else None,
        sha1=sha1 if sha1 and _SHA1_RE.match(sha1) else None,
        size=size,
    )


def known_names(source: Source) -> frozenset[str]:
    """The file names emulators know a firmware file by, from its hashes.

    Args:
        source: The firmware file.

    Returns:
        File names from core info, spelled as core info spells them (so
        mixed case, such as `BS-X.bin`), and from `KNOWN_NAMES_SUPPLEMENT`;
        none when neither knows the hashes. Compare them without regard to
        case.
    """
    names = retroarch_cores.firmware_names(source.md5, source.sha1)
    extra = KNOWN_NAMES_SUPPLEMENT.get((source.md5 or "").lower())
    return names | {extra} if extra else names


def _safe_relpath(relpath: PurePosixPath) -> bool:
    """Whether a target path stays under its root.

    Args:
        relpath: The path.

    Returns:
        False when it is empty, absolute, or holds a `.`, `..`, backslash or NUL.
    """
    raw = str(relpath)
    if not relpath.parts or relpath.is_absolute() or "\\" in raw or "\0" in raw:
        return False
    return all(part not in ("", ".", "..") for part in relpath.parts)


def _md5_of(path: Path) -> str:
    """The md5 of a file, read in chunks.

    Args:
        path: The file.

    Returns:
        Its md5, lower case.

    Raises:
        OSError: When the file cannot be read, or the budget runs out.
    """
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        while True:
            _tick()
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _open_source(source: Source) -> int:
    """Open a source for reading, re-checking it still lies under `ROM_ROOT`.

    `resolve_source` checked the path when the request came in; the file is
    read later, after the slot is restored. The file is opened first, without
    following a final symlink and without blocking on a FIFO, and only then is
    the file actually opened checked: a regular file, under `ROM_ROOT` by the
    kernel's own path for the descriptor (or, without `/proc`, by a resolved
    path checked to hold that same file). A path or a parent folder swapped
    in between is never read.

    Args:
        source: The firmware file.

    Returns:
        A read-only file descriptor.

    Raises:
        OSError: When the file cannot be opened, is not a regular file, or
            the descriptor's real path is no longer under `ROM_ROOT`.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(source.path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{source.path} is not a regular file")
        opened = _opened_path(fd, source.path)
        if not opened.is_relative_to(settings.rom_root()):
            raise OSError(f"{source.path} opened as {opened}, outside {settings.rom_root()}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _opened_path(fd: int, path: Path) -> Path:
    """Where an open descriptor really points.

    Args:
        fd: The descriptor.
        path: The path it was opened by, for a system without `/proc`.

    Returns:
        The kernel's path for `fd` from `/proc/self/fd`. Without `/proc`,
        `path` resolved, once the file there is checked to be the one `fd`
        has open.

    Raises:
        OSError: Without `/proc`, when the resolved path no longer holds the
            open file.
    """
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError as exc:
        log.warning("firmware: cannot read /proc/self/fd (%s); checking %s by its path instead", exc, path)
    real = Path(os.path.realpath(path))
    if not os.path.samestat(os.fstat(fd), os.stat(real)):
        raise OSError(f"{path} changed while it was opened")
    return real


def _source_md5(source: Source) -> str:
    """The md5 of a source's bytes, read through `_open_source`.

    Args:
        source: The firmware file.

    Returns:
        Its md5, lower case.

    Raises:
        OSError: When the file cannot be read, is over `MAX_FILE_BYTES`, or
            the budget runs out.
    """
    digest = hashlib.md5(usedforsecurity=False)
    read = 0
    with os.fdopen(_open_source(source), "rb") as fh:
        while True:
            _tick()
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            if read > MAX_FILE_BYTES:
                raise OSError(f"{source.path} is over the {MAX_FILE_BYTES} byte cap")
            digest.update(chunk)
    return digest.hexdigest()


def with_content_hash(source: Source) -> Source:
    """The source with its md5 read from disk, when RomM sent no hash at all.

    Without a hash nothing can recognise the file by its contents, so a
    renamed upload would keep its uploaded name. RomM hashes every file it
    scans, so this only reads files RomM could not hash.

    Args:
        source: The firmware file.

    Returns:
        The same source when RomM sent an md5 or sha1, else one carrying the
        md5 of the bytes on disk.

    Raises:
        OSError: When the file cannot be read.
    """
    if source.md5 or source.sha1:
        return source
    return replace(source, md5=_source_md5(source))


def _load_record() -> dict[str, dict[str, Any]]:
    """Read the record of files the broker placed.

    Returns:
        Target path to `{"md5", "size", "mtime_ns"}`. Empty when the record is
        missing or unreadable, which only makes the broker more careful: a file
        it does not know it placed is never replaced.
    """
    record = _read_record()
    return {} if record is None else record


def _read_record() -> Optional[dict[str, dict[str, Any]]]:
    """Read the record, telling a read that failed from one that found nothing usable.

    Returns:
        The record; empty when it is missing, not JSON or not an object
        (rewriting those loses nothing readable). None when reading it failed,
        so a write would replace entries that may be intact.
    """
    try:
        data = json.loads(settings.FIRMWARE_RECORD.read_text())
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except ValueError as exc:
        log.warning("firmware: ignoring unreadable record %s: %s", settings.FIRMWARE_RECORD, exc)
        return {}
    except OSError as exc:
        log.warning("firmware: could not read the record %s: %s", settings.FIRMWARE_RECORD, exc)
        return None
    if not isinstance(data, dict):
        log.warning("firmware: ignoring record %s, which is not a JSON object", settings.FIRMWARE_RECORD)
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def _stale_after() -> float:
    """How old a temp file has to be before it is swept.

    Returns:
        `_TMP_STALE_S`, or twice the placement budget plus a minute when that is longer.
    """
    return max(_TMP_STALE_S, 2 * settings.FIRMWARE_PLACE_BUDGET + 60)


def _sweep_temps(dest: Path) -> None:
    """Remove the temp files a killed broker left beside `dest`.

    Only names `_TMP_RE` matches for exactly this file are touched, only
    regular files, and only ones whose inode has not changed for
    `_TMP_STALE_S`, so a copy another call is still writing is never pulled
    out from under it. The change time is used, not the mtime: a finished
    copy is back-dated to its source's mtime before the rename, and the
    change time cannot be set back.

    Args:
        dest: The file the temp files were for.
    """
    cutoff = time.time() - _stale_after()
    for stale in dest.parent.glob(f".{glob.escape(dest.name)}.{_TMP_MARK}*.tmp"):
        match = _TMP_RE.match(stale.name)
        if not (match and match["name"] == dest.name):
            continue
        try:
            st = os.stat(stale, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_ctime < cutoff:
            stale.unlink(missing_ok=True)


def _save_record(changes: dict[str, dict[str, Any]]) -> bool:
    """Merge entries into the record on disk, through a temp file and a rename.

    The record is read again under `_record_lock` and only `changes` are
    written over it, so a call activate gave up on that lands a file late
    still records it without dropping what a newer call recorded meanwhile.
    When the record cannot be read nothing is written, so a passing read
    error never wipes it. A failure is logged. A temp file a killed broker
    left behind is swept first.

    Args:
        changes: Target path to entry, for the targets this call placed or restamped.

    Returns:
        True when the record on disk now holds `changes` (less any entry
        dropped because its file has changed since).
    """
    path = settings.FIRMWARE_RECORD
    with _record_lock:
        record = _read_record()
        if record is None:
            log.error("firmware: not updating the record %s, it could not be read", path)
            return False
        for key, entry in changes.items():
            # Only an entry that still describes the file there is recorded:
            # a newer call may have replaced it since this one stamped it.
            try:
                st = os.stat(key, follow_symlinks=False)
            except OSError as exc:
                log.warning("firmware: not recording %s, which is gone: %s", key, exc)
                continue
            if st.st_size == entry.get("size") and st.st_mtime_ns == entry.get("mtime_ns"):
                record[key] = entry
            else:
                log.warning("firmware: not recording %s, which has changed since it was placed", key)
        return _write_record(path, record)


def _write_record(path: Path, record: dict[str, dict[str, Any]]) -> bool:
    """Write the whole record through a temp file and a rename. A failure is logged.

    Args:
        path: The record file.
        record: The record.

    Returns:
        True when it was written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _sweep_temps(path)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.{_TMP_MARK}", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    except OSError as exc:
        log.error("firmware: could not write the record %s: %s", path, exc)
        return False
    return True


def _copy_in(source: Source, dest: Path) -> Optional[tuple[str, os.stat_result]]:
    """Copy a source over `dest` through a temp file in the same directory.

    The copy keeps the source's mtime, as `cp -p` does, or
    `_MTIME_BACKDATE_NS` before now when the source is newer. A fresh mtime
    would put the file inside the exit dump's launch window, and a BIOS placed
    in a save subtree (Flycast's DATA_DIR) would ship to RomM as the player's
    save. A temp file a killed broker left behind is swept first.

    Args:
        source: The firmware file.
        dest: Where it goes. Its directory exists.

    Returns:
        The md5 of what was written and the file's stat, taken before the
        rename so nothing can fail between the file landing and its record
        entry; None when the copy did not match the expected hash and was
        thrown away.

    Raises:
        OSError: When the copy or the rename fails, or the budget runs out
            or activate has given up (`OutOfTime`); the temp file is removed
            and `dest` is untouched.
    """
    if _stopped():
        raise OutOfTime("activate stopped waiting for the firmware")
    _sweep_temps(dest)
    src_fd = _open_source(source)
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.{_TMP_MARK}", suffix=".tmp", dir=dest.parent)
    except BaseException:
        os.close(src_fd)
        raise
    try:
        digest = hashlib.md5(usedforsecurity=False)
        sha1 = hashlib.sha1(usedforsecurity=False) if source.sha1 and not source.md5 else None
        copied = 0
        with os.fdopen(fd, "wb") as out, os.fdopen(src_fd, "rb") as src:
            src_fd = -1
            src_stat = os.fstat(src.fileno())
            while True:
                _tick()
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_FILE_BYTES:
                    log.error(
                        "firmware: %s grew past the %d byte cap while copying; not placed",
                        source.path,
                        MAX_FILE_BYTES,
                    )
                    return None
                digest.update(chunk)
                if sha1 is not None:
                    sha1.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        written = digest.hexdigest()
        if source.md5 and written != source.md5:
            log.error(
                "firmware: %s reads as md5 %s, not %s; not placed", source.path, written, source.md5
            )
            return None
        if sha1 is not None and sha1.hexdigest() != source.sha1:
            log.error(
                "firmware: %s reads as sha1 %s but RomM has %s; not placed",
                source.path,
                sha1.hexdigest(),
                source.sha1,
            )
            return None
        os.chmod(tmp, 0o644)
        mtime_ns = min(src_stat.st_mtime_ns, time.time_ns() - _MTIME_BACKDATE_NS)
        os.utime(tmp, ns=(src_stat.st_atime_ns, mtime_ns))
        landed = os.stat(tmp)
        if _stopped():
            raise OutOfTime("activate stopped waiting for the firmware")
        os.replace(tmp, dest)
        return written, landed
    finally:
        if src_fd >= 0:
            os.close(src_fd)
        Path(tmp).unlink(missing_ok=True)


def _stamp(md5: str, st: os.stat_result, sha1: Optional[str] = None) -> dict[str, Any]:
    """A record entry for a file the broker placed.

    Args:
        md5: The placed bytes' md5.
        st: The placed file's stat.
        sha1: The placed bytes' sha1, when it was checked.

    Returns:
        The entry.
    """
    entry: dict[str, Any] = {"md5": md5, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if sha1:
        entry["sha1"] = sha1
    return entry


def _same_bytes(entry: dict[str, Any], source: Source, want: list[str]) -> bool:
    """Whether a record entry's hash is the source's, reading the source only when it must.

    For a source RomM sent only a sha1 for, a recorded sha1 settles it
    without a read, and the recorded md5 then fills `want`.

    Args:
        entry: The record entry.
        source: The firmware file.
        want: The one-item md5 cache `_place_one` shares.

    Returns:
        True when the entry's md5, or for a sha1-only source the entry's
        sha1, is the source's.
    """
    if not source.md5 and source.sha1 and entry.get("sha1") and isinstance(entry.get("md5"), str):
        if entry["sha1"] != source.sha1:
            return False
        if not want:
            want.append(entry["md5"])
        return True
    return bool(entry.get("md5") == _cached_md5(source, want))


def _place_one(
    source: Source,
    src_size: int,
    target: Target,
    record: dict[str, dict[str, Any]],
    want: list[str],
) -> str:
    """Bring one target in line with its source.

    Args:
        source: The firmware file.
        src_size: Its size on disk.
        target: Where it goes.
        record: The ownership record, updated in place.
        want: A one-item cache for the source's md5, filled on first need.

    Returns:
        "placed", "unchanged", "kept" or "failed". A target in an emulator
        folder inside the ROM library is never written: "kept" when a file is
        already there (a deliberate no-copy setup), "failed" when none is.

    Raises:
        OSError: When the filesystem fails along the way.
    """
    if not _safe_relpath(target.relpath):
        log.error("firmware: refusing target %r under %s", str(target.relpath), target.root)
        return "failed"
    dest = target.dest
    if os.path.isfile(dest) and os.path.samefile(dest, source.path):
        # The emulator already reads the library copy itself.
        return "unchanged"
    if Path(os.path.realpath(target.root)).is_relative_to(settings.rom_root()) or Path(
        os.path.realpath(dest.parent)
    ).is_relative_to(settings.rom_root()):
        # The emulator folder, or the subfolder this file goes in, is the ROM
        # library itself: a no-copy setup, never written to.
        if os.path.isfile(dest):
            log.info("firmware: kept %s as it is; the broker never writes into the ROM library", dest)
            return "kept"
        log.warning(
            "firmware: %s is missing and inside the ROM library %s, which the broker never writes to",
            dest,
            settings.rom_root(),
        )
        return "failed"
    target.root.mkdir(parents=True, exist_ok=True)
    real_root = Path(os.path.realpath(target.root))
    # Checked before mkdir so no folder is made outside the root, and again
    # after it in case a folder was swapped for a link in between.
    if not Path(os.path.realpath(dest.parent)).is_relative_to(real_root):
        log.error("firmware: refusing %s, its folder leads outside %s", dest, target.root)
        return "failed"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not Path(os.path.realpath(dest.parent)).is_relative_to(real_root):
        log.error("firmware: refusing %s, its folder leads outside %s", dest, target.root)
        return "failed"

    # A killed broker's leftover is cleared even when the target needs no copy.
    _sweep_temps(dest)

    def source_md5() -> str:
        return _cached_md5(source, want)

    key = str(dest)
    entry = record.get(key)
    if os.path.lexists(dest):
        st = os.stat(dest, follow_symlinks=False)
        regular = not dest.is_symlink() and dest.is_file()
        if not dest.is_file():
            log.warning("firmware: kept %s, which is not a regular file", dest)
            return "kept"
        if (
            regular
            and entry is not None
            and entry.get("size") == st.st_size
            and entry.get("mtime_ns") == st.st_mtime_ns
            and _same_bytes(entry, source, want)
        ):
            return "unchanged"
        have: Optional[str] = None
        if dest.stat().st_size == src_size:
            have = _md5_of(dest)
            if have == source_md5():
                if regular and entry is not None and entry.get("md5") == have:
                    record[key] = _stamp(have, dest.stat(), entry.get("sha1"))
                return "unchanged"
        if not regular or entry is None:
            log.info(
                "firmware: kept %s, which differs from RomM's %s and was not placed by the broker",
                dest,
                source.name,
            )
            return "kept"
        if entry.get("size") != st.st_size or (have or _md5_of(dest)) != entry.get("md5"):
            log.info("firmware: kept %s, which the broker placed but something has changed since", dest)
            return "kept"
    copied = _copy_in(source, dest)
    if copied is None:
        return "failed"
    written, landed = copied
    # The bytes just written are the source's now, whatever an earlier read saw.
    want[:] = [written]
    record[key] = _stamp(written, landed, source.sha1 if not source.md5 else None)
    log.info("firmware: placed %s as %s", source.name, dest)
    return "placed"


def _copyable_size(source: Source) -> Optional[int]:
    """A source's size on disk, when it is a file small enough to copy.

    Args:
        source: The firmware file.

    Returns:
        Its size in bytes, or None when it is not a regular file or is over
        `MAX_FILE_BYTES`.
    """
    try:
        if not source.path.is_file():
            return None
        size = source.path.stat().st_size
    except OSError:
        return None
    return size if size <= MAX_FILE_BYTES else None


def _cached_md5(source: Source, want: list[str]) -> str:
    """A source's md5, from RomM, the one-item cache, or a read that fills it.

    Args:
        source: The firmware file.
        want: The cache `_place_one` shares.

    Returns:
        The md5.

    Raises:
        OSError: When the file has to be read and cannot be.
    """
    if not want:
        want.append(source.md5 or _source_md5(source))
    return want[0]


def places_firmware(emulator: "Emulator") -> bool:
    """Whether an emulator places any firmware at all.

    Args:
        emulator: The emulator.

    Returns:
        False when it keeps the base class's `firmware_targets`, which wants
        nothing, so its sources are never read.
    """
    from .emulators.base import Emulator

    return type(emulator).firmware_targets is not Emulator.firmware_targets


def _plan(
    emulator: "Emulator", sources: Sequence[Source], report: dict[str, list[str]]
) -> list[tuple[Source, list[Target]]]:
    """Ask the emulator where each source goes, in the order they are placed.

    A source RomM sent no hash for is hashed first, so it can be named by its
    contents, but only for an emulator that places firmware. A source whose
    hash names one of its targets goes first, smaller files before larger
    ones within each of those two groups, and a stable order settles the
    rest, so a file core info knows by that name always wins the target over
    one that only shares the name, a large file cannot spend the budget
    before the small ones of its group, and two files never trade places
    between launches.

    Args:
        emulator: The emulator about to launch.
        sources: The firmware RomM sent.
        report: The report; a source that fails here is added to `failed`.

    Returns:
        Each source the emulator wants, with its targets.
    """
    if not places_firmware(emulator):
        return []
    planned: list[tuple[bool, Source, list[Target]]] = []
    # Smallest first here too: hashing a hashless source is a full read, and
    # one large file must not spend the budget before the small ones.
    for source in sorted(sources, key=_sort_size):
        try:
            if not (source.md5 or source.sha1) and _copyable_size(source) is not None:
                source = with_content_hash(source)
            targets = emulator.firmware_targets(source)
            known = {name.lower() for name in known_names(source)}
            recognised = any(target.relpath.name.lower() in known for target in targets)
        except OutOfTime as exc:
            log.error("firmware: %s not placed: %s", source.name, exc)
            report["failed"].append(source.name)
            continue
        except Exception:  # noqa: BLE001 - one bad file or hook must never stop the launch
            log.exception("firmware: working out where %s goes failed", source.name)
            report["failed"].append(source.name)
            continue
        if not targets:
            log.debug("firmware: %s wants no copy of %s", emulator.name, source.name)
            continue
        planned.append((recognised, source, targets))
    planned.sort(key=lambda item: (not item[0], _sort_size(item[1]), item[1].name, str(item[1].path)))
    return [(source, targets) for _, source, targets in planned]


def _sort_size(source: Source) -> int:
    """A source's size for ordering, so a large file cannot use up the budget before small ones.

    Args:
        source: The firmware file.

    Returns:
        Its size on disk, or one past the cap when it cannot be copied.
    """
    size = _copyable_size(source)
    return MAX_FILE_BYTES + 1 if size is None else size


def empty_report() -> dict[str, list[str]]:
    """A report with nothing in it.

    Returns:
        `placed`, `unchanged` and `kept` (target paths), `skipped` (source
        file names another file with different bytes beat to every target)
        and `failed` (source file names), all empty. A source whose bytes are
        already placed at its target this launch is in no list: the file it
        names is there.
    """
    return {"placed": [], "unchanged": [], "kept": [], "skipped": [], "failed": []}


def place(
    emulator: "Emulator",
    sources: Sequence[Source],
    stop: Optional[threading.Event] = None,
    budget: Optional[float] = None,
) -> dict[str, list[str]]:
    """Place every source where the emulator wants it, within the time budget.

    Every read in here, hashing included, stops once
    `settings.FIRMWARE_PLACE_BUDGET` seconds have passed or `stop` is set; a
    file not finished by then is listed as failed and the target keeps what
    it had. A target that needs no read (a source RomM sent a hash for,
    already in place by size and time) is still settled after the budget is
    spent. Once `stop` is set no further target is started and nothing more
    is renamed into place; a file that landed just before is still recorded,
    merged into the record on disk, so a call activate has given up on never
    drops what a newer call recorded.

    Args:
        emulator: The emulator about to launch.
        sources: The firmware RomM sent.
        stop: Set by activate when it stops waiting.
        budget: Seconds for this call; `settings.FIRMWARE_PLACE_BUDGET` when
            None. Activate passes what is left after checking the list.

    Returns:
        The report from `empty_report`, filled in. A source counts as failed
        once, however many of its targets failed.
    """
    report = empty_report()
    if not places_firmware(emulator):
        return report
    _budget.seconds = settings.FIRMWARE_PLACE_BUDGET if budget is None else max(0.0, budget)
    _budget.deadline = time.monotonic() + _budget.seconds
    _budget.stop = stop
    try:
        _place_all(emulator, sources, report)
    finally:
        _budget.deadline = None
        _budget.stop = None
        _budget.seconds = 0.0
    counts = (
        len(report["placed"]),
        len(report["unchanged"]),
        len(report["kept"]),
        len(report["skipped"]),
        len(report["failed"]),
    )
    # Nothing to say for a platform with no firmware; said at debug, not on every launch.
    (log.info if any(counts) else log.debug)(
        "firmware: %d placed, %d unchanged, %d kept, %d skipped, %d failed", *counts
    )
    return report


def _place_all(emulator: "Emulator", sources: Sequence[Source], report: dict[str, list[str]]) -> None:
    """The body of `place`, run with the budget set.

    Args:
        emulator: The emulator about to launch.
        sources: The firmware RomM sent.
        report: The report, filled in place.
    """
    record = _load_record()
    loaded = dict(record)
    # What landed at each target this launch, hashed only if a later source needs to compare.
    seen: dict[str, tuple[Source, list[str]]] = {}
    planned = _plan(emulator, sources, report)
    for source, targets in planned:
        src_size = _copyable_size(source)
        if src_size is None:
            log.error(
                "firmware: %s at %s is not a file under the size cap; not placed", source.name, source.path
            )
            report["failed"].append(source.name)
            continue
        want: list[str] = []
        failed = False
        beaten = False
        landed = False
        for target in targets:
            if _stopped():
                # Activate has launched without the firmware; touch nothing more.
                log.error("firmware: %s not placed: activate stopped waiting", source.name)
                failed = True
                continue
            key = "?"
            try:
                key = str(target.dest)
                if key in seen:
                    if _cached_md5(*seen[key]) != _cached_md5(source, want):
                        log.warning(
                            "firmware: %s skipped, another file already took %s this launch", source.name, key
                        )
                        beaten = True
                    else:
                        # The same bytes are already there: this target is done.
                        landed = True
                    continue
                outcome = _place_one(source, src_size, target, record, want)
            except OutOfTime as exc:
                log.error("firmware: %s not placed at %s: %s", source.name, key, exc)
                failed = True
                continue
            except Exception:  # noqa: BLE001 - one bad file or hook must never stop the launch
                log.exception("firmware: placing %s at %s failed", source.name, key)
                failed = True
                continue
            if outcome == "failed":
                failed = True
                continue
            if outcome in ("placed", "unchanged"):
                seen[key] = (source, want)
            landed = True
            if key not in report[outcome]:
                report[outcome].append(key)
            if outcome == "placed":
                # Recorded as it lands, even after activate gave up: a file on
                # disk with no entry would look like the user's from then on.
                if _save_record({key: record[key]}):
                    loaded[key] = record[key]
        if failed:
            report["failed"].append(source.name)
        elif beaten and not landed:
            report["skipped"].append(source.name)
    changes = {key: entry for key, entry in record.items() if loaded.get(key) != entry}
    if changes:
        _save_record(changes)
    # Two sources sharing a file name count as one failed file.
    report["failed"] = list(dict.fromkeys(report["failed"]))
    # A target one source kept and another found already right is right.
    done = set(report["placed"]) | set(report["unchanged"])
    report["kept"] = [key for key in report["kept"] if key not in done]
