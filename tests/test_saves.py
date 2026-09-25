"""Save archive build and restore.

Covers the delta archive built at exit, the restore run at activate, and the import and export
writers.
"""

import io
import json
import logging
import os
import time
import tracemalloc
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import pytest

from webstation_broker import saves

from .conftest import corrupt_zip_member, mangle_zip_member

# Zip entries carry a DOS timestamp, which has no room for anything before
# 1980, so the fixture clock sits in 2020 rather than at the epoch.
OLD = 1_600_000_000
BASELINE = OLD + 2000
NEW = OLD + 3000


def _write(path: Path, content: bytes = b"data", mtime: Optional[float] = None) -> Path:
    """Write a file, creating its parents and optionally pinning its mtime.

    Args:
        path: Where to write.
        content: The bytes to write.
        mtime: Unix time to stamp on the file, or None to leave the clock alone.

    Returns:
        The path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _zip(
    members: dict[str, bytes], when: tuple[int, int, int, int, int, int] = (2020, 1, 1, 0, 0, 0)
) -> bytes:
    """Build an in-memory zip archive whose entries all carry one timestamp.

    Args:
        members: Archive member names mapped to their bytes.
        when: The date_time tuple stamped on every entry.

    Returns:
        The zip file contents.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=when), content)
    return buf.getvalue()


_CORRUPTIBLE = bytes(range(256)) * 64
"""Member data that compresses to enough bytes for `corrupt_zip_member` to damage under every method."""
_METHODS = [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]
"""Every compression method a member may use; bzip2 and lzma are refused outright."""
_METHOD_IDS = ["stored", "deflate"]
"""Test ids for `_METHODS`, in the same order."""


def _zip_with(name: str, data: bytes, method: int) -> bytes:
    """Build a one-member archive compressed with `method`.

    Args:
        name: The member's name.
        data: The member's bytes.
        method: A `zipfile.ZIP_*` compression constant.

    Returns:
        The zip file contents.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), data, compress_type=method)
    return buf.getvalue()


def test_build_ships_only_files_written_since_the_baseline(tmp_path: Path) -> None:
    """Build ships only the files written since the baseline."""
    _write(tmp_path / "memcards" / "old.bin", b"old", mtime=OLD)
    _write(tmp_path / "memcards" / "new.bin", b"new", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("memcards",), baseline=BASELINE)

    assert [f["path"] for f in report["files"]] == ["memcards/new.bin"]
    assert report["total_bytes"] == 3
    with zipfile.ZipFile(io.BytesIO(report["zip_bytes"])) as zf:
        assert zf.namelist() == ["memcards/new.bin"]


def test_build_ignores_subtrees_it_was_not_given(tmp_path: Path) -> None:
    """Build ignores subtrees it was not given."""
    _write(tmp_path / "memcards" / "card.bin", mtime=NEW)
    _write(tmp_path / "elsewhere" / "secret.bin", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("memcards",), baseline=0)

    assert [f["path"] for f in report["files"]] == ["memcards/card.bin"]


def test_build_ships_a_dot_prefixed_save(tmp_path: Path) -> None:
    """A save an emulator wrote under a dot-prefixed name is still save data."""
    _write(tmp_path / "sstates" / ".hidden" / "inside.bin", mtime=NEW)
    _write(tmp_path / "sstates" / ".config.tmp", mtime=NEW)
    _write(tmp_path / "sstates" / "state.p2s", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("sstates",), baseline=0)

    assert [f["path"] for f in report["files"]] == [
        "sstates/.config.tmp", "sstates/.hidden/inside.bin", "sstates/state.p2s"
    ]


@pytest.mark.parametrize(
    "scratch",
    [
        pytest.param(".state.p2s.0123456789abcdef.tmp", id="staging"),
        pytest.param("sub/.card.raw.0123456789abcdef.tmp", id="nested-staging"),
        pytest.param(".atime-probe.1234", id="atime-probe"),
        pytest.param(".atime-probe.1234.89abcdef", id="atime-probe-tagged"),
        pytest.param(".Mcd001.ps2.new/_pcsx2_superblock", id="memcard-staging"),
        pytest.param(".Mcd001.ps2.old/_pcsx2_superblock", id="memcard-backup"),
    ],
)
def test_build_skips_broker_scratch_a_crash_left_behind(tmp_path: Path, scratch: str) -> None:
    """Scratch the broker writes inside a save tree never ships, wherever it was left."""
    _write(tmp_path / "sstates" / scratch, mtime=NEW)
    _write(tmp_path / "sstates" / "state.p2s", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("sstates",), baseline=0)

    assert [f["path"] for f in report["files"]] == ["sstates/state.p2s"]


def test_build_skips_a_fresh_staging_name(tmp_path: Path) -> None:
    """The skip matches what `_staging_name` actually produces."""
    _write(tmp_path / "sstates" / saves._staging_name("state.p2s"), mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("sstates",), baseline=0)

    assert report["files"] == []


def test_build_skips_a_manifest_left_in_a_save_tree(tmp_path: Path) -> None:
    """The broker's own archive index is never mistaken for save data."""
    _write(tmp_path / "sstates" / saves.MANIFEST_NAME, b"{}", mtime=NEW)
    _write(tmp_path / "sstates" / "state.p2s", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("sstates",), baseline=0)

    assert [f["path"] for f in report["files"]] == ["sstates/state.p2s"]


def test_build_drops_the_shadps4_corrupted_marker(tmp_path: Path) -> None:
    """Build drops the shadPS4 corrupted marker."""
    _write(tmp_path / "savedata" / "CUSA00001" / "sce_sys" / "corrupted", b"", mtime=NEW)
    _write(tmp_path / "savedata" / "CUSA00001" / "save.bin", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("savedata",), baseline=0)

    assert [f["path"] for f in report["files"]] == ["savedata/CUSA00001/save.bin"]


def test_build_refuses_a_dump_over_the_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build refuses a dump over the size limit."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 8)
    _write(tmp_path / "sstates" / "big.p2s", b"x" * 16, mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("sstates",), baseline=0)

    assert report["zip_bytes"] is None
    assert "size limit" in report["error"]


def test_a_size_limit_failure_carries_no_unrelated_skip_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stat failure elsewhere in the walk must not dress up a size-limit failure.

    The two have nothing to do with each other, and a "could not be read" line
    logged alongside the size-limit error reads as its explanation to whoever
    is debugging the failed exit.
    """
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 8)
    big = _write(tmp_path / "sstates" / "big.p2s", b"x" * 16, mtime=NEW)
    vanished = tmp_path / "sstates" / "gone.p2s"

    def _listing(root: Path, subtrees: tuple[str, ...]) -> list[Path]:
        """List the oversized file and one removed between walk and stat.

        Args:
            root: The save data root, unused.
            subtrees: The subtree names, unused.

        Returns:
            The two paths, the second of which no longer exists.
        """
        return [big, vanished]

    monkeypatch.setattr(saves, "_iter_save_files", _listing)

    with caplog.at_level(logging.WARNING):
        report = saves.build_save_archive(tmp_path, ("sstates",), baseline=BASELINE)

    assert report["error"] == "changed saves exceed size limit (16 bytes)"
    assert report["zip_bytes"] is None
    assert report["skipped_files"] == ["sstates/gone.p2s"]
    assert "could not stat" in caplog.text
    assert "could not be read" not in caplog.text


def test_build_reports_a_missing_save_root(tmp_path: Path) -> None:
    """Build reports a missing save root."""
    report = saves.build_save_archive(tmp_path / "gone", ("sstates",), baseline=0)

    assert report["zip_bytes"] is None
    assert "save data root missing" in report["error"]


def test_build_produces_nothing_when_the_session_wrote_nothing(tmp_path: Path) -> None:
    """Build produces nothing when the session wrote nothing."""
    _write(tmp_path / "memcards" / "card.bin", mtime=OLD)

    report = saves.build_save_archive(tmp_path, ("memcards",), baseline=BASELINE)

    assert report["files"] == []
    assert report["zip_bytes"] is None
    assert report["error"] is None


def test_a_stat_failure_alone_is_not_a_failed_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that vanishes mid-walk cannot fail a session that changed nothing.

    Its mtime is unknowable once the stat fails, so there is no evidence it was
    even touched this session; erroring would report a clean no-op exit as a
    lost save and have RomM treat the session as failed.
    """
    kept = _write(tmp_path / "memcards" / "old.bin", b"old", mtime=OLD)
    vanished = tmp_path / "memcards" / "gone.bin"

    def _listing(root: Path, subtrees: tuple[str, ...]) -> list[Path]:
        """List one real file plus one already removed between walk and stat.

        Args:
            root: The save data root, unused.
            subtrees: The subtree names, unused.

        Returns:
            The two paths, the second of which no longer exists.
        """
        return [kept, vanished]

    monkeypatch.setattr(saves, "_iter_save_files", _listing)

    with caplog.at_level(logging.WARNING):
        report = saves.build_save_archive(tmp_path, ("memcards",), baseline=BASELINE)

    assert report["error"] is None
    assert report["skipped"] == 1
    assert report["skipped_files"] == ["memcards/gone.bin"]
    assert report["zip_bytes"] is None
    # The stat failure is logged where it happens, naming the file it dropped.
    assert "could not stat" in caplog.text
    assert str(vanished) in caplog.text


