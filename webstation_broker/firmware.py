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
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Optional

from . import settings
from .emulators import retroarch_cores

if TYPE_CHECKING:
    from .emulators.base import Emulator

log = logging.getLogger(__name__)

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
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")

KNOWN_NAMES_SUPPLEMENT: dict[str, str] = {
    "924e392ed05558ffdb115408c263dccf": "scph1001.bin",
    "1e68c231d0896b7eadcad1d7d8e76129": "scph7001.bin",
}
"""md5 to file name for BIOS that core info has no hash for.

Each one is in Argosy's table and agrees with RomM's `known_bios_files.json`.
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
    except (OSError, ValueError) as exc:
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
        OSError: When the file cannot be read.
    """
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _load_record() -> dict[str, dict[str, Any]]:
    """Read the record of files the broker placed.

    Returns:
        Target path to `{"md5", "size", "mtime_ns"}`. Empty when the record is
        missing or unreadable, which only makes the broker more careful: a file
        it does not know it placed is never replaced.
    """
    try:
        data = json.loads(settings.FIRMWARE_RECORD.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("firmware: ignoring unreadable record %s: %s", settings.FIRMWARE_RECORD, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("firmware: ignoring record %s, which is not a JSON object", settings.FIRMWARE_RECORD)
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def _save_record(record: dict[str, dict[str, Any]]) -> None:
    """Write the record through a temp file and a rename. A failure is logged.

    Args:
        record: The record.
    """
    path = settings.FIRMWARE_RECORD
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    except OSError as exc:
        log.error("firmware: could not write the record %s: %s", path, exc)


def _copy_in(source: Source, dest: Path) -> Optional[str]:
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
        The md5 of what was written, or None when the copy did not match
        RomM's md5 and was thrown away.

    Raises:
        OSError: When the copy or the rename fails; the temp file is removed.
    """
    for stale in dest.parent.glob(f".{glob.escape(dest.name)}.*.tmp"):
        stale.unlink(missing_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    try:
        digest = hashlib.md5(usedforsecurity=False)
        with os.fdopen(fd, "wb") as out, source.path.open("rb") as src:
            src_stat = os.fstat(src.fileno())
            while chunk := src.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        written = digest.hexdigest()
        if source.md5 and written != source.md5:
            log.error(
                "firmware: %s reads as md5 %s but RomM has %s; not placed", source.path, written, source.md5
            )
            return None
        os.chmod(tmp, 0o644)
        mtime_ns = min(src_stat.st_mtime_ns, time.time_ns() - _MTIME_BACKDATE_NS)
        os.utime(tmp, ns=(src_stat.st_atime_ns, mtime_ns))
        os.replace(tmp, dest)
        return written
    finally:
        Path(tmp).unlink(missing_ok=True)


def _stamp(md5: str, dest: Path) -> dict[str, Any]:
    """A record entry for a file the broker placed.

    Args:
        md5: The placed bytes' md5.
        dest: The placed file.

    Returns:
        The entry.
    """
    st = dest.stat()
    return {"md5": md5, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _place_one(
    source: Source, src_size: int, target: Target, record: dict[str, dict[str, Any]], want: list[str]
) -> str:
    """Bring one target in line with its source.

    Args:
        source: The firmware file.
        src_size: Its size on disk.
        target: Where it goes.
        record: The ownership record, updated in place.
        want: A one-item cache for the source's md5, filled on first need.

    Returns:
        "placed", "unchanged", "kept" or "failed".

    Raises:
        OSError: When the filesystem fails along the way.
    """
    if not _safe_relpath(target.relpath):
        log.error("firmware: refusing target %r under %s", str(target.relpath), target.root)
        return "failed"
    dest = target.dest
    target.root.mkdir(parents=True, exist_ok=True)
    real_root = Path(os.path.realpath(target.root))
    if not Path(os.path.realpath(dest.parent)).is_relative_to(real_root):
        log.error("firmware: refusing %s, its folder leads outside %s", dest, target.root)
        return "failed"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not Path(os.path.realpath(dest.parent)).is_relative_to(real_root):
        log.error("firmware: refusing %s, its folder leads outside %s", dest, target.root)
        return "failed"

    def source_md5() -> str:
        if not want:
            want.append(source.md5 or _md5_of(source.path))
        return want[0]

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
            and entry.get("md5") == source_md5()
        ):
            return "unchanged"
        have: Optional[str] = None
        if dest.stat().st_size == src_size:
            have = _md5_of(dest)
            if have == source_md5():
                if regular and entry is not None and entry.get("md5") == have:
                    record[key] = _stamp(have, dest)
                return "unchanged"
        owned = regular and entry is not None and entry.get("size") == st.st_size
        if not owned or (have or _md5_of(dest)) != entry.get("md5"):
            log.warning(
                "firmware: kept %s, which differs from RomM's %s and was not placed by the broker",
                dest,
                source.name,
            )
            return "kept"
    written = _copy_in(source, dest)
    if written is None:
        return "failed"
    record[key] = _stamp(written, dest)
    log.info("firmware: placed %s as %s", source.name, dest)
    return "placed"


def empty_report() -> dict[str, list[str]]:
    """A report with nothing in it.

    Returns:
        `placed`, `unchanged` and `kept` (target paths) and `failed` (source
        file names), all empty.
    """
    return {"placed": [], "unchanged": [], "kept": [], "failed": []}


def place(emulator: "Emulator", sources: Sequence[Source]) -> dict[str, list[str]]:
    """Place every source where the emulator wants it.

    Args:
        emulator: The emulator about to launch.
        sources: The firmware RomM sent.

    Returns:
        The report from `empty_report`, filled in. A source counts as failed
        once, however many of its targets failed.
    """
    report = empty_report()
    record = _load_record()
    before = json.dumps(record, sort_keys=True)
    seen: set[str] = set()
    # A stable order, so two files wanting one target settle the same way
    # every launch instead of replacing each other.
    for source in sorted(sources, key=lambda s: (s.name, str(s.path))):
        failed = False
        try:
            targets = emulator.firmware_targets(source)
            if not targets:
                log.debug("firmware: %s wants no copy of %s", emulator.name, source.name)
                continue
            if not source.path.is_file():
                log.error("firmware: %s is not a file at %s; not placed", source.name, source.path)
                report["failed"].append(source.name)
                continue
            src_size = source.path.stat().st_size
            if src_size > MAX_FILE_BYTES:
                log.error(
                    "firmware: %s is %d bytes, over the %d byte cap; not placed",
                    source.name,
                    src_size,
                    MAX_FILE_BYTES,
                )
                report["failed"].append(source.name)
                continue
            want: list[str] = []
            for target in targets:
                key = str(target.dest)
                if key in seen:
                    log.info("firmware: %s was already placed this launch, skipping %s", key, source.name)
                    continue
                seen.add(key)
                outcome = _place_one(source, src_size, target, record, want)
                if outcome == "failed":
                    failed = True
                else:
                    report[outcome].append(key)
        except Exception:  # noqa: BLE001 - one bad file or hook must never stop the launch
            log.exception("firmware: placing %s failed", source.name)
            failed = True
        if failed:
            report["failed"].append(source.name)
    if json.dumps(record, sort_keys=True) != before:
        _save_record(record)
    log.info(
        "firmware: %d placed, %d unchanged, %d kept, %d failed",
        len(report["placed"]),
        len(report["unchanged"]),
        len(report["kept"]),
        len(report["failed"]),
    )
    return report
