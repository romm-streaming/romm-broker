"""Save data in and out of the emulator's save directories.

Activate restores a zip archive into the emulator's save directories; exit
zips every save file modified since launch.
"""

import calendar
import io
import json
import logging
import lzma
import os
import secrets
import stat
import time
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Optional

from . import settings

log = logging.getLogger(__name__)

SAVE_FILE_MAX_BYTES = int(os.environ.get("SAVE_FILE_MAX_BYTES", str(256 * 1024 * 1024)))
"""Env-tunable guard against runaway dumps, from `SAVE_FILE_MAX_BYTES` (default 256 MiB)."""
MANIFEST_NAME = ".broker-manifest.json"
"""Index the broker adds to a dump archive, labelling each member for the parent.

Dot-prefixed so `_iter_save_files` skips it if it ever lands in a save tree,
and so a restore can tell the broker's own index from real save data.
"""
MANIFEST_VERSION = 1
"""Schema version of the archive manifest, for a parent reading old archives."""
IMPORT_PREFIX = ".import/"
"""Archive prefix that marks a member as a declared import rather than a restored dump.

The broker never dumps a dot-prefixed path, so no v1 archive can carry one. A
member under it is placed by the emulator's own import rules, not by path.
"""
MANIFEST_MAX_BYTES = 1024 * 1024
"""Largest manifest a restore will parse, and only when the archive holds imports.

A v1 dump listing ten thousand files can pass this, which is why a v1-only
archive never has its manifest parsed at all.
"""
ZIP_READ_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    RuntimeError,
    NotImplementedError,
    EOFError,
    zlib.error,
    lzma.LZMAError,
    zipfile.BadZipFile,
)
"""What reading a zip member's data can raise, under every compression method `zipfile` supports."""
_SAVE_MTIME_SLACK = 2.0
"""Seconds of slack on the newer-file guard.

Zip stores mtimes at 2 s DOS resolution; the slack keeps the guard from
skipping files over rounding alone.
"""
_BASELINE_MTIME_SLACK = 2.0
"""Seconds of slack on the launch baseline when picking out changed files.

The baseline is a `time.time()` reading, but not every filesystem a save tree
can sit on records mtimes that finely: FAT and exFAT round down to 2 s, and
several network filesystems to 1 s. A save written seconds after launch can
therefore carry an mtime stamped just before the baseline, and comparing
against the bare value drops it from the dump and loses the player their save.
The slack costs at worst an unchanged file riding along, since the restore
that runs before the baseline is taken stamps its files with the archive's own
(older) mtimes.
"""
_ZIP_MIN_DATE = (1980, 1, 1, 0, 0, 0)
"""Earliest timestamp a zip entry can carry: DOS dates start in 1980."""
_ZIP_MAX_DATE = (2107, 12, 31, 23, 59, 58)
"""Latest timestamp a zip entry can carry: the DOS year field stops in 2107."""


def _iter_save_files(root: Path, subtrees: tuple[str, ...]) -> list[Path]:
    """List every regular file under the allowed subtrees.

    Sorted so identical content zips to identical bytes. Dot-prefixed
    components are staging or tmp entries and never ship, and symlinks are
    skipped.

    Args:
        root: The emulator's save data root.
        subtrees: Subdirectory names under `root` that hold save data.

    Returns:
        The files found, in sorted order.
    """
    files: list[Path] = []
    for sub in subtrees:
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(base)
            if any(part.startswith(".") for part in rel.parts):
                continue
            # shadPS4 writes sce_sys/corrupted while a save is mounted
            # read-write and removes it on unmount; shipping it would make
            # the next mount treat the save as corrupt.
            if p.name == "corrupted" and p.parent.name == "sce_sys":
                continue
            files.append(p)
    return files


def _read_file_stable(p: Path, retries: int = 4, settle: float = 0.5) -> Optional[tuple[bytes, float]]:
    """Read `p` only when size and mtime match before and after the read.

    This is what keeps a file the emulator is mid-writing from ever being
    shipped torn.

    Args:
        p: The file to read.
        retries: How many reads to attempt before giving up.
        settle: Seconds to wait between attempts.

    Returns:
        The file contents and its mtime, or None when the file could not be
        read or was still changing after every attempt (both are logged).
    """
    for attempt in range(retries):
        try:
            st_before = p.stat()
            data = p.read_bytes()
            st_after = p.stat()
        except OSError as exc:
            log.warning("saves: could not read %s: %s", p, exc)
            return None
        if (st_before.st_size, st_before.st_mtime_ns) == (
            st_after.st_size,
            st_after.st_mtime_ns,
        ):
            return data, st_after.st_mtime
        if attempt < retries - 1:
            time.sleep(settle)
    log.warning("saves: %s still being written, skipped", p)
    return None


