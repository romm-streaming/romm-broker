"""Xenia ROM resolution, launch, the stale-save clear, the profile restore, and the exit restamp."""

import io
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator, NoReturn, Optional

import pytest

from webstation_broker import saves
from webstation_broker.emulators import xenia


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point xenia.ROM_ROOT at a fresh temp directory."""
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(xenia, "ROM_ROOT", root)
    return root


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point xenia's storage root and log path at a fresh temp directory."""
    d = tmp_path / "xenia"
    monkeypatch.setattr(xenia, "DATA_DIR", d)
    monkeypatch.setattr(xenia.Xenia, "save_root", d)
    monkeypatch.setattr(xenia, "XENIA_LOG_PATH", tmp_path / "xenia.log")
    monkeypatch.setattr(xenia.Xenia, "log_path", tmp_path / "xenia.log")
    return d


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"rom")
    return path


# ── ROM resolution ───────────────────────────────────────────────────────────


def test_resolve_takes_a_file_as_given(rom_root: Path) -> None:
    """A direct file path is returned unchanged."""
    rom = _touch(rom_root / "Game.iso")

    assert xenia.Xenia().resolve_rom_file(rom) == rom


def test_resolve_boots_an_extracted_dump_from_its_default_xex(rom_root: Path) -> None:
    """An extracted dump folder boots from its default.xex, not sibling files."""
    game = rom_root / "game"
    _touch(game / "Game.iso")
    xex = _touch(game / "default.xex")

    assert xenia.Xenia().resolve_rom_file(game) == xex


def test_resolve_accepts_a_default_xex_that_symlinks_inside_the_rom_root(rom_root: Path) -> None:
    """A default.xex symlink is followed when it stays inside ROM_ROOT."""
    shared = rom_root / "SharedAssets"
    real_xex = _touch(shared / "actual.xex")
    game = rom_root / "game"
    game.mkdir()
    (game / "default.xex").symlink_to(real_xex)

    assert xenia.Xenia().resolve_rom_file(game) == game / "default.xex"


