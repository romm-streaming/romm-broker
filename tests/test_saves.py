"""Save archive build and restore.

Covers the delta archive built at exit, the restore run at activate, and the import and export
writers.
"""

import io
import json
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

import pytest

from webstation_broker import saves

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


def test_build_skips_dot_prefixed_entries(tmp_path: Path) -> None:
    """Build skips dot-prefixed entries."""
    _write(tmp_path / "sstates" / ".staging.tmp", mtime=NEW)
    _write(tmp_path / "sstates" / ".hidden" / "inside.bin", mtime=NEW)
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

    assert result == {"written": 1, "skipped": 0, "excluded": 0, "failed": 0, "error": None}
    assert (target / "GC" / "card.raw").read_bytes() == b"payload"


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

    assert result == {"written": 1, "skipped": 0, "excluded": 0, "failed": 0, "error": None}
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

    assert result == {"written": 1, "skipped": 0, "excluded": 0, "failed": 0, "error": None}
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