def _zip_date_time(mtime: float, what: str) -> tuple[int, int, int, int, int, int]:
    """Convert an mtime into a date tuple a `ZipInfo` will accept.

    UTC on both sides, so a timezone difference between the dump and a later
    restore never shifts a member past the newer-file guard. A timestamp a zip
    entry cannot represent is clamped instead of raised: save files reach the
    tree with an epoch mtime (copied or unpacked with one preserved) or a
    corrupted one often enough, and a `ZipInfo` refusing one of those would
    abort the dump for every other save in the session too.

    Args:
        mtime: The file's modification time, as a Unix timestamp.
        what: The member the timestamp belongs to, for the log line.

    Returns:
        A `(year, month, day, hour, minute, second)` tuple inside the range
        zip can store.
    """
    try:
        parts = time.gmtime(mtime)[:6]
    except (OSError, OverflowError, ValueError) as exc:
        log.warning("saves: unusable mtime %r on %s (%s), clamping it", mtime, what, exc)
        return _ZIP_MIN_DATE
    if parts[0] < _ZIP_MIN_DATE[0]:
        log.warning("saves: mtime %r on %s predates 1980, clamping it", mtime, what)
        return _ZIP_MIN_DATE
    if parts[0] > _ZIP_MAX_DATE[0]:
        log.warning("saves: mtime %r on %s is past 2107, clamping it", mtime, what)
        return _ZIP_MAX_DATE
    return parts


def _finish_dump(report: dict[str, Any], changed_skipped: list[str]) -> dict[str, Any]:
    """Fold a dump's skipped files into its success or failure, and log them.

    A skip means a save the player made is not in the archive. The exit path
    decides what to tell RomM from `error` alone, so a dump where every file
    known to have changed was skipped has to set it: leaving it clear reports
    the session as a clean no-op, and RomM files it as saved and moves on. A
    partial skip keeps `error` clear, since failing the dump there would throw
    away the saves that did come out intact; it is logged and left in
    `skipped`/`skipped_files` for the report instead.

    A file whose `stat()` failed is skipped without ever being weighed against
    the baseline, so there is no evidence it changed this session at all.
    Counting one of those as a failed dump would turn a stat blip on some
    untouched file into a failed exit for a session that saved nothing, so it
    is reported and logged where it happens and never named here. That also
    keeps a caller that has already settled on some other `error`, such as the
    size limit, from picking up a skipped-file line that does not explain it.

    Args:
        report: The dump report, mutated in place.
        changed_skipped: Root-relative paths of the skips that were confirmed
            modified this session, the only ones a failed dump may rest on.

    Returns:
        The same report.
    """
    if not report["skipped"]:
        return report
    if report["files"]:
        log.error(
            "saves: dump is incomplete, %d file(s) could not be read: %s",
            report["skipped"],
            ", ".join(report["skipped_files"]),
        )
    elif changed_skipped and report["error"] is None:
        report["error"] = (
            f"none of the {len(changed_skipped)} changed save file(s) could be read: "
            f"{', '.join(changed_skipped)}"
        )
        log.error("saves: %s", report["error"])
    return report