def test_resolve_refuses_a_default_xex_that_symlinks_outside_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """A default.xex symlink that escapes ROM_ROOT is refused."""
    outside = tmp_path / "outside.xex"
    outside.write_bytes(b"xex")
    game = rom_root / "game"
    game.mkdir()
    (game / "default.xex").symlink_to(outside)

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_refuses_a_dangling_default_xex_symlink(rom_root: Path) -> None:
    """A default.xex symlink pointing nowhere is refused, not a fallback."""
    game = rom_root / "game"
    game.mkdir()
    (game / "default.xex").symlink_to(rom_root / "does-not-exist")

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_refuses_a_default_xex_symlink_to_a_non_regular_file_outside_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """A default.xex symlink to a non-regular file outside ROM_ROOT is refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.mkfifo(outside / "pipe")
    game = rom_root / "game"
    game.mkdir()
    (game / "default.xex").symlink_to(outside / "pipe")

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_prefers_an_iso_over_a_stray_xex(rom_root: Path) -> None:
    """An .iso outranks a loose .xex in the same folder."""
    game = rom_root / "game"
    _touch(game / "update.xex")
    _touch(game / "Game.iso")

    assert xenia.Xenia().resolve_rom_file(game).name == "Game.iso"


def test_resolve_picks_disc_one_of_a_multi_disc_folder(rom_root: Path) -> None:
    """The lowest disc number wins when a folder holds multiple discs."""
    game = rom_root / "game"
    _touch(game / "Game (Disc 2).iso")
    _touch(game / "Game (Disc 1).iso")

    assert xenia.Xenia().resolve_rom_file(game).name == "Game (Disc 1).iso"


def test_resolve_searches_one_level_into_a_folder(rom_root: Path) -> None:
    """A ROM one directory level deep is still found."""
    _touch(rom_root / "game" / "inner" / "Game.iso")

    assert xenia.Xenia().resolve_rom_file(rom_root / "game").name == "Game.iso"


def test_resolve_ignores_unbootable_and_hidden_files(rom_root: Path) -> None:
    """Files with an unrecognized extension or a leading dot are skipped."""
    game = rom_root / "game"
    _touch(game / "readme.txt")
    _touch(game / ".Game.iso")

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """A ROM candidate that symlinks outside ROM_ROOT is refused."""
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    game = rom_root / "game"
    game.mkdir()
    (game / "linked.iso").symlink_to(outside)

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_gives_up_on_a_path_that_is_not_there(rom_root: Path) -> None:
    """A path that does not exist resolves to None."""
    assert xenia.Xenia().resolve_rom_file(rom_root / "gone") is None


def _container(path: Path, magic: bytes = b"LIVE") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic + b"\x00" * 60)
    return path


def test_resolve_finds_the_xbla_package_under_a_full_content_tree(rom_root: Path) -> None:
    """An XBLA package is found under a full Content/<XUID>/<TITLE_ID> tree."""
    # The layout as the console writes it and as RomM holds it, with the
    # game's own folder on top.
    game = rom_root / "DOOM"
    pkg = _container(
        game / "Content" / "0000000000000000" / "58410824" / "000D0000"
        / "5BE22631DA178A036A01DC57A30D1326FF562F1F58"
    )

    assert xenia.Xenia().resolve_rom_file(game) == pkg


def test_resolve_finds_the_package_when_the_title_id_is_the_root(rom_root: Path) -> None:
    """A package is found when the title ID folder is handed over bare."""
    game = rom_root / "58410960"
    pkg = _container(game / "000D0000" / "F3B26E77DCA7E3BE683193FC5F6AB46F70FE6A5E58")

    assert xenia.Xenia().resolve_rom_file(game) == pkg


def test_resolve_finds_a_games_on_demand_install(rom_root: Path) -> None:
    """A Games on Demand install is found, and its .data payload is ignored."""
    game = rom_root / "Halo 3"
    pkg = _container(game / "Content" / "0000000000000000" / "4D5307E6" / "00007000" / "ABCDEF", b"PIRS")
    # The payload fragments live in a sibling directory named after the
    # package; they are not the thing to boot.
    _touch(pkg.parent / "ABCDEF.data" / "Data0000")

    assert xenia.Xenia().resolve_rom_file(game) == pkg


def test_resolve_leaves_dlc_and_title_updates_alone(rom_root: Path) -> None:
    """DLC and title-update content types are not treated as bootable."""
    game = rom_root / "game"
    title = game / "Content" / "0000000000000000" / "58410824"
    _container(title / "00000002" / "DLCPACK")
    _container(title / "000B0000" / "TU_1")

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_refuses_a_file_in_the_right_place_with_the_wrong_magic(rom_root: Path) -> None:
    """A file in a content-type folder without an STFS magic is refused."""
    game = rom_root / "game"
    _container(game / "58410824" / "000D0000" / "README", b"hello")

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_prefers_an_executable_or_disc_over_a_container(rom_root: Path) -> None:
    """A disc image or executable outranks an STFS content package."""
    game = rom_root / "game"
    _container(game / "58410824" / "000D0000" / "PKG")
    iso = _touch(game / "Game.iso")

    assert xenia.Xenia().resolve_rom_file(game) == iso


# ── Launch ───────────────────────────────────────────────────────────────────


def _spawned(monkeypatch: pytest.MonkeyPatch, rom: Path, resume_slot: Optional[int] = None) -> list[str]:
    calls = []
    monkeypatch.setattr(xenia.Xenia, "_spawn", lambda self, cmd, env: calls.append(cmd))
    xenia.Xenia().launch(rom, resume_slot)
    assert len(calls) == 1
    return calls[0]


def test_launch_runs_headless_fullscreen_against_the_broker_storage_root(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, data_dir: Path
) -> None:
    """The launch command line is headless, fullscreen, and storage-rooted."""
    rom = _touch(rom_root / "Game.iso")

    cmd = _spawned(monkeypatch, rom)

    assert cmd[0] == xenia.XENIA_BIN
    assert "--fullscreen" in cmd
    # Guest dialogs (storage select, sign-in) are auto-answered; nobody in
    # the stream could dismiss them otherwise.
    assert "--headless" in cmd
    assert f"--storage_root={data_dir}" in cmd
    assert "--discord=false" in cmd
    assert cmd[-1] == str(rom)


def test_launch_creates_the_storage_root(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, data_dir: Path
) -> None:
    """Launching creates the storage root directory if it does not exist."""
    rom = _touch(rom_root / "Game.iso")
    assert not data_dir.exists()

    _spawned(monkeypatch, rom)

    assert data_dir.is_dir()


def test_launch_ignores_a_resume_slot_because_there_are_no_states(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, data_dir: Path
) -> None:
    """A resume slot is logged, not passed on the command line, since there are no states."""
    rom = _touch(rom_root / "Game.iso")

    cmd = _spawned(monkeypatch, rom, resume_slot=3)

    assert "3" not in cmd
    assert not xenia.Xenia.supports_states


def test_the_save_archive_is_the_content_tree(data_dir: Path) -> None:
    """The save root and subtrees cover the whole content tree, saves and profile alike."""
    # Saves are keyed by the profile XUID, and the profile lives under
    # content/ too, so the two have to travel together.
    emu = xenia.Xenia()

    assert emu.save_root == data_dir
    assert emu.save_subtrees == ("content",)


def test_launch_records_the_session_baseline(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, data_dir: Path
) -> None:
    """Launching stamps the baseline the exit restamp scopes itself by."""
    rom = _touch(rom_root / "Game.iso")
    emu = xenia.Xenia()
    monkeypatch.setattr(xenia.Xenia, "_spawn", lambda self, cmd, env: None)
    assert emu._session_start == float("inf")

    before = time.time()
    emu.launch(rom, None)

    assert before <= emu._session_start <= time.time()


# ── Exit restamp ─────────────────────────────────────────────────────────────

_XUID = "0000000000000000"
_TITLE = "58410824"


def _content_file(path: Path, mtime: Optional[float] = None, content: bytes = b"save") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _started(data_dir: Path) -> xenia.Xenia:
    emu = xenia.Xenia()
    emu._session_start = time.time() - 100
    (data_dir / "content").mkdir(parents=True, exist_ok=True)
    return emu


def test_exit_ships_a_saved_title_whole_including_its_header_sidecar(data_dir: Path) -> None:
    """A title written this session has its untouched header restamped with it.

    The header is written once when the save is created, so an mtime-scoped
    dump would ship the save data without the record that names it.
    """
    emu = _started(data_dir)
    title = data_dir / "content" / _XUID / _TITLE
    _content_file(title / "00000001" / "SAVEGAME" / "savedata.bin")
    header = _content_file(
        title / "Headers" / "00000001" / "SAVEGAME", mtime=emu._session_start - 500
    )

    emu.save_and_exit(None)

    assert header.stat().st_mtime >= emu._session_start


def test_exit_leaves_a_title_this_session_never_wrote_alone(data_dir: Path) -> None:
    """Another title's content keeps its stamps and stays out of the dump."""
    emu = _started(data_dir)
    touched = data_dir / "content" / _XUID / _TITLE
    _content_file(touched / "00000001" / "SAVEGAME" / "savedata.bin")
    dlc = _content_file(
        data_dir / "content" / _XUID / "4D5307E6" / "00000002" / "PACK" / "dlc.bin",
        mtime=emu._session_start - 500,
    )

    emu.save_and_exit(None)

    assert dlc.stat().st_mtime < emu._session_start