def test_a_dump_where_every_candidate_fails_to_stat_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When every save-file candidate fails to stat, there is no evidence anything was unchanged.

    A single stray stat failure among otherwise-stattable files is tolerated
    (see test_a_stat_failure_alone_is_not_a_failed_dump), but when the walk
    turns up candidates and *none* of them can be stat'd, "nothing changed"
    was never actually established and must not be reported as a clean no-op.
    """
    first = tmp_path / "memcards" / "gone1.bin"
    second = tmp_path / "memcards" / "gone2.bin"

    def _listing(root: Path, subtrees: tuple[str, ...]) -> list[Path]:
        """List two paths, both already removed between walk and stat.

        Args:
            root: The save data root, unused.
            subtrees: The subtree names, unused.

        Returns:
            The two vanished paths.
        """
        return [first, second]

    monkeypatch.setattr(saves, "_iter_save_files", _listing)

    with caplog.at_level(logging.ERROR):
        report = saves.build_save_archive(tmp_path, ("memcards",), baseline=BASELINE)

    assert report["zip_bytes"] is None
    assert report["skipped"] == 2
    assert report["skipped_files"] == ["memcards/gone1.bin", "memcards/gone2.bin"]
    assert "could not stat any of the 2 save file(s)" in report["error"]
    assert "could not stat any of the 2 save file(s)" in caplog.text


def test_a_dump_whose_every_changed_file_is_unreadable_still_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed save that could not be read fails the dump rather than reading as a no-op."""
    _write(tmp_path / "memcards" / "card.bin", b"new", mtime=NEW)

    def _unreadable(p: Path, retries: int = 4, settle: float = 0.5) -> None:
        """Fail the read the way a file still being written does.

        Args:
            p: The file to read.
            retries: How many reads to attempt, unused.
            settle: Seconds between attempts, unused.

        Returns:
            None, always.
        """
        return None

    monkeypatch.setattr(saves, "_read_file_stable", _unreadable)

    report = saves.build_save_archive(tmp_path, ("memcards",), baseline=BASELINE)

    assert "none of the 1 changed save file(s) could be read" in report["error"]
    assert report["skipped_files"] == ["memcards/card.bin"]