def build_save_archive(
    root: Path,
    subtrees: tuple[str, ...],
    baseline: float,
    identity: Optional[dict[str, Any]] = None,
    classify: Optional[Callable[[str], str]] = None,
    *,
    always_include: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Zip every save file modified since `baseline` (the launch timestamp).

    Member paths are relative to `root` and mtimes are stored in UTC on both
    sides, so a timezone difference between the dump and a later restore never
    shifts them past the newer-file guard.

    When `identity` is given the archive also carries `MANIFEST_NAME`, which
    labels each member and names the session it came from. Every emulator lays
    its save directories out differently, so the manifest is what lets the
    parent sort states from saves without a table of those layouts.

    Args:
        root: The emulator's save data root.
        subtrees: Subdirectory names under `root` that hold save data.
        baseline: Unix timestamp; files with an mtime at or after it, less
            `_BASELINE_MTIME_SLACK`, are included.
        identity: Session identity to record in the manifest, or None to leave
            the manifest out entirely.
        classify: Maps a member path to its kind, usually
            `Emulator.save_file_kind`; members go in unlabelled without it.
        always_include: Paths relative to `root` that ship whatever their
            mtime: the files this session's declared imports placed. The walk
            still decides what exists, so a placed file that was deleted or
            set aside is simply absent.

    Returns:
        A report dict of the shape
        `{"files": [{"path", "size", "mtime"}...], "skipped": n,
        "skipped_files": [path...], "total_bytes": n, "zip_bytes": bytes | None,
        "error": str | None}`, with every path in `files` and `skipped_files`
        relative to `root`. `zip_bytes` is None when nothing changed or on
        error; `error` is set when the root is missing, the changed files exceed
        `SAVE_FILE_MAX_BYTES`, every file confirmed changed had to be skipped,
        or every save-file candidate failed to stat — each of the latter two
        would otherwise read to the caller exactly like a session that saved
        nothing.
    """
    report: dict = {
        "files": [],
        "skipped": 0,
        "skipped_files": [],
        "total_bytes": 0,
        "zip_bytes": None,
        "error": None,
    }
    if not root.is_dir():
        report["error"] = f"save data root missing: {root}"
        return report

    changed: list[Path] = []
    total = 0
    changed_skipped: list[str] = []
    candidates = 0
    cutoff = baseline - _BASELINE_MTIME_SLACK
    for p in _iter_save_files(root, subtrees):
        candidates += 1
        rel = p.relative_to(root).as_posix()
        try:
            st = p.stat()
        except OSError as exc:
            # Removed mid-walk or unreadable: it is a save that will not be in
            # the dump, so it never goes by unrecorded. Its mtime is unknown,
            # so it is not counted as a changed file: see `_finish_dump`.
            log.warning("saves: could not stat %s, leaving it out of the dump: %s", p, exc)
            report["skipped"] += 1
            report["skipped_files"].append(rel)
            continue
        if st.st_mtime >= cutoff or rel in always_include:
            changed.append(p)
            total += st.st_size
    if not changed:
        if candidates and report["skipped"] == candidates:
            # Every candidate failed to stat: unlike one blip on an untouched
            # file, this means nothing was actually weighed against the
            # baseline, so there is no evidence behind "nothing changed" —
            # reporting a clean no-op here could ship a session's saves as
            # lost without anyone noticing.
            report["error"] = (
                f"could not stat any of the {candidates} save file(s) under {root}, unable to "
                "tell whether anything changed"
            )
            log.error("saves: %s", report["error"])
            return report
        return _finish_dump(report, changed_skipped)
    if total > SAVE_FILE_MAX_BYTES:
        report["error"] = f"changed saves exceed size limit ({total} bytes)"
        return _finish_dump(report, changed_skipped)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in changed:
            rel = p.relative_to(root).as_posix()
            result = _read_file_stable(p)
            if result is None:
                report["skipped"] += 1
                changed_skipped.append(rel)
                report["skipped_files"].append(rel)
                continue
            data, mtime = result
            info = zipfile.ZipInfo(rel, date_time=_zip_date_time(mtime, rel))
            zf.writestr(info, data, zipfile.ZIP_DEFLATED)
            report["files"].append({"path": rel, "size": len(data), "mtime": mtime})
            report["total_bytes"] += len(data)
        if report["files"] and identity is not None:
            manifest_files = []
            for f in report["files"]:
                kind = "save"
                if classify is not None:
                    try:
                        kind = classify(f["path"])
                    except Exception as exc:
                        # A save is already zipped by this point; losing the
                        # manifest over a labelling bug should never cost the
                        # player their save data too.
                        log.warning("saves: could not classify %s: %s", f["path"], exc)
                manifest_files.append({"path": f["path"], "kind": kind})
            manifest = {
                "version": MANIFEST_VERSION,
                "created_at": time.time(),
                "session": identity,
                "files": manifest_files,
            }
            imported = sorted(f["path"] for f in report["files"] if f["path"] in always_include)
            if imported:
                manifest["imported"] = imported
            zf.writestr(
                zipfile.ZipInfo(MANIFEST_NAME, date_time=time.gmtime()[:6]),
                json.dumps(manifest, indent=2),
                zipfile.ZIP_DEFLATED,
            )
    if report["files"]:
        report["zip_bytes"] = buf.getvalue()
    return _finish_dump(report, changed_skipped)


@dataclass(frozen=True)
class ArchiveView:
    """One pass over an archive's member list, taken before anything is cleared.

    Attributes:
        error: The legacy whole-archive error (not a zip, too large, too many
            entries), or None.
        v1: Non-directory members that are neither the manifest nor under
            `IMPORT_PREFIX`, in zip order.
        imports: Non-directory members under `IMPORT_PREFIX`, in zip order.
        manifest: The parsed manifest, read only when `imports` is non-empty
            and `error` is None.
        manifest_error: Why the manifest could not be used, under the same
            condition.
    """

    error: Optional[str]
    v1: tuple[zipfile.ZipInfo, ...]
    imports: tuple[zipfile.ZipInfo, ...]
    manifest: Optional[Any] = None
    manifest_error: Optional[str] = None


def _read_manifest(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[Optional[Any], Optional[str]]:
    """Parse the archive's manifest, reporting rather than raising on a bad one.

    Args:
        zf: The open archive.
        info: The manifest's entry.

    Returns:
        The parsed JSON and None, or None and the reason it is unusable.
    """
    if info.file_size > MANIFEST_MAX_BYTES:
        return None, f"manifest exceeds {MANIFEST_MAX_BYTES} bytes"
    try:
        raw = zf.read(info)
    except ZIP_READ_ERRORS as exc:
        return None, f"manifest unreadable: {exc}"
    try:
        return json.loads(raw), None
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError and UnicodeDecodeError are both ValueErrors; a
        # deeply nested document raises RecursionError.
        return None, f"manifest is not JSON: {exc}"


def read_archive(content: bytes) -> ArchiveView:
    """Partition an archive's members and apply the whole-archive limits.

    The members are split even when a limit trips, so the caller can still
    tell whether the archive held imports and choose which kind of refusal
    to answer with.

    Args:
        content: The zip archive body.

    Returns:
        The view. Its `error` uses the wording restores have always used, and
        the manifest counts toward both limits, as it always has.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, ValueError, RuntimeError, NotImplementedError, EOFError) as exc:
        # ValueError covers a UTF-8-flagged name that is not valid UTF-8.
        log.debug("saves: archive could not be opened: %s", exc)
        return ArchiveView("body is not a zip archive", (), ())
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        manifest_info: Optional[zipfile.ZipInfo] = None
        v1: list[zipfile.ZipInfo] = []
        imports: list[zipfile.ZipInfo] = []
        for info in infos:
            if info.filename == MANIFEST_NAME:
                manifest_info = info
            elif info.filename.startswith(IMPORT_PREFIX):
                imports.append(info)
            else:
                v1.append(info)
        error: Optional[str] = None
        if sum(i.file_size for i in infos) > SAVE_FILE_MAX_BYTES:
            error = "archive exceeds size limit when extracted"
        elif len(infos) > settings.SAVE_FILE_MAX_ENTRIES:
            error = f"archive holds more than {settings.SAVE_FILE_MAX_ENTRIES} entries"
        manifest: Optional[Any] = None
        manifest_error: Optional[str] = None
        if imports and error is None:
            if manifest_info is None:
                manifest_error = "archive has no manifest"
            else:
                manifest, manifest_error = _read_manifest(zf, manifest_info)
    return ArchiveView(error, tuple(v1), tuple(imports), manifest, manifest_error)