def test_exit_ships_the_profile_with_a_title_that_saved(data_dir: Path) -> None:
    """The profile travels with the saves, since save paths embed its XUID."""
    emu = _started(data_dir)
    title = data_dir / "content" / _XUID / _TITLE
    _content_file(title / "00000001" / "SAVEGAME" / "savedata.bin")
    profile = _content_file(
        data_dir / "content" / _XUID / "FFFE07D1" / "00010000" / "Account",
        mtime=emu._session_start - 500,
    )

    emu.save_and_exit(None)

    assert profile.stat().st_mtime >= emu._session_start


def test_exit_leaves_the_profile_alone_when_no_title_saved(data_dir: Path) -> None:
    """A session that wrote no save does not drag the profile into the dump."""
    emu = _started(data_dir)
    profile = _content_file(
        data_dir / "content" / _XUID / "FFFE07D1" / "00010000" / "Account",
        mtime=emu._session_start - 500,
    )

    emu.save_and_exit(None)

    assert profile.stat().st_mtime < emu._session_start


def test_exit_without_a_launch_restamps_nothing(data_dir: Path) -> None:
    """An exit that never saw a launch must not claim every title's content.

    A zero baseline is newer than every file on disk, which would drag
    unrelated titles into this session's dump.
    """
    (data_dir / "content").mkdir(parents=True)
    old = _content_file(
        data_dir / "content" / _XUID / _TITLE / "00000001" / "SAVEGAME" / "savedata.bin",
        mtime=time.time() - 5000,
    )
    before = old.stat().st_mtime

    xenia.Xenia().save_and_exit(None)

    assert old.stat().st_mtime == before