def test_skipped_files_stay_root_relative_in_both_skip_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both skip paths name the file the same way, since one list feeds one error string."""
    torn = _write(tmp_path / "memcards" / "torn.bin", b"new", mtime=NEW)
    vanished = tmp_path / "memcards" / "gone.bin"

    def _listing(root: Path, subtrees: tuple[str, ...]) -> list[Path]:
        """List the readable file and one removed between walk and stat.

        Args:
            root: The save data root, unused.
            subtrees: The subtree names, unused.

        Returns:
            The two paths, the second of which no longer exists.
        """
        return [torn, vanished]

    def _unreadable(p: Path, retries: int = 4, settle: float = 0.5) -> None:
        """Fail the read the way a file still being written does.

        Args:
            p: The file to read.
            retries: How many reads to attempt, unused.
            settle: Seconds between attempts, unused.

        Returns:
            None, always.
        """
        return None

    monkeypatch.setattr(saves, "_iter_save_files", _listing)
    monkeypatch.setattr(saves, "_read_file_stable", _unreadable)

    report = saves.build_save_archive(tmp_path, ("memcards",), baseline=BASELINE)

    assert report["skipped_files"] == ["memcards/gone.bin", "memcards/torn.bin"]
    assert report["skipped"] == 2
    # Only the file confirmed changed counts towards the failure, and the error
    # names exactly the files it counted: a stat failure has no evidence behind
    # it, so listing it under that count would overstate what was lost.
    assert (
        report["error"] == "none of the 1 changed save file(s) could be read: memcards/torn.bin"
    )


def test_archive_round_trips_through_a_restore(tmp_path: Path) -> None:
    """An archive round-trips through a restore."""
    source = tmp_path / "source"
    _write(source / "GC" / "card.raw", b"payload", mtime=NEW)
    report = saves.build_save_archive(source, ("GC",), baseline=0)

    target = tmp_path / "target"
    target.mkdir()
    result = saves.extract_save_archive(report["zip_bytes"], target, ("GC",))

    assert result == {
        "written": 1, "skipped": 0, "excluded": 0, "failed": 0, "imported": 0, "error": None
    }
    assert (target / "GC" / "card.raw").read_bytes() == b"payload"


def test_a_dot_prefixed_save_round_trips_through_a_restore(tmp_path: Path) -> None:
    """A dotfile save dumped at exit lands back where it came from."""
    source = tmp_path / "source"
    _write(source / "GC" / ".hidden" / "card.raw", b"payload", mtime=NEW)
    report = saves.build_save_archive(source, ("GC",), baseline=0)

    target = tmp_path / "target"
    target.mkdir()
    result = saves.extract_save_archive(report["zip_bytes"], target, ("GC",))

    assert result["written"] == 1
    assert (target / "GC" / ".hidden" / "card.raw").read_bytes() == b"payload"


def test_restore_counts_a_corrupt_member_as_failed_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member with a bad CRC is counted as failed, not left to crash the restore.

    `zipfile.BadZipFile` is not an `OSError` or `ValueError`, so it slips past
    the per-member except clause unless it is caught alongside them; the rest
    of the archive still has to land instead of aborting half-applied.
    """
    content = _zip({"GC/good.bin": b"fine", "GC/bad.bin": b"corrupt"})
    original_read = zipfile.ZipFile.read

    def flaky_read(self: zipfile.ZipFile, name: Any, *args: Any, **kwargs: Any) -> bytes:
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if filename == "GC/bad.bin":
            raise zipfile.BadZipFile("Bad CRC-32 for file 'GC/bad.bin'")
        return original_read(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", flaky_read)

    result = saves.extract_save_archive(content, tmp_path, ("GC",))

    assert result["error"] is None
    assert result["written"] == 1
    assert result["failed"] == 1
    assert (tmp_path / "GC" / "good.bin").read_bytes() == b"fine"
    assert not (tmp_path / "GC" / "bad.bin").exists()


def test_restore_refuses_a_member_outside_the_save_subtrees(tmp_path: Path) -> None:
    """Restore refuses a member outside the save subtrees."""
    result = saves.extract_save_archive(_zip({"etc/passwd": b"x"}), tmp_path, ("GC",))

    assert "outside save subtrees" in result["error"]
    assert not (tmp_path / "etc").exists()


def test_restore_refuses_a_member_named_for_a_save_subtree(tmp_path: Path) -> None:
    """A member named for a subtree itself is refused before it can land as a file.

    Writing one would leave a plain file where the emulator keeps its states,
    and the mkdir on its next launch would raise FileExistsError.
    """
    result = saves.extract_save_archive(_zip({"states": b"x"}), tmp_path, ("states", "saves"))

    assert "names a save subtree" in result["error"]
    assert not (tmp_path / "states").exists()


def test_restore_refuses_a_member_named_for_an_excluded_subtree(tmp_path: Path) -> None:
    """An excluded subtree's own name is refused too, not quietly counted as excluded."""
    result = saves.extract_save_archive(
        _zip({"memcards": b"x"}), tmp_path, ("sstates",), excluded=("memcards",)
    )

    assert "names a save subtree" in result["error"]
    assert result["excluded"] == 0
    assert not (tmp_path / "memcards").exists()


def test_restore_refuses_a_member_that_escapes_the_root(tmp_path: Path) -> None:
    """Restore refuses a member that escapes the root."""
    result = saves.extract_save_archive(_zip({"GC/../../out.bin": b"x"}), tmp_path, ("GC",))

    assert "escapes save dir" in result["error"]


def test_restore_refuses_a_body_that_is_not_a_zip(tmp_path: Path) -> None:
    """Restore refuses a body that is not a zip."""
    result = saves.extract_save_archive(b"not a zip", tmp_path, ("GC",))

    assert result["error"] == "body is not a zip archive"


def test_restore_passes_over_an_excluded_subtree(tmp_path: Path) -> None:
    """Restore passes over an excluded subtree."""
    content = _zip({"memcards/Slot1/card": b"stale", "sstates/state.p2s": b"keep"})

    result = saves.extract_save_archive(
        content, tmp_path, ("sstates",), excluded=("memcards",)
    )

    assert result["excluded"] == 1
    assert result["written"] == 1
    assert result["error"] is None
    assert not (tmp_path / "memcards").exists()


def test_restore_never_rolls_back_a_newer_save(tmp_path: Path) -> None:
    """Restore never rolls back a newer save."""
    existing = _write(tmp_path / "GC" / "card.raw", b"newer", mtime=time.time() + 60)
    old = (2020, 1, 1, 0, 0, 0)

    result = saves.extract_save_archive(_zip({"GC/card.raw": b"older"}, when=old), tmp_path, ("GC",))

    assert result["skipped"] == 1
    assert result["written"] == 0
    assert existing.read_bytes() == b"newer"


def test_restore_refuses_an_archive_too_large_to_unpack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore refuses an archive too large to unpack."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 8)

    result = saves.extract_save_archive(_zip({"GC/card.raw": b"x" * 16}), tmp_path, ("GC",))

    assert "size limit" in result["error"]


def test_restore_refuses_an_archive_with_too_many_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore refuses an archive with more than SAVE_FILE_MAX_ENTRIES entries."""
    from webstation_broker import settings

    monkeypatch.setattr(settings, "SAVE_FILE_MAX_ENTRIES", 2)
    content = _zip({"GC/a.bin": b"1", "GC/b.bin": b"2", "GC/c.bin": b"3"})

    result = saves.extract_save_archive(content, tmp_path, ("GC",))

    assert "more than 2 entries" in result["error"]
    assert not (tmp_path / "GC").exists()


def test_restore_skips_a_member_whose_directory_resolves_outside_root(tmp_path: Path) -> None:
    """Restore skips a member whose directory resolves outside root via a symlink."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    (root / "GC").mkdir(parents=True)
    (root / "GC" / "linked").symlink_to(outside, target_is_directory=True)

    result = saves.extract_save_archive(
        _zip({"GC/linked/card.raw": b"x"}), root, ("GC",)
    )

    assert result["failed"] == 1
    assert result["written"] == 0
    assert not (outside / "card.raw").exists()


def test_write_export_persists_the_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write export persists the dump."""
    from webstation_broker import settings

    monkeypatch.setattr(settings, "EXPORT_DIR", tmp_path / "exports")

    path = saves.write_export(b"zip-body", "session-1.zip")

    assert (tmp_path / "exports" / "session-1.zip").read_bytes() == b"zip-body"
    assert path.endswith("session-1.zip")


def test_build_labels_every_member_in_the_manifest(tmp_path: Path) -> None:
    """With an identity, the archive carries a manifest naming each member's kind."""
    root = tmp_path / "root"
    _write(root / "states" / "game.state", b"s", mtime=NEW)
    _write(root / "saves" / "game.srm", b"v", mtime=NEW)
    identity = {"emulator": "retroarch", "core": "snes9x", "platform": "snes"}

    report = saves.build_save_archive(
        root,
        ("states", "saves"),
        baseline=0,
        identity=identity,
        classify=lambda rel: "state" if rel.startswith("states/") else "save",
    )

    with zipfile.ZipFile(io.BytesIO(report["zip_bytes"])) as zf:
        manifest = json.loads(zf.read(saves.MANIFEST_NAME))
    assert manifest["version"] == saves.MANIFEST_VERSION
    assert manifest["session"] == identity
    assert {f["path"]: f["kind"] for f in manifest["files"]} == {
        "states/game.state": "state",
        "saves/game.srm": "save",
    }
    # The manifest describes the archive; it is not one of the dumped files.
    assert [f["path"] for f in report["files"]] == ["states/game.state", "saves/game.srm"]


def test_build_labels_members_as_saves_without_a_classifier(tmp_path: Path) -> None:
    """An identity with no classifier still yields a manifest, all members plain saves."""
    root = tmp_path / "root"
    _write(root / "GC" / "card.raw", b"payload", mtime=NEW)

    report = saves.build_save_archive(root, ("GC",), baseline=0, identity={"emulator": "dolphin"})

    with zipfile.ZipFile(io.BytesIO(report["zip_bytes"])) as zf:
        manifest = json.loads(zf.read(saves.MANIFEST_NAME))
    assert manifest["files"] == [{"path": "GC/card.raw", "kind": "save"}]


def test_build_leaves_the_manifest_out_without_an_identity(tmp_path: Path) -> None:
    """No identity, no manifest: an archive stays exactly what it was before."""
    root = tmp_path / "root"
    _write(root / "GC" / "card.raw", b"payload", mtime=NEW)

    report = saves.build_save_archive(root, ("GC",), baseline=0)

    with zipfile.ZipFile(io.BytesIO(report["zip_bytes"])) as zf:
        assert zf.namelist() == ["GC/card.raw"]


def test_restore_drops_the_manifest_instead_of_refusing_the_archive(tmp_path: Path) -> None:
    """The manifest sits outside every subtree, so a restore has to pass it over."""
    target = tmp_path / "target"
    target.mkdir()
    body = _zip({saves.MANIFEST_NAME: b"{}", "GC/card.raw": b"payload"})

    result = saves.extract_save_archive(body, target, ("GC",))

    assert result == {
        "written": 1, "skipped": 0, "excluded": 0, "failed": 0, "imported": 0, "error": None
    }
    assert (target / "GC" / "card.raw").read_bytes() == b"payload"
    assert not (target / saves.MANIFEST_NAME).exists()


def test_manifest_archive_round_trips_through_a_restore(tmp_path: Path) -> None:
    """An archive built with a manifest restores as cleanly as one without."""
    source = tmp_path / "source"
    _write(source / "GC" / "card.raw", b"payload", mtime=NEW)
    report = saves.build_save_archive(
        source, ("GC",), baseline=0, identity={"emulator": "dolphin"}
    )

    target = tmp_path / "target"
    target.mkdir()
    result = saves.extract_save_archive(report["zip_bytes"], target, ("GC",))

    assert result == {
        "written": 1, "skipped": 0, "excluded": 0, "failed": 0, "imported": 0, "error": None
    }
    assert (target / "GC" / "card.raw").read_bytes() == b"payload"


def test_a_classifier_failure_logs_and_falls_back_to_save(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken classifier costs a manifest label, never the save data already zipped."""
    root = tmp_path / "root"
    _write(root / "GC" / "card.raw", b"payload", mtime=NEW)

    def broken(rel: str) -> str:
        raise ValueError("boom")

    with caplog.at_level(logging.WARNING):
        report = saves.build_save_archive(
            root, ("GC",), baseline=0, identity={"emulator": "dolphin"}, classify=broken
        )

    with zipfile.ZipFile(io.BytesIO(report["zip_bytes"])) as zf:
        assert zf.read("GC/card.raw") == b"payload"
        manifest = json.loads(zf.read(saves.MANIFEST_NAME))
    assert manifest["files"] == [{"path": "GC/card.raw", "kind": "save"}]
    assert "could not classify" in caplog.text


def test_restore_leaves_no_staging_file_behind_when_a_member_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member that fails on its way into place takes its staging file with it.

    The staging name is dot-prefixed so the dump walk never sees it, which is exactly why a
    leftover would sit unnoticed in the player's save directory for the life of the container.
    """

    def _refuse(src: object, dst: object) -> None:
        """Fail the swap into place the way a full disk would.

        Args:
            src: The staging file.
            dst: The final path.
        """
        raise OSError("no space left on device")

    monkeypatch.setattr(saves.os, "replace", _refuse)

    result = saves.extract_save_archive(_zip({"GC/card.raw": b"x"}), tmp_path, ("GC",))

    assert result["failed"] == 1
    assert result["written"] == 0
    assert list((tmp_path / "GC").iterdir()) == []


# -- read_archive: the partition every restore starts from --


def test_read_archive_partitions_v1_imports_and_the_manifest() -> None:
    """Members split into v1 and `.import/`, and the manifest is neither."""
    manifest = {"version": 2, "files": []}
    body = _zip(
        {
            "saves/a.srm": b"a",
            ".import/save/b.srm": b"b",
            saves.MANIFEST_NAME: json.dumps(manifest).encode(),
        }
    )

    view = saves.read_archive(body)

    assert view.error is None
    assert [i.filename for i in view.v1] == ["saves/a.srm"]
    assert [i.filename for i in view.imports] == [".import/save/b.srm"]
    assert view.manifest == manifest
    assert view.manifest_error is None


def test_read_archive_leaves_the_manifest_unparsed_without_imports() -> None:
    """A v1 dump's manifest is never parsed, so a large one can never refuse a restore."""
    body = _zip({"saves/a.srm": b"a", saves.MANIFEST_NAME: b"not json"})

    view = saves.read_archive(body)

    assert view.error is None
    assert view.manifest is None
    assert view.manifest_error is None


def test_read_archive_rejects_a_body_that_is_not_a_zip() -> None:
    """A non-zip body keeps today's error wording."""
    view = saves.read_archive(b"nope")

    assert view.error == "body is not a zip archive"
    assert view.v1 == () and view.imports == ()


def test_read_archive_reports_the_size_cap_but_still_partitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tripped cap keeps the partition, so the caller can tell whether imports exist."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 3)
    body = _zip({"saves/a.srm": b"aa", ".import/save/b.srm": b"bb"})

    view = saves.read_archive(body)

    assert view.error == "archive exceeds size limit when extracted"
    assert len(view.imports) == 1
    assert view.manifest is None


def test_read_archive_counts_the_manifest_toward_the_entry_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest is an entry like any other, as it is today."""
    monkeypatch.setattr(saves.settings, "SAVE_FILE_MAX_ENTRIES", 1)
    body = _zip({"saves/a.srm": b"a", saves.MANIFEST_NAME: b"{}"})

    view = saves.read_archive(body)

    assert view.error == "archive holds more than 1 entries"


@pytest.mark.parametrize(
    ("manifest", "fragment"),
    [
        (b"{not json", "not JSON"),
        (b"x" * (1024 * 1024 + 1), "exceeds"),
        (None, "no manifest"),
    ],
    ids=["not-json", "oversized", "missing"],
)
def test_read_archive_reports_an_unusable_manifest_beside_imports(
    manifest: Optional[bytes], fragment: str
) -> None:
    """With imports present, a manifest that cannot be used is recorded rather than raised.

    Args:
        manifest: The manifest bytes, or None to leave it out.
        fragment: Text the recorded manifest error must contain.
    """
    members = {".import/save/b.srm": b"b"}
    if manifest is not None:
        members[saves.MANIFEST_NAME] = manifest
    view = saves.read_archive(_zip(members))

    assert view.error is None
    assert view.manifest is None
    assert fragment in (view.manifest_error or "")


def test_read_archive_rejects_a_name_flagged_utf8_that_is_not() -> None:
    """A UTF-8-flagged name that does not decode is not a zip, not a crash."""
    body = mangle_zip_member(_zip({"GC/X": b"x"}), "GC/X", flags=0x800, raw_name=b"GC/\xff")

    view = saves.read_archive(body)

    assert view.error == "body is not a zip archive"
    assert view.v1 == () and view.imports == ()


def test_read_archive_reports_a_manifest_nested_too_deep_to_parse() -> None:
    """A manifest that blows the parser's recursion limit is unusable, not a crash."""
    deep = b"[" * 100_000 + b"]" * 100_000
    view = saves.read_archive(_zip({".import/save/b.srm": b"b", saves.MANIFEST_NAME: deep}))

    assert view.manifest is None
    assert "not JSON" in (view.manifest_error or "")


def test_read_archive_reports_a_manifest_whose_data_is_corrupt() -> None:
    """A deflated manifest whose data is damaged is unusable, not a crash."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".import/save/b.srm", b"b")
        zf.writestr(saves.MANIFEST_NAME, json.dumps({"version": 2, "pad": "x" * 4000}))
    body = bytearray(buf.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(body))) as zf:
        info = zf.getinfo(saves.MANIFEST_NAME)
    start = info.header_offset + 30 + len(info.filename.encode())
    body[start : start + info.compress_size] = b"\xff" * info.compress_size

    view = saves.read_archive(bytes(body))

    assert view.error is None
    assert view.manifest is None
    assert "manifest unreadable" in (view.manifest_error or "")


# -- plan_v1: every v1 check, with nothing written --


def _plan(tmp_path: Path, members: dict[str, bytes], **kwargs: Any) -> saves.V1Plan:
    """Plan a v1 restore of `members` into `tmp_path / "root"`.

    Args:
        tmp_path: The per-test temporary directory.
        members: Archive member names mapped to their bytes.
        **kwargs: Passed through to `plan_v1`.

    Returns:
        The plan.
    """
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    view = saves.read_archive(_zip(members))
    return saves.plan_v1(view, root, ("GC", "states"), kwargs.pop("excluded", ()), **kwargs)


def test_plan_v1_accepts_members_under_the_subtrees(tmp_path: Path) -> None:
    """Members under a subtree are planned in zip order and nothing is written."""
    plan = _plan(tmp_path, {"GC/a.raw": b"a", "states/b.s": b"b"})

    assert plan.names == ("GC/a.raw", "states/b.s")
    assert plan.problems == ()
    assert plan.error is None
    assert not (tmp_path / "root" / "GC").exists()


@pytest.mark.parametrize(
    ("name", "message", "kind"),
    [
        ("../x", "archive member escapes save dir: ../x", "escapes"),
        ("/abs", "archive member escapes save dir: /abs", "escapes"),
        ("GC", "archive member names a save subtree: GC", "names_subtree"),
        ("other/x", "archive member outside save subtrees: other/x", "outside"),
    ],
)
def test_plan_v1_keeps_the_legacy_messages(tmp_path: Path, name: str, message: str, kind: str) -> None:
    """Each check keeps today's wording, and the problem carries its kind.

    Args:
        tmp_path: The per-test temporary directory.
        name: The offending member name.
        message: The legacy error text.
        kind: The problem kind recorded beside it.
    """
    plan = _plan(tmp_path, {name: b"x"})

    assert plan.problems == ((name, message, kind),)
    assert plan.error == message


@pytest.mark.parametrize(
    ("mangle", "message"),
    [
        ({"flags": 0x1}, "archive member is encrypted: GC/a"),
        ({"method": 99}, "archive member uses unsupported compression method 99: GC/a"),
        (
            {"method": zipfile.ZIP_BZIP2},
            "archive member uses unsupported compression method 12: GC/a",
        ),
        (
            {"method": zipfile.ZIP_LZMA},
            "archive member uses unsupported compression method 14: GC/a",
        ),
        ({"dos_date": 0}, "archive member has an invalid timestamp (1980, 0, 0, 0, 0, 0): GC/a"),
    ],
    ids=["encrypted", "compression", "bzip2", "lzma", "date"],
)
def test_plan_v1_refuses_a_member_that_would_fail_to_write(
    tmp_path: Path, mangle: dict[str, int], message: str
) -> None:
    """A member whose header says it cannot be read or stamped is refused before any write.

    Each of these used to raise inside the write, after the slot was cleared.

    Args:
        tmp_path: The per-test temporary directory.
        mangle: The header fields to break, passed to `mangle_zip_member`.
        message: The legacy-style error text.
    """
    root = tmp_path / "root"
    root.mkdir()
    body = mangle_zip_member(_zip({"GC/a": b"a", "GC/ok": b"k"}), "GC/a", **mangle)

    plan = saves.plan_v1(saves.read_archive(body), root, ("GC",), ())

    assert plan.problems == (("GC/a", message, "unreadable"),)
    assert plan.names == ("GC/ok",)


def test_plan_v1_never_reads_an_excluded_member_header(tmp_path: Path) -> None:
    """A dropped member is never written, so a broken header on it is no problem."""
    root = tmp_path / "root"
    root.mkdir()
    body = mangle_zip_member(_zip({"card/a": b"a"}), "card/a", flags=0x1)

    plan = saves.plan_v1(saves.read_archive(body), root, ("GC",), ("card",))

    assert plan.problems == ()
    assert plan.excluded_count == 1


def test_plan_v1_collects_every_problem(tmp_path: Path) -> None:
    """All problems are collected, not just the first, and `error` is the first."""
    plan = _plan(tmp_path, {"../x": b"x", "other/y": b"y", "GC/ok": b"z"})

    assert [p[2] for p in plan.problems] == ["escapes", "outside"]
    assert plan.names == ("GC/ok",)
    assert plan.error == "archive member escapes save dir: ../x"


def test_plan_v1_counts_excluded_members(tmp_path: Path) -> None:
    """A member under an excluded subtree is counted and dropped."""
    root = tmp_path / "root"
    root.mkdir()
    view = saves.read_archive(_zip({"card/a": b"a", "GC/b": b"b"}))

    plan = saves.plan_v1(view, root, ("GC",), ("card",))

    assert plan.excluded_count == 1
    assert plan.names == ("GC/b",)


def test_plan_v1_skips_imports_unless_asked(tmp_path: Path) -> None:
    """`.import/` members are left out, unless the legacy path asks for them."""
    members = {"GC/a": b"a", ".import/save/x": b"x"}

    assert _plan(tmp_path, members).problems == ()
    legacy = _plan(tmp_path, members, include_imports=True)
    assert legacy.error == "archive member outside save subtrees: .import/save/x"


def test_plan_v1_refuses_a_subtree_whose_link_leaves_the_root(tmp_path: Path) -> None:
    """A subtree that is a symlink out of the root is refused before any clear."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "states").symlink_to(outside)

    plan = _plan(tmp_path, {"states/a.s": b"a"})

    assert plan.problems == (
        ("states/a.s", "archive member resolves outside save dir: states/a.s", "symlink"),
    )


@pytest.mark.parametrize(
    "members",
    [{"GC/a": b"file", "GC/a/b": b"child"}, {"GC/a/b": b"child", "GC/a": b"file"}],
    ids=["file-first", "dir-first"],
)
def test_plan_v1_refuses_a_member_that_is_another_members_directory(
    tmp_path: Path, members: dict[str, bytes]
) -> None:
    """A path cannot be both a file and a directory, so one of the two would fail after the clear.

    Args:
        tmp_path: The per-test temporary directory.
        members: The two colliding members, in either zip order.
    """
    plan = _plan(tmp_path, members)

    assert plan.problems == (
        ("GC/a", "archive member is also the directory of another member: GC/a", "collides"),
    )
    assert plan.names == ("GC/a/b",)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("GC/.a.raw.0123456789abcdef.tmp", id="staging"),
        pytest.param("GC/.atime-probe.1234", id="atime-probe"),
        pytest.param("GC/.Mcd001.ps2.old/_pcsx2_superblock", id="memcard-backup"),
        pytest.param(f"GC/{saves.MANIFEST_NAME}", id="manifest"),
    ],
)
def test_plan_v1_refuses_a_member_no_dump_would_carry_back(tmp_path: Path, name: str) -> None:
    """A member named like broker scratch would land on disk and never be saved again."""
    plan = _plan(tmp_path, {name: b"x", "GC/b.raw": b"b"})

    assert plan.problems == (
        (name, f"archive member is named like broker scratch, which is never saved back: {name}", "scratch"),
    )
    assert plan.names == ("GC/b.raw",)