def under_subtrees(member: PurePosixPath, subtrees: tuple[str, ...]) -> bool:
    """Whether an archive member path lies strictly inside one of the subtrees.

    A member that is a subtree name rather than a path below one is refused
    outright by `plan_v1` before it reaches here, so equality
    never has to count as inside.

    Args:
        member: The member path, relative to the save data root.
        subtrees: Subdirectory names to test against.

    Returns:
        True when `member` starts with one of the subtrees followed by a slash.
    """
    rel = member.as_posix()
    return any(rel.startswith(sub + "/") for sub in subtrees)


V1Problem = Literal["escapes", "names_subtree", "outside", "symlink", "unreadable"]
"""Why a v1 member was refused, so a v2 caller can fold it into a refusal code."""


@dataclass(frozen=True)
class V1Plan:
    """What a v1 restore would write, decided before the working slot is cleared.

    Attributes:
        names: Member names to write, in zip order.
        excluded_count: Members dropped because they sit under an excluded subtree.
        problems: Every refused member as `(name, legacy message, kind)`, in zip order.
    """

    names: tuple[str, ...]
    excluded_count: int
    problems: tuple[tuple[str, str, V1Problem], ...]

    @property
    def error(self) -> Optional[str]:
        """The first problem's legacy message, or None when there is none."""
        return self.problems[0][1] if self.problems else None