def test_exit_skips_content_dirs_that_are_not_an_xuid_and_title_id_pair(data_dir: Path) -> None:
    """Only `<16 hex>/<8 hex>` pairs are save data; the rest of the root is not."""
    emu = _started(data_dir)
    stray = _content_file(
        data_dir / "content" / "shaders" / "cache" / "old.bin",
        mtime=emu._session_start - 500,
    )
    _content_file(data_dir / "content" / "shaders" / "cache" / "new.bin")
    named = _content_file(
        data_dir / "content" / _XUID / "not-a-title-id" / "old.bin",
        mtime=emu._session_start - 500,
    )
    _content_file(data_dir / "content" / _XUID / "not-a-title-id" / "new.bin")

    emu.save_and_exit(None)

    assert stray.stat().st_mtime < emu._session_start
    assert named.stat().st_mtime < emu._session_start


def test_exit_survives_a_content_tree_that_cannot_be_listed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A content tree that fails to list is logged, not raised through the exit path."""
    emu = _started(data_dir)

    def boom(self: Path) -> None:
        raise OSError("storage root went away")

    monkeypatch.setattr(Path, "iterdir", boom)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.xenia"):
        report = emu.save_and_exit(None)

    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
    assert any("could not list the content tree" in r.getMessage() for r in caplog.records)


def test_exit_survives_a_title_dir_that_cannot_be_walked(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A title dir that fails to walk is logged and stepped over, not raised."""
    emu = _started(data_dir)
    _content_file(data_dir / "content" / _XUID / _TITLE / "00000001" / "SAVEGAME" / "savedata.bin")

    def boom(self: Path, pattern: str) -> None:
        raise OSError("save vanished")

    monkeypatch.setattr(Path, "rglob", boom)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.xenia"):
        report = emu.save_and_exit(None)

    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
    assert any("could not walk" in r.getMessage() for r in caplog.records)


def test_exit_reports_no_state(data_dir: Path) -> None:
    """Exit reports that Xenia has no save state to offer."""
    assert xenia.Xenia().save_and_exit(None) == {
        "state_saved": None,
        "state_slot": None,
        "state_file": None,
    }


# ── Stale save data ──────────────────────────────────────────────────────────


def test_clearing_the_working_slot_drops_the_last_players_save(data_dir: Path) -> None:
    """A previous player's saved game must not survive into this session.

    The restore only writes the members this player's archive names, so an
    untouched leftover would still be in the content tree at exit, where the
    restamp ships its title whole into this player's own archive.
    """
    title = data_dir / "content" / _XUID / _TITLE
    save = _content_file(title / "00000001" / "SAVEGAME" / "savedata.bin")
    header = _content_file(title / "Headers" / "00000001" / "SAVEGAME")

    xenia.Xenia().clear_working_slot()

    assert not save.exists()
    assert not header.exists()
    assert not (title / "00000001").exists()


def test_clearing_the_working_slot_drops_another_accounts_saves_too(data_dir: Path) -> None:
    """A save under another XUID or another title is another player's data just the same."""
    other = _content_file(
        data_dir / "content" / "1111111111111111" / "4D5307E6" / "00000001" / "SAVE" / "s.bin"
    )

    xenia.Xenia().clear_working_slot()

    assert not other.exists()


def test_clearing_the_working_slot_keeps_installed_dlc_and_title_updates(data_dir: Path) -> None:
    """DLC and title updates share the title dir with the saves but are the game, not player data."""
    title = data_dir / "content" / _XUID / _TITLE
    _content_file(title / "00000001" / "SAVEGAME" / "savedata.bin")
    dlc = _content_file(title / "00000002" / "PACK" / "dlc.bin")
    update = _content_file(title / "000B0000" / "TU_1")

    xenia.Xenia().clear_working_slot()

    assert dlc.exists()
    assert update.exists()