def _zip_entries(entries: list[tuple[str, bytes]]) -> bytes:
    """Build an in-memory zip archive that may hold one name more than once.

    Args:
        entries: `(name, bytes)` pairs, written in order.

    Returns:
        The zip file contents.
    """
    buf = io.BytesIO()
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries:
            zf.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), content)
    return buf.getvalue()


def test_plan_v1_refuses_a_name_the_archive_holds_twice(tmp_path: Path) -> None:
    """Two copies of one member leave which one lands up to write order, so neither is written."""
    root = tmp_path / "root"
    root.mkdir()
    body = _zip_entries([("GC/a", b"first"), ("GC/b", b"b"), ("GC/a", b"second")])

    plan = saves.plan_v1(saves.read_archive(body), root, ("GC",), ())

    assert plan.problems == (("GC/a", "archive holds 2 members for GC/a: GC/a", "duplicate"),)
    assert plan.names == ("GC/b",)


def test_plan_v1_refuses_a_dot_segment_alias_of_another_member(tmp_path: Path) -> None:
    """`GC/./a` and `GC//a` are the file `GC/a` under another spelling."""
    plan = _plan(tmp_path, {"GC/./a": b"one", "GC/b": b"b", "GC/a": b"two", "GC//a": b"three"})

    assert plan.problems == (("GC/./a", "archive holds 3 members for GC/a: GC/./a", "duplicate"),)
    assert plan.names == ("GC/b",)