_READABLE_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
)
"""Compression methods `zipfile` can decompress; any other raises on read."""
_ZIP_ENCRYPTED_FLAG = 0x1
"""Zip general-purpose flag bit marking an encrypted entry."""


def member_problem(info: zipfile.ZipInfo, *, check_date: bool = True) -> Optional[str]:
    """Find what would make a member fail to write, from its header alone.

    Each of these raises only once the member is read or stamped, and a
    restore reads after the working slot is cleared, so they are caught here
    instead.

    Args:
        info: The member's zip entry.
        check_date: Also check the stored timestamp, which only a v1 member's
            write uses.

    Returns:
        What is wrong, phrased to follow "archive member", or None.
    """
    if info.flag_bits & _ZIP_ENCRYPTED_FLAG:
        return "is encrypted"
    if info.compress_type not in _READABLE_COMPRESSION:
        return f"uses unsupported compression method {info.compress_type}"
    if check_date:
        try:
            calendar.timegm(info.date_time)
        except (ValueError, OverflowError):
            return f"has an invalid timestamp {info.date_time}"
    return None


_VERIFY_CHUNK = 1024 * 1024
"""Most bytes one `verify_members` read returns. It does not cap what bzip2 or lzma decompress to fill it."""


def verify_members(content: bytes, names: Iterable[str]) -> tuple[tuple[Optional[str], str], ...]:
    """Read every named member in full, before the working slot is cleared.

    `plan_v1` and preflight judge a member by its headers. Corrupt data only
    shows once the member is decompressed and its CRC checked, and the write
    that would do that runs after the clear. Each member is read in full in
    `_VERIFY_CHUNK` pieces, `zipfile` cuts its output at the declared size,
    and every byte returned counts against `SAVE_FILE_MAX_BYTES`. For bzip2
    and lzma, `zipfile` decompresses a whole compressed read at a time with no
    output cap, so the budget bounds what is returned, not peak memory.

    Args:
        content: The zip archive body.
        names: The members about to be written. A repeated name is read once.

    Returns:
        `(member, message)` for each member that fails, in `names` order, each
        message phrased the way restores phrase theirs. A member of None is an
        archive-level problem, and it ends the check.
    """
    problems: list[tuple[Optional[str], str]] = []
    budget = SAVE_FILE_MAX_BYTES
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, ValueError, RuntimeError, NotImplementedError, EOFError):
        return ((None, "body is not a zip archive"),)
    with zf:
        for name in dict.fromkeys(names):
            try:
                with zf.open(name) as fh:
                    while chunk := fh.read(_VERIFY_CHUNK):
                        budget -= len(chunk)
                        if budget < 0:
                            problems.append((None, "archive exceeds size limit when extracted"))
                            return tuple(problems)
            except (KeyError, *ZIP_READ_ERRORS) as exc:
                log.warning("saves: archive member %s failed its read check: %s", name, exc)
                problems.append((name, f"archive member is corrupt: {name}"))
    return tuple(problems)


def _longest_subtree(rel: str, subtrees: tuple[str, ...]) -> Optional[str]:
    """The deepest subtree `rel` sits strictly inside.

    Args:
        rel: A posix path relative to the save root.
        subtrees: Subtree names to test.

    Returns:
        The longest matching subtree, or None.
    """
    hits = [s for s in subtrees if rel.startswith(s + "/")]
    return max(hits, key=len) if hits else None