def test_clearing_the_working_slot_keeps_the_whole_profile_package(data_dir: Path) -> None:
    """The profile survives the clear, whatever content type it holds.

    Save paths embed the profile XUID, and Xenia Edge with no profile stops on
    a native dialog nobody in the stream can dismiss, so clearing it would
    leave the next session unable to launch at all.
    """
    profile = data_dir / "content" / _XUID / "FFFE07D1"
    account = _content_file(profile / "00010000" / "Account")
    save_shaped = _content_file(profile / "00000001" / "gamerpicture")

    xenia.Xenia().clear_working_slot()

    assert account.exists()
    assert save_shaped.exists()


def test_clearing_the_working_slot_leaves_non_save_trees_alone(data_dir: Path) -> None:
    """Only `<16 hex>/<8 hex>` pairs are save data; caches under the same root are not."""
    cache = _content_file(data_dir / "content" / "shaders" / "00000001" / "cache.bin")
    named = _content_file(data_dir / "content" / _XUID / "not-a-title-id" / "00000001" / "x.bin")

    xenia.Xenia().clear_working_slot()

    assert cache.exists()
    assert named.exists()


def test_clearing_the_working_slot_without_a_content_tree_does_nothing(data_dir: Path) -> None:
    """A container that has never run Xenia has no content tree, and that is not an error."""
    xenia.Xenia().clear_working_slot()

    assert not (data_dir / "content").exists()


def test_clearing_the_working_slot_logs_save_data_it_cannot_remove(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A save dir that cannot be removed is reported, never silently left in place."""
    _content_file(data_dir / "content" / _XUID / _TITLE / "00000001" / "SAVEGAME" / "savedata.bin")

    def boom(path: Path) -> NoReturn:
        raise OSError("read-only file system")

    monkeypatch.setattr(xenia.shutil, "rmtree", boom)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.xenia"):
        xenia.Xenia().clear_working_slot()

    assert any("could not clear stale save data" in r.getMessage() for r in caplog.records)


def test_clearing_the_working_slot_survives_a_content_tree_that_cannot_be_listed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A content tree that fails to list is logged, not raised through activate."""
    _content_file(data_dir / "content" / _XUID / _TITLE / "00000001" / "SAVEGAME" / "savedata.bin")

    def boom(self: Path) -> NoReturn:
        raise OSError("storage root went away")

    monkeypatch.setattr(Path, "iterdir", boom)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.xenia"):
        xenia.Xenia().clear_working_slot()

    assert any("could not list the content tree" in r.getMessage() for r in caplog.records)


def test_clearing_the_working_slot_reports_a_title_dir_it_cannot_list(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A title dir that fails to list is logged, so a save left behind is never silent."""
    _content_file(data_dir / "content" / _XUID / _TITLE / "00000001" / "SAVEGAME" / "savedata.bin")
    listing = Path.iterdir

    def selective(self: Path) -> Iterator[Path]:
        if self.name == _TITLE:
            raise OSError("title dir went away")
        return listing(self)

    monkeypatch.setattr(Path, "iterdir", selective)

    with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.xenia"):
        xenia.Xenia().clear_working_slot()

    assert any("an earlier session's saves may survive" in r.getMessage() for r in caplog.records)


def test_xenia_declares_that_it_clears_stale_saves() -> None:
    """The activate contract's flag has to match what clear_working_slot actually does."""
    assert xenia.Xenia.clears_stale_saves is True


def test_the_profile_survives_a_clear_that_no_restore_follows(data_dir: Path) -> None:
    """A session activated with no archive still finds a profile to sign in with.

    Xenia Edge with no profile stops on a native "No Profiles Found" dialog
    nobody in the stream can dismiss, so a clear that took the package with the
    saves would leave the next player looking at it.
    """
    profile = _content_file(data_dir / "content" / _XUID / "FFFE07D1" / "00010000" / "Account")
    stale = _content_file(
        data_dir / "content" / _XUID / _TITLE / "00000001" / "SAVEGAME" / "savedata.bin"
    )

    xenia.Xenia().clear_working_slot()

    assert profile.exists()
    assert not stale.exists()


# ── Profile restore ──────────────────────────────────────────────────────────

_PROFILE_REL = f"content/{_XUID}/FFFE07D1/00010000/Account"
_SAVE_REL = f"content/{_XUID}/{_TITLE}/00000001/SAVEGAME/savedata.bin"
# Zip entries carry a DOS timestamp with no room for anything before 1980, so
# an archive taken "last session" is dated rather than offset from now.
_ARCHIVED = (2020, 1, 1, 0, 0, 0)


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=_ARCHIVED), content)
    return buf.getvalue()