def _name_max(path: Path) -> int:
    """The longest single name the filesystem under `path` takes.

    Args:
        path: Any existing directory on the filesystem.

    Returns:
        Its `PC_NAME_MAX`, in bytes.
    """
    return os.pathconf(path, "PC_NAME_MAX")


def test_plan_v1_refuses_a_component_longer_than_the_filesystem_takes(tmp_path: Path) -> None:
    """A name the filesystem cannot hold is refused before the clear, not failed after it."""
    long_dir = "d" * (_name_max(tmp_path) + 1)

    plan = _plan(tmp_path, {f"GC/{long_dir}/a.raw": b"a"})

    assert [(p[0], p[2]) for p in plan.problems] == [(f"GC/{long_dir}/a.raw", "too_long")]
    assert plan.names == ()


def test_plan_v1_counts_the_staging_suffix_against_the_name_limit(tmp_path: Path) -> None:
    """The write stages through `.<name>.<16 hex>.tmp`, so that name has to fit too.

    A final component that fits on its own but not once staged would pass a
    bare NAME_MAX check and still fail after the clear.
    """
    staging_overhead = len(".") + len(".") + 16 + len(".tmp")
    fits = "f" * (_name_max(tmp_path) - staging_overhead)
    too_long = fits + "g"

    plan = _plan(tmp_path, {f"GC/{fits}": b"a", f"GC/{too_long}": b"b"})

    assert [(p[0], p[2]) for p in plan.problems] == [(f"GC/{too_long}", "too_long")]
    assert plan.names == (f"GC/{fits}",)
    written = saves.write_save_archive(
        _zip({f"GC/{fits}": b"a"}), tmp_path / "root", saves.ArchivePlan(plan.names, 0)
    )
    assert written["failed"] == 0


