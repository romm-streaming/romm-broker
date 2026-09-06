"""Save data in and out of the emulator's save directories.

Activate restores a zip archive into the emulator's save directories; exit
zips every save file modified since launch.
"""

import calendar
import io
import json
import logging
import os
import secrets
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

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
        if st.st_mtime >= cutoff:
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
            zf.writestr(
                zipfile.ZipInfo(MANIFEST_NAME, date_time=time.gmtime()[:6]),
                json.dumps(manifest, indent=2),
                zipfile.ZIP_DEFLATED,
            )
    if report["files"]:
        report["zip_bytes"] = buf.getvalue()
    return _finish_dump(report, changed_skipped)


def _under(member: PurePosixPath, subtrees: tuple[str, ...]) -> bool:
    """Whether an archive member path lies strictly inside one of the subtrees.

    A member that is a subtree name rather than a path below one is refused
    outright by `extract_save_archive` before it reaches here, so equality
    never has to count as inside.

    Args:
        member: The member path, relative to the save data root.
        subtrees: Subdirectory names to test against.

    Returns:
        True when `member` starts with one of the subtrees followed by a slash.
    """
    rel = member.as_posix()
    return any(rel.startswith(sub + "/") for sub in subtrees)


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


def extract_save_archive(
    content: bytes,
    root: Path,
    subtrees: tuple[str, ...],
    excluded: tuple[str, ...] = (),
    always_restore: Optional[Callable[[str], bool]] = None,
) -> dict[str, Any]:
    """Restore an archive into the emulator's data dir.

    `excluded` names subtrees the emulator owns but this session syncs some
    other way. Those members are dropped rather than refused: archives taken
    before that sync was turned on still carry them, and restoring one would
    undo what the other route just wrote. A member under neither is still a
    hard error, since that is the guard against an archive writing outside the
    save area.

    Existing files newer than their archive member are skipped so a restore
    can never roll back saves made since the archive was taken. `always_restore`
    exempts the members an emulator says that guard does not describe: a file
    whose mtime on disk records which player last used the container rather
    than progress this player would lose. Each file is written through a temp
    file and renamed into place.

    An archive's `MANIFEST_NAME` is dropped: it describes the archive for the
    parent and is not save data.

    Args:
        content: The zip archive body.
        root: The emulator's save data root.
        subtrees: Subdirectory names under `root` that members may be restored into.
        excluded: Subdirectory names whose members are counted and dropped.
        always_restore: Maps a member path to whether it is exempt from the
            newer-file guard, usually `Emulator.always_restore`; without it
            every member is subject to the guard.

    Returns:
        A dict of the shape `{"written", "skipped", "excluded", "failed", "error"}`
        with counts for the first four and `error` set (and nothing written) when
        the body is not a zip, the archive is too large, or a member escapes the
        save dir, names a subtree itself, or lies outside the subtrees.
    """
    result = {"written": 0, "skipped": 0, "excluded": 0, "failed": 0, "error": None}
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        result["error"] = "body is not a zip archive"
        return result
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > SAVE_FILE_MAX_BYTES:
            result["error"] = "archive exceeds size limit when extracted"
            return result
        if len(infos) > settings.SAVE_FILE_MAX_ENTRIES:
            result["error"] = f"archive holds more than {settings.SAVE_FILE_MAX_ENTRIES} entries"
            return result
        wanted = []
        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                result["error"] = f"archive member escapes save dir: {info.filename}"
                return result
            if info.filename == MANIFEST_NAME:
                # The broker's own index, not save data: it sits outside every
                # subtree, so it has to be dropped before the subtree check.
                continue
            rel = member.as_posix()
            if rel in subtrees or rel in excluded:
                # A save file always sits inside a subtree, never is one: a dump
                # only ever walks below `root / sub`. Writing such a member would
                # leave a plain file where the emulator expects its save
                # directory, and the mkdir on its next launch would fail.
                result["error"] = f"archive member names a save subtree: {info.filename}"
                return result
            if _under(member, excluded):
                result["excluded"] += 1
                continue
            if not _under(member, subtrees):
                result["error"] = f"archive member outside save subtrees: {info.filename}"
                return result
            wanted.append(info)

        root_real = root.resolve()
        for info in wanted:
            target = root / PurePosixPath(info.filename)
            mtime = calendar.timegm(info.date_time)
            exempt = _guard_exempt(always_restore, info.filename)
            tmp: Optional[Path] = None
            try:
                # Belt-and-suspenders on top of the member-path check above:
                # confirms the resolved write location is still under root
                # even if some ancestor directory turned out to be a symlink.
                if not target.parent.resolve().is_relative_to(root_real):
                    log.warning("saves: %s resolves outside save dir, skipped", info.filename)
                    result["failed"] += 1
                    continue
                if (
                    not exempt
                    and target.exists()
                    and target.stat().st_mtime > mtime + _SAVE_MTIME_SLACK
                ):
                    result["skipped"] += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # Unique per member: two restores sharing one staging name
                # interleave their writes, and the os.replace below then
                # publishes the mixture as the player's save.
                tmp = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
                tmp.write_bytes(zf.read(info))
                os.replace(tmp, target)
                os.utime(target, (mtime, mtime))
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                log.warning("saves: could not restore %s: %s", info.filename, exc)
                # The staging file is dot-prefixed, so `_iter_save_files` never
                # sees it and no later dump would ever carry it off the disk.
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError as cleanup_exc:
                        log.warning(
                            "saves: could not remove the staging file %s: %s", tmp, cleanup_exc
                        )
                result["failed"] += 1
                continue
            result["written"] += 1
    return result


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