def surviving_chain_escapes(root: Path, rel: PurePosixPath, subtrees: tuple[str, ...]) -> bool:
    """Whether a directory the clear leaves standing links `rel` out of the save root.

    Only the components from `root` down to and including the subtree
    directory are checked, which the clear always leaves standing. A link
    below the subtree can survive too (rpcs3, and clears with `keep`, retain
    some entries), but `_write_member`'s resolve check catches it at write
    time, as a 422 after the clear. A component that does not exist yet
    cannot be a link, and the ones below it cannot exist either.

    Args:
        root: The emulator's save data root.
        rel: The destination, relative to `root`.
        subtrees: The subtrees `rel` may sit under; the longest match is used.

    Returns:
        True when a surviving component is a symlink that resolves outside
        `root`, or when a component cannot be inspected. Refusing is the
        choice that cannot write outside the save root.
    """
    sub = _longest_subtree(rel.as_posix(), subtrees)
    if sub is None:
        return False
    try:
        root_real = root.resolve()
    except (OSError, RuntimeError):
        return True
    path = root
    for part in PurePosixPath(sub).parts:
        path = path / part
        try:
            st = path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            try:
                target = path.resolve()
            except (OSError, RuntimeError):
                return True
            if not target.is_relative_to(root_real):
                return True
    return False


def plan_v1(
    view: ArchiveView,
    root: Path,
    subtrees: tuple[str, ...],
    excluded: tuple[str, ...],
    *,
    include_imports: bool = False,
) -> V1Plan:
    """Run every per-member restore check on the v1 members, writing nothing.

    The checks and their messages are the ones restores have always used,
    in the same order, followed by two new ones: a member whose surviving
    parent chain links out of `root`, and a member `member_problem` says
    cannot be read or stamped, are refused here rather than failing to
    write after the slot has already been cleared.

    Args:
        view: The archive, from `read_archive`.
        root: The emulator's save data root.
        subtrees: Subdirectory names members may be restored into.
        excluded: Subdirectory names whose members are counted and dropped.
        include_imports: Also check `.import/` members as if they were v1,
            merged back in zip order, which is how the legacy
            `extract_save_archive` path refuses them.

    Returns:
        The plan, with every problem collected.
    """
    infos = list(view.v1)
    if include_imports and view.imports:
        infos = sorted(infos + list(view.imports), key=lambda i: i.header_offset)
    names: list[str] = []
    problems: list[tuple[str, str, V1Problem]] = []
    excluded_count = 0
    escapes_by_subtree: dict[Optional[str], bool] = {}
    for info in infos:
        name = info.filename
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts:
            problems.append((name, f"archive member escapes save dir: {name}", "escapes"))
            continue
        rel = member.as_posix()
        if rel in subtrees or rel in excluded:
            # A save file always sits inside a subtree, never is one: a dump
            # only ever walks below `root / sub`. Writing such a member would
            # leave a plain file where the emulator expects its save
            # directory, and the mkdir on its next launch would fail.
            problems.append((name, f"archive member names a save subtree: {name}", "names_subtree"))
            continue
        if under_subtrees(member, excluded):
            excluded_count += 1
            continue
        if not under_subtrees(member, subtrees):
            problems.append((name, f"archive member outside save subtrees: {name}", "outside"))
            continue
        sub = _longest_subtree(rel, subtrees)
        if sub not in escapes_by_subtree:
            escapes_by_subtree[sub] = surviving_chain_escapes(root, member, subtrees)
        if escapes_by_subtree[sub]:
            problems.append((name, f"archive member resolves outside save dir: {name}", "symlink"))
            continue
        problem = member_problem(info)
        if problem is not None:
            problems.append((name, f"archive member {problem}: {name}", "unreadable"))
            continue
        names.append(name)
    return V1Plan(tuple(names), excluded_count, tuple(problems))


def _guard_exempt(always_restore: Optional[Callable[[str], bool]], rel: str) -> bool:
    """Whether the emulator exempts `rel` from the restore's newer-file guard.

    Args:
        always_restore: The emulator's test, or None when it declares no exemption.
        rel: The member path, relative to the save data root.

    Returns:
        True only when the emulator says so. A test that raises answers False:
        keeping the guard is the choice that cannot lose a save, and a
        labelling bug in one emulator must not roll another player's progress
        back.
    """
    if always_restore is None:
        return False
    try:
        return bool(always_restore(rel))
    except Exception as exc:
        log.warning(
            "saves: could not test %s against the newer-file guard, keeping the guard: %s",
            rel,
            exc,
        )
        return False