def test_plan_v1_reads_the_name_limit_from_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name limit is read from the filesystem: eCryptfs takes 143 bytes, so 255 is not safe.

    Args:
        tmp_path: The per-test temporary directory.
        monkeypatch: Stands in a filesystem with a 143-byte name limit.
    """
    real_pathconf = os.pathconf

    def ecryptfs(path: Any, name: Any) -> int:
        """Answer like an eCryptfs mount.

        Args:
            path: The queried path.
            name: The queried limit.

        Returns:
            143 for the name limit, the real answer otherwise.
        """
        return 143 if name == "PC_NAME_MAX" else real_pathconf(path, name)

    monkeypatch.setattr(os, "pathconf", ecryptfs)
    member = f"GC/{'d' * 150}/a.raw"

    plan = _plan(tmp_path, {member: b"a", "GC/ok.raw": b"k"})

    assert [(p[0], p[2]) for p in plan.problems] == [(member, "too_long")]
    assert "143" in plan.problems[0][1]
    assert plan.names == ("GC/ok.raw",)


def test_plan_v1_refuses_a_path_longer_than_the_filesystem_takes(tmp_path: Path) -> None:
    """Every component can fit and the whole path still be too long to open."""
    deep = "/".join(["d" * 200] * 21)
    member = f"GC/{deep}/g.raw"

    plan = _plan(tmp_path, {member: b"x"})

    assert [(p[0], p[2]) for p in plan.problems] == [(member, "too_long")]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only mode")
def test_plan_v1_refuses_a_subtree_it_cannot_write(tmp_path: Path) -> None:
    """Permission drift on a save directory is found before the clear, once per directory."""
    root = tmp_path / "root"
    locked = root / "GC"
    locked.mkdir(parents=True)
    locked.chmod(0o500)
    try:
        plan = _plan(tmp_path, {"GC/a.raw": b"a", "GC/b.raw": b"b", "states/c.s": b"c"})
    finally:
        locked.chmod(0o700)

    assert plan.problems == (
        ("GC/a.raw", "archive member cannot be written, GC is not writable: GC/a.raw", "unwritable"),
    )
    assert plan.names == ("states/c.s",)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only mode")
def test_plan_v1_probes_the_subtree_a_clear_leaves_behind(tmp_path: Path) -> None:
    """The clear removes a writable subdirectory, and the write then recreates it in the subtree."""
    root = tmp_path / "root"
    locked = root / "GC"
    (locked / "sub").mkdir(parents=True)
    locked.chmod(0o500)
    try:
        plan = _plan(tmp_path, {"GC/sub/a.raw": b"a"})
    finally:
        locked.chmod(0o700)

    assert plan.problems == (
        ("GC/sub/a.raw", "archive member cannot be written, GC is not writable: GC/sub/a.raw", "unwritable"),
    )
    assert plan.names == ()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only mode")
def test_plan_v1_probes_the_nearest_directory_that_exists(tmp_path: Path) -> None:
    """A subtree the write would create is judged by the directory it would be created in."""
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0o500)
    try:
        plan = _plan(tmp_path, {"GC/sub/a.raw": b"a"})
    finally:
        root.chmod(0o700)

    assert [p[2] for p in plan.problems] == ["unwritable"]


def test_plan_v1_leaves_nothing_behind_from_its_write_probe(tmp_path: Path) -> None:
    """The writability probe cleans up after itself and creates no directories."""
    root = tmp_path / "root"
    (root / "GC").mkdir(parents=True)

    plan = _plan(tmp_path, {"GC/deep/a.raw": b"a", "states/b.s": b"b"})

    assert plan.problems == ()
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == ["GC"]


def test_surviving_chain_escapes_ignores_links_that_stay_inside(tmp_path: Path) -> None:
    """A link that resolves inside the root is harmless, and a missing chain cannot escape."""
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "GC").symlink_to(root / "real")

    assert saves.surviving_chain_escapes(root, PurePosixPath("GC/a"), ("GC",)) is False
    assert saves.surviving_chain_escapes(root, PurePosixPath("states/a"), ("states",)) is False


def test_surviving_chain_escapes_checks_every_level_of_a_nested_subtree(tmp_path: Path) -> None:
    """A link part-way down a multi-level subtree is caught, using the longest match."""
    outside = tmp_path / "outside"
    (outside / "b").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").symlink_to(outside)

    assert saves.surviving_chain_escapes(root, PurePosixPath("a/b/f"), ("a", "a/b")) is True


def _linked_root(tmp_path: Path) -> tuple[Path, Path]:
    """Build a save root whose `savestates` subtree links to a directory beside it.

    Args:
        tmp_path: The per-test temporary directory.

    Returns:
        The save root and the directory `savestates` links to.
    """
    real = tmp_path / "sstates"
    real.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "savestates").symlink_to(real, target_is_directory=True)
    return root, real


def test_surviving_chain_escapes_lets_a_declared_link_root_through(tmp_path: Path) -> None:
    """A subtree link that resolves exactly to a link root is not an escape."""
    root, real = _linked_root(tmp_path)
    rel = PurePosixPath("savestates/BLUS30443/x")

    assert saves.surviving_chain_escapes(root, rel, ("savestates",)) is True
    assert saves.surviving_chain_escapes(root, rel, ("savestates",), (real,)) is False


def test_surviving_chain_escapes_holds_a_link_root_to_an_exact_match(tmp_path: Path) -> None:
    """A link into a subdirectory of a link root, or to anywhere else, still escapes."""
    root, real = _linked_root(tmp_path)
    (real / "sub").mkdir()
    (root / "savestates").unlink()
    (root / "savestates").symlink_to(real / "sub", target_is_directory=True)
    rel = PurePosixPath("savestates/x")

    assert saves.surviving_chain_escapes(root, rel, ("savestates",), (real,)) is True

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "savestates").unlink()
    (root / "savestates").symlink_to(elsewhere, target_is_directory=True)

    assert saves.surviving_chain_escapes(root, rel, ("savestates",), (real,)) is True


def test_plan_v1_accepts_a_member_through_a_link_root(tmp_path: Path) -> None:
    """The later-activate case: the link exists when the plan is made, and the plan takes it."""
    root, real = _linked_root(tmp_path)
    view = saves.read_archive(_zip({"savestates/BLUS30443/x.SAVESTAT": b"s"}))

    refused = saves.plan_v1(view, root, ("savestates",), ())
    accepted = saves.plan_v1(view, root, ("savestates",), (), link_roots=(real,))

    assert [p[2] for p in refused.problems] == ["symlink"]
    assert accepted.problems == ()
    assert accepted.names == ("savestates/BLUS30443/x.SAVESTAT",)


def test_write_save_archive_writes_through_a_link_root(tmp_path: Path) -> None:
    """The first-activate case: the link is made after the plan, and the write still lands."""
    root, real = _linked_root(tmp_path)
    body = _zip({"savestates/BLUS30443/x.SAVESTAT": b"state"})
    names = ("savestates/BLUS30443/x.SAVESTAT",)

    result = saves.write_save_archive(body, root, saves.ArchivePlan(names, 0, link_roots=(real,)))

    assert (result["written"], result["failed"]) == (1, 0)
    assert (real / "BLUS30443" / "x.SAVESTAT").read_bytes() == b"state"


def test_write_save_archive_refuses_a_link_root_write_without_the_declaration(tmp_path: Path) -> None:
    """With no `link_roots` the same write fails, as it always has."""
    root, real = _linked_root(tmp_path)
    body = _zip({"savestates/BLUS30443/x.SAVESTAT": b"state"})

    result = saves.write_save_archive(body, root, saves.ArchivePlan(("savestates/BLUS30443/x.SAVESTAT",), 0))

    assert (result["written"], result["failed"]) == (0, 1)
    assert not (real / "BLUS30443").exists()


def test_write_save_archive_still_refuses_a_hostile_link_below_a_link_root(tmp_path: Path) -> None:
    """A link inside the link root that leaves it is caught by the write-time check."""
    root, real = _linked_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (real / "BLUS30443").symlink_to(outside, target_is_directory=True)
    body = _zip({"savestates/BLUS30443/x.SAVESTAT": b"state"})
    names = ("savestates/BLUS30443/x.SAVESTAT",)

    result = saves.write_save_archive(body, root, saves.ArchivePlan(names, 0, link_roots=(real,)))

    assert (result["written"], result["failed"]) == (0, 1)
    assert list(outside.iterdir()) == []


def test_plan_v1_still_refuses_a_dotdot_member_with_link_roots(tmp_path: Path) -> None:
    """Declaring a link root does not soften the member-path checks."""
    root, real = _linked_root(tmp_path)
    view = saves.read_archive(_zip({"savestates/../escape": b"x"}))

    plan = saves.plan_v1(view, root, ("savestates",), (), link_roots=(real,))

    assert [p[2] for p in plan.problems] == ["escapes"]


# -- write_save_archive: the write half of a restore --


def test_write_save_archive_writes_v1_members_under_the_guard(tmp_path: Path) -> None:
    """v1 members keep the zip mtime and the newer-file guard."""
    root = tmp_path / "root"
    _write(root / "GC" / "kept.raw", b"newer", mtime=NEW)
    body = _zip({"GC/kept.raw": b"older", "GC/new.raw": b"fresh"})

    result = saves.write_save_archive(body, root, saves.ArchivePlan(("GC/kept.raw", "GC/new.raw"), 2))

    assert result == {
        "written": 1, "skipped": 1, "excluded": 2, "failed": 0, "imported": 0, "error": None
    }
    assert (root / "GC" / "kept.raw").read_bytes() == b"newer"
    assert (root / "GC" / "new.raw").read_bytes() == b"fresh"


def test_write_save_archive_places_imports_unguarded_and_stamped(tmp_path: Path) -> None:
    """Placed members skip the guard and carry the write stamp, not the zip mtime."""
    root = tmp_path / "root"
    _write(root / "GC" / "slot.raw", b"newer", mtime=NEW)
    body = _zip({".import/save/x.raw": b"imported"})
    plan = saves.ArchivePlan(
        (), 0, placed=((".import/save/x.raw", PurePosixPath("GC/slot.raw")),)
    )

    result = saves.write_save_archive(body, root, plan, stamp=NEW + 50)

    assert result["imported"] == 1
    assert result["written"] == 0
    assert (root / "GC" / "slot.raw").read_bytes() == b"imported"
    assert (root / "GC" / "slot.raw").stat().st_mtime == NEW + 50


def test_write_save_archive_writes_sidecars(tmp_path: Path) -> None:
    """Sidecars are written with the stamp and only their failures are counted."""
    root = tmp_path / "root"
    root.mkdir()
    plan = saves.ArchivePlan((), 0, sidecars=((PurePosixPath("GC/a.rom"), b"id\n"),))

    result = saves.write_save_archive(_zip({}), root, plan, stamp=NEW)

    assert result["written"] == 0 and result["failed"] == 0
    assert (root / "GC" / "a.rom").read_bytes() == b"id\n"


def test_write_save_archive_counts_a_link_out_of_the_root_as_failed(tmp_path: Path) -> None:
    """The resolve check still guards every write, placed members included."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "GC").symlink_to(outside)
    body = _zip({".import/save/x": b"x"})
    plan = saves.ArchivePlan((), 0, placed=((".import/save/x", PurePosixPath("GC/x")),))

    result = saves.write_save_archive(body, root, plan)

    assert result["failed"] == 1 and result["imported"] == 0
    assert not (outside / "x").exists()