def _restore(emu: xenia.Xenia, root: Path, members: dict[str, bytes]) -> dict[str, Any]:
    """Run the activate restore over `members` the way the route wires it up."""
    return saves.extract_save_archive(
        _zip(members), root, emu.save_subtrees, (), emu.always_restore
    )


def test_a_restore_lands_the_incoming_profile_over_the_last_players(data_dir: Path) -> None:
    """Player B's archived profile replaces player A's leftover, older or not.

    A's profile is the one file the clear cannot take out, and it carries the
    fresh mtime A's exit restamp gave it. B's own profile was archived at the
    end of B's previous session, so it is older: under the newer-file guard the
    restore passes it over and B plays, and dumps, as A.
    """
    emu = xenia.Xenia()
    profile = _content_file(data_dir / _PROFILE_REL, mtime=time.time(), content=b"player-a")

    emu.clear_working_slot()
    result = _restore(emu, data_dir, {_PROFILE_REL: b"player-b"})

    assert result["error"] is None
    assert (result["written"], result["skipped"]) == (1, 0)
    assert profile.read_bytes() == b"player-b"


def test_a_restore_still_refuses_to_roll_back_a_newer_save(data_dir: Path) -> None:
    """The exemption reaches the profile only; a newer save on disk still wins.

    Both members are older than what is on disk, so nothing but the exemption
    separates them.
    """
    emu = xenia.Xenia()
    save = _content_file(data_dir / _SAVE_REL, mtime=time.time(), content=b"newer")
    profile = _content_file(data_dir / _PROFILE_REL, mtime=time.time(), content=b"player-a")

    result = _restore(emu, data_dir, {_SAVE_REL: b"older", _PROFILE_REL: b"player-b"})

    assert (result["written"], result["skipped"]) == (1, 1)
    assert save.read_bytes() == b"newer"
    assert profile.read_bytes() == b"player-b"


def test_a_restored_profile_does_not_ride_forward_into_the_next_players_archive(
    data_dir: Path,
) -> None:
    """The identity the exit restamp ships is the one this session restored.

    The restamp drags the profile along with any title that saved, so a
    profile the restore had passed over would leave again in this player's own
    dump and reach whoever plays next.
    """
    emu = xenia.Xenia()
    _content_file(data_dir / _PROFILE_REL, mtime=time.time(), content=b"player-a")

    emu.clear_working_slot()
    _restore(emu, data_dir, {_PROFILE_REL: b"player-b"})
    emu._session_start = time.time()
    _content_file(data_dir / _SAVE_REL)
    emu.save_and_exit(None)

    profile = data_dir / _PROFILE_REL
    assert profile.stat().st_mtime >= emu._session_start
    assert profile.read_bytes() == b"player-b"


@pytest.mark.parametrize(
    "rel, exempt",
    [
        (_PROFILE_REL, True),
        (f"content/{_XUID}/fffe07d1/00010000/Account", True),
        (_SAVE_REL, False),
        (f"content/{_XUID}/FFFE07D1", False),
        (f"content/{_XUID}/{_TITLE}/00000001/FFFE07D1/save.bin", False),
        ("content/not-a-xuid/FFFE07D1/00010000/Account", False),
        (f"config/{_XUID}/FFFE07D1/00010000/Account", False),
    ],
)
def test_only_profile_members_skip_the_newer_file_guard(rel: str, exempt: bool) -> None:
    """Only a file under `content/<XUID>/FFFE07D1` is exempt from the guard."""
    assert xenia.Xenia().always_restore(rel) is exempt