@dataclass(frozen=True)
class ArchivePlan:
    """Everything a restore writes, fixed before the working slot is cleared.

    Attributes:
        v1: v1 member names, written to their own path under the newer-file guard.
        excluded_count: v1 members dropped for sitting under an excluded subtree.
        placed: `(member name, destination)` pairs for declared imports.
        sidecars: `(destination, bytes)` pairs the broker writes beside placed members.
    """

    v1: tuple[str, ...]
    excluded_count: int
    placed: tuple[tuple[str, PurePosixPath], ...] = ()
    sidecars: tuple[tuple[PurePosixPath, bytes], ...] = ()


def _write_member(
    root: Path,
    root_real: Path,
    rel: PurePosixPath,
    read: Callable[[], bytes],
    mtime: float,
    *,
    guard: bool,
    label: str,
) -> Literal["written", "skipped", "failed"]:
    """Write one file into the save tree through a staging file.

    Args:
        root: The emulator's save data root.
        root_real: `root` resolved, for the escape check.
        rel: The destination, relative to `root`.
        read: Returns the bytes to write.
        mtime: The mtime to stamp on the written file.
        guard: Whether a newer file already on disk is kept.
        label: The name to log the file under.

    Returns:
        How the write went.
    """
    target = root / rel
    tmp: Optional[Path] = None
    try:
        # Belt-and-suspenders on top of the member-path checks: confirms the
        # resolved write location is still under root even if some ancestor
        # directory turned out to be a symlink.
        if not target.parent.resolve().is_relative_to(root_real):
            log.warning("saves: %s resolves outside save dir, skipped", label)
            return "failed"
        if guard and target.exists() and target.stat().st_mtime > mtime + _SAVE_MTIME_SLACK:
            return "skipped"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Unique per member: two restores sharing one staging name interleave
        # their writes, and the os.replace below then publishes the mixture
        # as the player's save.
        tmp = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
        tmp.write_bytes(read())
        os.replace(tmp, target)
        os.utime(target, (mtime, mtime))
    except ZIP_READ_ERRORS as exc:
        # `plan_v1` refuses what a header gives away; corrupt data only
        # shows once it is read.
        # This also catches filesystem errors from the write itself, so never narrow it to read errors.
        log.warning("saves: could not restore %s: %s", label, exc)
        # The staging file is dot-prefixed, so `_iter_save_files` never sees
        # it and no later dump would ever carry it off the disk.
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                log.warning("saves: could not remove the staging file %s: %s", tmp, cleanup_exc)
        return "failed"
    return "written"


def write_save_archive(
    content: bytes,
    root: Path,
    plan: ArchivePlan,
    always_restore: Optional[Callable[[str], bool]] = None,
    *,
    stamp: Optional[float] = None,
) -> dict[str, Any]:
    """Write a restore that `plan_v1` (and, for imports, preflight) already approved.

    v1 members are restored as they always have been: existing files newer
    than their member are skipped, so a restore can never roll back saves
    made since the archive was taken, unless `always_restore` exempts the
    member. Placed imports and sidecars skip that guard, because preflight
    refused any collision, and are stamped with the write time rather than a
    zip mtime.

    Args:
        content: The zip archive body, the same bytes the plan was made from.
        root: The emulator's save data root.
        plan: What to write.
        always_restore: Maps a v1 member path to whether it is exempt from the
            newer-file guard, usually `Emulator.always_restore`.
        stamp: The mtime for placed members and sidecars; defaults to now.

    Returns:
        `{"written", "skipped", "excluded", "failed", "imported", "error"}`.
        `written` and `skipped` count v1 members, `imported` counts placed
        members written, `failed` counts every failed write, and `error` is
        set only when the body is not a zip.
    """
    result: dict[str, Any] = {
        "written": 0,
        "skipped": 0,
        "excluded": plan.excluded_count,
        "failed": 0,
        "imported": 0,
        "error": None,
    }
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        result["error"] = "body is not a zip archive"
        return result
    when = time.time() if stamp is None else stamp
    root_real = root.resolve()
    with zf:
        for name in plan.v1:
            try:
                info = zf.getinfo(name)
            except KeyError:
                log.warning("saves: %s is not in the archive, skipped", name)
                result["failed"] += 1
                continue
            try:
                mtime = float(calendar.timegm(info.date_time))
            except (ValueError, OverflowError) as exc:
                log.warning("saves: %s has an unusable timestamp, stamping it now: %s", name, exc)
                mtime = when
            outcome = _write_member(
                root,
                root_real,
                PurePosixPath(name),
                lambda info=info: zf.read(info),
                mtime,
                guard=not _guard_exempt(always_restore, name),
                label=name,
            )
            result[outcome] += 1
        for name, dest in plan.placed:
            outcome = _write_member(
                root, root_real, dest, lambda name=name: zf.read(name), when, guard=False, label=name
            )
            if outcome == "written":
                result["imported"] += 1
            else:
                result["failed"] += 1
        for dest, data in plan.sidecars:
            outcome = _write_member(
                root, root_real, dest, lambda data=data: data, when, guard=False, label=dest.as_posix()
            )
            if outcome == "failed":
                result["failed"] += 1
    return result