def test_write_save_archive_stamps_an_unusable_zip_date_with_now(tmp_path: Path) -> None:
    """A date the plan did not vet falls back to the write stamp instead of raising."""
    root = tmp_path / "root"
    root.mkdir()
    body = mangle_zip_member(_zip({"GC/a": b"a"}), "GC/a", dos_date=0)

    result = saves.write_save_archive(body, root, saves.ArchivePlan(("GC/a",), 0), stamp=NEW)

    assert result["written"] == 1 and result["failed"] == 0
    assert (root / "GC" / "a").stat().st_mtime == NEW


def test_a_future_member_is_stamped_before_the_restore_and_never_ships_back(tmp_path: Path) -> None:
    """A member from a device with a bad clock must not look like this session's own save.

    The dump ships anything stamped at or after the launch baseline, so a
    2107 mtime left on disk would ride along on every exit, still stamped
    2107. Stamping it at the restore itself is not enough either: the
    baseline is taken right after, and its slack reaches back past it.
    """
    root = tmp_path / "root"
    root.mkdir()
    body = _zip({"GC/a.raw": b"old"}, when=(2107, 12, 31, 0, 0, 0))

    result = saves.write_save_archive(body, root, saves.ArchivePlan(("GC/a.raw",), 0))
    launch = time.time()

    assert result["written"] == 1
    assert (root / "GC" / "a.raw").stat().st_mtime < launch
    dump = saves.build_save_archive(root, ("GC",), launch)
    assert dump["files"] == []


def test_a_past_member_keeps_its_own_mtime(tmp_path: Path) -> None:
    """Only a future stamp is replaced; an ordinary one still round-trips unchanged."""
    root = tmp_path / "root"
    root.mkdir()
    body = _zip({"GC/a.raw": b"old"}, when=(2020, 1, 1, 0, 0, 0))

    saves.write_save_archive(body, root, saves.ArchivePlan(("GC/a.raw",), 0), stamp=NEW)

    assert (root / "GC" / "a.raw").stat().st_mtime == 1_577_836_800


@pytest.mark.parametrize(
    "mangle",
    [{"flags": 0x1}, {"method": 99}],
    ids=["encrypted", "compression"],
)
def test_write_save_archive_counts_an_unreadable_member_as_failed(
    tmp_path: Path, mangle: dict[str, int]
) -> None:
    """A member that raises on read is a failed write, never an exception out of the restore.

    Args:
        tmp_path: The per-test temporary directory.
        mangle: The header fields to break, passed to `mangle_zip_member`.
    """
    root = tmp_path / "root"
    root.mkdir()
    body = mangle_zip_member(_zip({"GC/a": b"a"}), "GC/a", **mangle)

    result = saves.write_save_archive(body, root, saves.ArchivePlan(("GC/a",), 0))

    assert result["failed"] == 1 and result["written"] == 0
    assert list((root / "GC").iterdir()) == []


def test_write_save_archive_counts_a_corrupt_lzma_member_as_failed(tmp_path: Path) -> None:
    """Corrupt lzma data is a failed write, not an `LZMAError` out of the restore."""
    root = tmp_path / "root"
    root.mkdir()
    body = corrupt_zip_member(_zip_with("GC/a", _CORRUPTIBLE, zipfile.ZIP_LZMA), "GC/a")

    result = saves.write_save_archive(body, root, saves.ArchivePlan(("GC/a",), 0))

    assert result["failed"] == 1 and result["written"] == 0
    assert list((root / "GC").iterdir()) == []


def test_read_archive_refuses_a_manifest_compressed_with_bzip2_or_lzma() -> None:
    """A manifest using either method is refused by its header, never decompressed.

    `_read_manifest` runs before any per-member plan exists to gate it, so it
    applies `member_problem` itself rather than inheriting the guard from a caller.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(".import/save/a.srm", b"a")
        zf.writestr(
            zipfile.ZipInfo(saves.MANIFEST_NAME, date_time=(2020, 1, 1, 0, 0, 0)),
            json.dumps({"version": 2, "files": []}),
            compress_type=zipfile.ZIP_LZMA,
        )

    view = saves.read_archive(buf.getvalue())

    assert view.manifest is None
    assert view.manifest_error == "manifest uses unsupported compression method 14"


# -- verify_members: the pre-clear read --


@pytest.mark.parametrize("method", _METHODS, ids=_METHOD_IDS)
def test_verify_members_passes_an_intact_member(method: int) -> None:
    """An intact member reads clean under every compression method.

    Args:
        method: The member's compression method.
    """
    body = _zip_with("saves/a.srm", _CORRUPTIBLE, method)

    assert saves.verify_members(body, ["saves/a.srm"]) == ()


@pytest.mark.parametrize("method", _METHODS, ids=_METHOD_IDS)
def test_verify_members_reports_a_corrupt_member(method: int, caplog: pytest.LogCaptureFixture) -> None:
    """Corrupt data fails its read under every readable method, both raising `BadZipFile` on the CRC.

    Args:
        method: The member's compression method.
        caplog: Pytest's log capture.
    """
    body = corrupt_zip_member(_zip_with("saves/a.srm", _CORRUPTIBLE, method), "saves/a.srm")

    with caplog.at_level(logging.WARNING, logger="webstation_broker.saves"):
        problems = saves.verify_members(body, ["saves/a.srm"])

    assert problems == (("saves/a.srm", "archive member is corrupt: saves/a.srm"),)
    assert "saves/a.srm failed its read check" in caplog.text


@pytest.mark.parametrize(
    "method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA], ids=["bzip2", "lzma"]
)
def test_verify_members_refuses_a_bzip2_or_lzma_member_without_reading_it(
    method: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A member using either method is refused by its header, the same as a corrupt one.

    `zipfile`'s bzip2 and lzma readers have no cap on a single call's decompressed
    output, so `verify_members` checks `member_problem` before it ever opens a
    member, the same guard `plan_v1` and `normalise_member` apply.

    Args:
        method: The member's compression method.
        caplog: Pytest's log capture.
    """
    body = _zip_with("saves/a.srm", b"intact data", method)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.saves"):
        problems = saves.verify_members(body, ["saves/a.srm"])

    assert problems == (
        ("saves/a.srm", f"archive member uses unsupported compression method {method}: saves/a.srm"),
    )
    assert f"uses unsupported compression method {method}" in caplog.text


def test_verify_members_refuses_a_bzip2_bomb_without_decompressing_it() -> None:
    """A member that would balloon on decompression is refused by its header, never decompressed.

    Builds a member whose real data decompresses to 24 MiB of zeros, compressed
    down to a few hundred bytes by bzip2. Before the read-hardening this member
    would have been decompressed in full to check its declared size; the fix
    refuses it by its compression method instead, so peak memory during the
    call stays near zero regardless of what the member would expand to.
    """
    huge = bytes(24 * 1024 * 1024)
    body = _zip_with("saves/a.srm", huge, zipfile.ZIP_BZIP2)
    del huge

    tracemalloc.start()
    try:
        problems = saves.verify_members(body, ["saves/a.srm"])
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert problems == (
        ("saves/a.srm", "archive member uses unsupported compression method 12: saves/a.srm"),
    )
    assert peak < 4 * 1024 * 1024


def test_verify_members_reads_only_the_names_it_is_given() -> None:
    """A corrupt member the plan leaves out is never read."""
    body = corrupt_zip_member(_zip({"saves/a": _CORRUPTIBLE, "saves/b": b"b"}), "saves/a")

    assert saves.verify_members(body, ["saves/b"]) == ()


def test_verify_members_reads_a_repeated_name_once() -> None:
    """A name listed twice is read, and reported, once."""
    body = corrupt_zip_member(_zip({"saves/a": _CORRUPTIBLE}), "saves/a")

    assert saves.verify_members(body, ["saves/a", "saves/a"]) == (
        ("saves/a", "archive member is corrupt: saves/a"),
    )


def test_verify_members_reports_a_name_the_archive_lacks() -> None:
    """A planned name missing from the archive is a problem, not a `KeyError`."""
    assert saves.verify_members(_zip({"saves/a": b"a"}), ["saves/b"]) == (
        ("saves/b", "archive member is missing: saves/b"),
    )


def test_verify_members_stops_at_the_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decompressed bytes count against `SAVE_FILE_MAX_BYTES`, and the check ends once they pass it.

    Args:
        monkeypatch: Pytest's attribute patcher.
    """
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 10)
    body = _zip({"saves/a": b"x" * 8, "saves/b": b"y" * 8, "saves/c": b"z"})

    assert saves.verify_members(body, ["saves/a", "saves/b", "saves/c"]) == (
        (None, "archive exceeds size limit when extracted"),
    )


def test_verify_members_reports_a_body_that_is_not_a_zip() -> None:
    """A body that is not a zip is one archive-level problem."""
    assert saves.verify_members(b"not a zip", ["saves/a"]) == ((None, "body is not a zip archive"),)


def test_extract_save_archive_still_refuses_import_members(tmp_path: Path) -> None:
    """The legacy path refuses `.import/` members exactly as it always has."""
    root = tmp_path / "root"
    root.mkdir()
    body = _zip({"GC/a": b"a", ".import/save/x": b"x"})

    result = saves.extract_save_archive(body, root, ("GC",))

    assert result["error"] == "archive member outside save subtrees: .import/save/x"
    assert not (root / "GC").exists()


# -- always_include: placed imports ship even if untouched --


def test_always_include_ships_an_untouched_placed_file(tmp_path: Path) -> None:
    """A placed import older than the baseline still ships, and the manifest names it."""
    root = tmp_path / "root"
    _write(root / "GC" / "placed.raw", b"p", mtime=OLD)
    _write(root / "GC" / "other.raw", b"o", mtime=OLD)

    report = saves.build_save_archive(
        root,
        ("GC",),
        BASELINE,
        identity={"emulator": "fake"},
        always_include=frozenset({"GC/placed.raw"}),
    )

    assert [f["path"] for f in report["files"]] == ["GC/placed.raw"]
    with zipfile.ZipFile(io.BytesIO(report["zip_bytes"])) as zf:
        manifest = json.loads(zf.read(saves.MANIFEST_NAME))
    assert manifest["imported"] == ["GC/placed.raw"]
    assert manifest["version"] == 1


def test_always_include_never_resurrects_a_file_that_is_gone(tmp_path: Path) -> None:
    """A placed path that was deleted or set aside is simply absent."""
    root = tmp_path / "root"
    _write(root / "GC" / "placed.raw.untrusted", b"p", mtime=OLD)

    report = saves.build_save_archive(
        root, ("GC",), BASELINE, always_include=frozenset({"GC/placed.raw", "GC/gone.raw"})
    )

    assert report["files"] == []
    assert report["zip_bytes"] is None


def test_always_include_still_respects_the_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forcing a file in never lets a dump past the size cap."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 1)
    root = tmp_path / "root"
    _write(root / "GC" / "placed.raw", b"big", mtime=OLD)

    report = saves.build_save_archive(
        root, ("GC",), BASELINE, always_include=frozenset({"GC/placed.raw"})
    )

    assert report["error"] is not None and "size limit" in report["error"]


def test_a_dump_without_imports_has_no_imported_key(tmp_path: Path) -> None:
    """A plain dump's manifest is byte-for-byte the v1 shape it always was."""
    root = tmp_path / "root"
    _write(root / "GC" / "a.raw", b"a", mtime=NEW)

    report = saves.build_save_archive(root, ("GC",), BASELINE, identity={"emulator": "fake"})

    with zipfile.ZipFile(io.BytesIO(report["zip_bytes"])) as zf:
        assert "imported" not in json.loads(zf.read(saves.MANIFEST_NAME))