def extract_save_archive(
    content: bytes,
    root: Path,
    subtrees: tuple[str, ...],
    excluded: tuple[str, ...] = (),
    always_restore: Optional[Callable[[str], bool]] = None,
) -> dict[str, Any]:
    """Restore an archive into the emulator's data dir in one call.

    Composed from `read_archive`, `plan_v1` and `write_save_archive`. It is
    kept for callers that validate and write in one step: activate uses the
    pieces instead, so it can validate before the working slot is cleared.
    `.import/` members are checked as v1 here and refused as lying outside
    the subtrees, which is how this path has always treated them.

    `excluded` names subtrees the emulator owns but this session syncs some
    other way; their members are counted and dropped rather than refused.
    An archive's `MANIFEST_NAME` is dropped: it describes the archive for the
    parent and is not save data.

    Args:
        content: The zip archive body.
        root: The emulator's save data root.
        subtrees: Subdirectory names under `root` that members may be restored into.
        excluded: Subdirectory names whose members are counted and dropped.
        always_restore: Maps a member path to whether it is exempt from the
            newer-file guard, usually `Emulator.always_restore`.

    Returns:
        `{"written", "skipped", "excluded", "failed", "imported", "error"}`,
        with `error` set (and nothing written) when the body is not a zip, the
        archive is too large, or a member escapes the save dir, names a
        subtree itself, lies outside the subtrees, resolves outside the
        save dir through a surviving symlink, or is encrypted, compressed in
        a way `zipfile` cannot read, or stamped with an impossible date.
    """
    view = read_archive(content)
    error = view.error
    plan: Optional[V1Plan] = None
    if error is None:
        plan = plan_v1(view, root, subtrees, excluded, include_imports=True)
        error = plan.error
    if error is not None or plan is None:
        return {
            "written": 0, "skipped": 0, "excluded": 0, "failed": 0, "imported": 0, "error": error
        }
    return write_save_archive(
        content, root, ArchivePlan(plan.names, plan.excluded_count), always_restore
    )


def write_export(zip_bytes: bytes, name: str) -> str:
    """Persist a dump archive under `settings.EXPORT_DIR` for inspection.

    Written through a uniquely named staging file in the same directory and
    renamed into place, so the archive only ever appears whole: this is the
    copy kept when an upload fails, and a truncated one left by a crash or read
    mid-write would look like a save the player still has.

    Args:
        zip_bytes: The archive body.
        name: The filename to write it as.

    Returns:
        The path written, as a string.

    Raises:
        OSError: When the archive could not be staged or renamed into place.
    """
    settings.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = settings.EXPORT_DIR / name
    tmp = settings.EXPORT_DIR / f".{name}.{secrets.token_hex(8)}.tmp"
    try:
        tmp.write_bytes(zip_bytes)
        os.replace(tmp, path)
    except OSError as exc:
        log.error("saves: could not write the export %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            log.warning("saves: could not remove the staging file %s: %s", tmp, cleanup_exc)
        raise
    log.debug("saves: wrote the export %s (%d bytes)", path, len(zip_bytes))
    return str(path)
