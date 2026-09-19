"""Tests for DuckStation ROM resolution, settings.ini patching, resume-state handling, and exit-on-save."""

import os
from pathlib import Path, PurePosixPath
from typing import NoReturn, Optional

import pytest
from fastapi.testclient import TestClient

from webstation_broker import imports
from webstation_broker.emulators import duckstation

from .conftest import PREFIX, import_zip, preflight_import, restore_import


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ROM_ROOT at an isolated temporary directory."""
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(duckstation, "ROM_ROOT", root)
    return root


@pytest.fixture
def duckstation_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point DuckStation's data, ini, savestate and memory card paths at temp directories.

    Args:
        monkeypatch: Pytest's attribute patcher.
        tmp_path: The test's temp directory.

    Returns:
        The patched paths, by name. Only the data dir is created.
    """
    data_dir = tmp_path / "data"
    ini_path = data_dir / "settings.ini"
    sstate_dir = data_dir / "savestates"
    memcard_dir = data_dir / "memcards"
    data_dir.mkdir()

    monkeypatch.setattr(duckstation, "DATA_DIR", data_dir)
    monkeypatch.setattr(duckstation, "INI_PATH", ini_path)
    monkeypatch.setattr(duckstation, "SSTATE_DIR", sstate_dir)
    monkeypatch.setattr(duckstation, "MEMCARD_DIR", memcard_dir)
    monkeypatch.setattr(duckstation.Duckstation, "save_root", data_dir)
    return {
        "data_dir": data_dir,
        "ini_path": ini_path,
        "sstate_dir": sstate_dir,
        "memcard_dir": memcard_dir,
    }


def _touch(path: Path, mtime: Optional[float] = None, content: bytes = b"state") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ── resolve_rom_file / _pick_rom_file ───────────────────────────────────


def test_resolve_takes_a_direct_file_as_given(rom_root: Path) -> None:
    """Resolve returns a direct file path unchanged."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(rom) == rom


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(rom_root: Path) -> None:
    """Resolve returns None for a path that is neither a file nor a folder."""
    missing = rom_root / "nope"

    assert duckstation.Duckstation().resolve_rom_file(missing) is None


def test_resolve_prefers_m3u_over_a_raw_bin_beside_it(rom_root: Path) -> None:
    """Resolve prefers an .m3u playlist over a raw .bin in the same folder."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "MyGame.bin").write_bytes(b"")
    m3u = folder / "MyGame.m3u"
    m3u.write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) == m3u


def test_resolve_prefers_disc_1_over_disc_2_at_the_same_extension_rank(rom_root: Path) -> None:
    """Resolve prefers Disc 1 over Disc 2 when both share the same extension rank."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    disc1 = folder / "MyGame (Disc 1).cue"
    disc1.write_bytes(b"")
    (folder / "MyGame (Disc 2).cue").write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) == disc1


def test_resolve_ignores_dotfiles(rom_root: Path) -> None:
    """Resolve ignores dotfiles when picking a ROM from a folder."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / ".hidden.chd").write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) is None


def test_resolve_ignores_extensions_it_does_not_recognize(rom_root: Path) -> None:
    """Resolve ignores files with extensions it does not recognize."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) is None


def test_resolve_refuses_a_disc_image_that_symlinks_outside_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """Resolve refuses a disc image whose symlink escapes the ROM root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.chd"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "MyGame.chd").symlink_to(secret)

    assert duckstation.Duckstation().resolve_rom_file(folder) is None


def test_resolve_accepts_a_disc_image_that_symlinks_inside_the_rom_root(rom_root: Path) -> None:
    """Resolve accepts a disc image symlinked to another location inside the ROM root."""
    shared = rom_root / "SharedAssets"
    shared.mkdir()
    real = shared / "actual.chd"
    real.write_bytes(b"game data")
    folder = rom_root / "MyGame"
    folder.mkdir()
    link = folder / "MyGame.chd"
    link.symlink_to(real)

    assert duckstation.Duckstation().resolve_rom_file(folder) == real


def test_resolve_searches_one_level_of_subfolders(rom_root: Path) -> None:
    """Resolve finds a ROM nested one level of subfolders deep."""
    folder = rom_root / "MyGame"
    sub = folder / "disc"
    sub.mkdir(parents=True)
    rom = sub / "MyGame.chd"
    rom.write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) == rom


# ── settings.ini patching ───────────────────────────────────────────────


def test_patch_ini_seeds_a_missing_file_with_every_forced_key(duckstation_dirs: dict[str, Path]) -> None:
    """Patching a missing ini file seeds it with every forced key."""
    duckstation._patch_ini()

    text = duckstation.INI_PATH.read_text()
    assert "SetupWizardIncomplete = false" in text
    assert "ConfirmPowerOff = false" in text
    assert "SaveStateOnExit = true" in text
    assert "CreateSaveStateBackups = false" in text
    assert "[AutoUpdater]" in text
    assert "CheckAtStartup = false" in text
    assert "[MemoryCards]" in text
    assert "Card1Type = Shared" in text
    assert "Card1Path = shared_card_1.mcd" in text
    assert "Directory = memcards" in text
    assert "[Folders]" in text
    assert "SaveStates = savestates" in text


def test_patch_ini_pins_memory_card_1_to_the_shared_card(duckstation_dirs: dict[str, Path]) -> None:
    """A per-game card type and a custom card path are both replaced; other card settings stay.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
    """
    duckstation.INI_PATH.write_text(
        "[MemoryCards]\n"
        "Card1Type = PerGameTitle\n"
        "Card1Path = /elsewhere/card.mcd\n"
        "Card2Type = None\n"
    )

    duckstation._patch_ini()

    text = duckstation.INI_PATH.read_text()
    assert "Card1Type = Shared" in text
    assert "Card1Path = shared_card_1.mcd" in text
    assert "PerGameTitle" not in text
    assert "/elsewhere/card.mcd" not in text
    assert "Card2Type = None" in text


def test_patch_ini_overwrites_a_conflicting_value_but_keeps_the_rest(
    duckstation_dirs: dict[str, Path]
) -> None:
    """Patching overwrites a conflicting forced value while leaving other settings intact."""
    duckstation.INI_PATH.write_text(
        "[Main]\n"
        "SaveStateOnExit = false\n"
        "SomeOtherSetting = 5\n"
    )

    duckstation._patch_ini()

    text = duckstation.INI_PATH.read_text()
    assert "SaveStateOnExit = true" in text
    assert "SomeOtherSetting = 5" in text


def test_patch_ini_adds_a_missing_key_into_an_existing_section(duckstation_dirs: dict[str, Path]) -> None:
    """Patching adds a missing forced key into an existing section."""
    duckstation.INI_PATH.write_text("[Main]\nSomeOtherSetting = 5\n")

    duckstation._patch_ini()

    lines = duckstation.INI_PATH.read_text().splitlines()
    assert "[Main]" in lines
    assert "ConfirmPowerOff = false" in lines


def test_patch_ini_adds_a_missing_section_entirely(duckstation_dirs: dict[str, Path]) -> None:
    """Patching adds an entirely missing section along with its forced keys."""
    duckstation.INI_PATH.write_text("[Main]\nSetupWizardIncomplete = false\n")

    duckstation._patch_ini()

    text = duckstation.INI_PATH.read_text()
    assert "[AutoUpdater]" in text
    assert "CheckAtStartup = false" in text


def test_patch_ini_raises_and_leaves_the_existing_file_untouched(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed patch aborts the launch and leaves the existing ini as it was.

    Proceeding would run the session with SaveStateOnExit off, so the whole
    session's progress is lost at exit with only a log line to say why.
    """
    original = "[Main]\nSetupWizardIncomplete = false\nSomeOtherSetting = 5\n"
    duckstation.INI_PATH.write_text(original)
    real_write_text = Path.write_text

    def guarded(self: Path, *a: object, **kw: object) -> int:
        if self.suffix == ".tmp":
            raise OSError("disk full")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", guarded)

    with pytest.raises(RuntimeError, match="broker settings"):
        duckstation._patch_ini()

    assert duckstation.INI_PATH.read_text() == original


# ── memory card migration ────────────────────────────────────────────────


def test_migration_copies_the_newest_per_game_card_to_the_pinned_card(
    duckstation_dirs: dict[str, Path]
) -> None:
    """An older archive's newest per-game card becomes the pinned card, and every source stays.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
    """
    cards = duckstation_dirs["memcard_dir"]
    old = _touch(cards / "Old_1.mcd", mtime=1000, content=b"old card")
    new = _touch(cards / "New_1.mcd", mtime=2000, content=b"new card")

    duckstation._migrate_memory_card()

    assert (cards / "shared_card_1.mcd").read_bytes() == b"new card"
    assert old.read_bytes() == b"old card"
    assert new.read_bytes() == b"new card"
    assert sorted(p.name for p in cards.iterdir()) == ["New_1.mcd", "Old_1.mcd", "shared_card_1.mcd"]


def test_migration_leaves_an_existing_pinned_card_alone(duckstation_dirs: dict[str, Path]) -> None:
    """A pinned card already there is the player's current one, whatever per-game card sits beside it.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
    """
    cards = duckstation_dirs["memcard_dir"]
    _touch(cards / "shared_card_1.mcd", mtime=1000, content=b"pinned")
    _touch(cards / "Game_1.mcd", mtime=2000, content=b"per-game")

    duckstation._migrate_memory_card()

    assert (cards / "shared_card_1.mcd").read_bytes() == b"pinned"


@pytest.mark.parametrize("make_dir", [False, True])
def test_migration_without_a_per_game_card_does_nothing(
    duckstation_dirs: dict[str, Path], make_dir: bool
) -> None:
    """No memcards dir, or one holding only a slot 2 card, leaves no pinned card behind.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        make_dir: Whether the memcards dir exists, holding a slot 2 card.
    """
    cards = duckstation_dirs["memcard_dir"]
    if make_dir:
        _touch(cards / "Game_2.mcd", content=b"slot 2")

    duckstation._migrate_memory_card()

    assert not (cards / "shared_card_1.mcd").exists()


def test_migration_skips_a_card_it_cannot_read(
    duckstation_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """A dangling card link is logged and passed over; the readable card is still carried over.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        caplog: Pytest's log capture.
    """
    cards = duckstation_dirs["memcard_dir"]
    _touch(cards / "Game (USA)_1.mcd", content=b"card")
    (cards / "Broken_1.mcd").symlink_to(cards / "missing.mcd")

    duckstation._migrate_memory_card()

    assert (cards / "shared_card_1.mcd").read_bytes() == b"card"
    assert "could not read memory card" in caplog.text


@pytest.mark.parametrize("with_card", [True, False])
def test_migration_passes_over_a_directory_named_like_a_card(
    duckstation_dirs: dict[str, Path], with_card: bool
) -> None:
    """A directory named like a card is never copied, and never stops a launch.

    An archive member below `X_1.mcd/` makes such a directory. It is the
    newest entry here, so only skipping it lets the older real card through.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        with_card: Whether a real, older per-game card sits beside the directory.
    """
    cards = duckstation_dirs["memcard_dir"]
    if with_card:
        _touch(cards / "Y_1.mcd", mtime=1000, content=b"card")
    _touch(cards / "X_1.mcd" / "foo", content=b"not a card")
    os.utime(cards / "X_1.mcd", (2000, 2000))

    duckstation._migrate_memory_card()

    pinned = cards / "shared_card_1.mcd"
    if with_card:
        assert pinned.read_bytes() == b"card"
    else:
        assert not pinned.exists()


def test_migration_that_cannot_copy_raises_and_leaves_no_partial_card(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed copy stops the launch rather than boot on a blank card, and leaves no torn file.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        monkeypatch: Pytest's attribute patcher.
        caplog: Pytest's log capture.
    """
    cards = duckstation_dirs["memcard_dir"]
    _touch(cards / "Game (USA)_1.mcd", content=b"card")

    def torn_copy(src: Path, dst: Path) -> NoReturn:
        """Write part of the card, then fail the way a full disk would.

        Args:
            src: The source card; unused.
            dst: The file being written.

        Raises:
            OSError: Always.
        """
        Path(dst).write_bytes(b"ca")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(duckstation.shutil, "copyfile", torn_copy)
    pattern = r"could not carry memory card Game \(USA\)_1\.mcd over to shared_card_1\.mcd"

    with pytest.raises(RuntimeError, match=pattern):
        duckstation._migrate_memory_card()

    assert sorted(p.name for p in cards.iterdir()) == ["Game (USA)_1.mcd"]
    assert "could not carry memory card" in caplog.text


@pytest.mark.parametrize("with_card", [True, False])
def test_migration_does_not_take_a_directory_at_the_pinned_path_for_a_card(
    duckstation_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, with_card: bool
) -> None:
    """A directory at the pinned card's path is no card, and it is never removed.

    With a per-game card to carry, the copy cannot replace the directory,
    so the launch stops rather than boot with no card mounted. Without one
    there is nothing to carry, and the directory is left as it was.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        caplog: Pytest's log capture.
        with_card: Whether a per-game card sits beside the directory.
    """
    cards = duckstation_dirs["memcard_dir"]
    _touch(cards / "shared_card_1.mcd" / "inside", content=b"kept")
    if with_card:
        _touch(cards / "Game_1.mcd", content=b"card")
        pattern = r"could not carry memory card Game_1\.mcd over to shared_card_1\.mcd"
        with pytest.raises(RuntimeError, match=pattern):
            duckstation._migrate_memory_card()
    else:
        duckstation._migrate_memory_card()

    pinned = cards / "shared_card_1.mcd"
    assert [p.name for p in pinned.iterdir()] == ["inside"]
    assert (pinned / "inside").read_bytes() == b"kept"
    assert not (cards / "shared_card_1.mcd.tmp").exists()
    assert "treating the pinned card as absent" in caplog.text


def test_migration_replaces_a_link_to_a_directory_at_the_pinned_path(
    duckstation_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """A link to a directory is no card either; the carried card replaces the link, not what it points at.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        caplog: Pytest's log capture.
    """
    cards = duckstation_dirs["memcard_dir"]
    target = _touch(duckstation_dirs["data_dir"] / "elsewhere" / "inside", content=b"kept").parent
    _touch(cards / "Game_1.mcd", content=b"card")
    (cards / "shared_card_1.mcd").symlink_to(target)

    duckstation._migrate_memory_card()

    pinned = cards / "shared_card_1.mcd"
    assert not pinned.is_symlink()
    assert pinned.read_bytes() == b"card"
    assert [p.name for p in target.iterdir()] == ["inside"]
    assert "treating the pinned card as absent" in caplog.text


# ── resume state snapshot / diff ────────────────────────────────────────


def test_resume_snapshot_is_empty_without_a_savestates_dir(duckstation_dirs: dict[str, Path]) -> None:
    """Resume snapshot is empty when the savestates directory does not exist."""
    assert duckstation._resume_snapshot() == {}


def test_changed_resume_state_picks_the_newest_of_several_writes(
    duckstation_dirs: dict[str, Path]
) -> None:
    """Among several states written this session, the highest mtime wins."""
    before = duckstation._resume_snapshot()
    _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav", mtime=1000)
    newest = _touch(duckstation.SSTATE_DIR / "SLUS-00002_resume.sav", mtime=3000)

    assert duckstation._changed_resume_state(before) == newest


def test_changed_resume_state_ignores_another_games_untouched_state(
    duckstation_dirs: dict[str, Path]
) -> None:
    """A state already on disk and not rewritten is never claimed as this session's.

    The savestates directory is shared across titles, so picking the newest
    file outright hands DuckStation another game's state on the next resume.
    """
    stale = _touch(duckstation.SSTATE_DIR / "SLUS-90001_resume.sav", mtime=9000)
    before = duckstation._resume_snapshot()

    assert duckstation._changed_resume_state(before) is None

    mine = _touch(duckstation.SSTATE_DIR / "SLUS-00002_resume.sav", mtime=1000)
    # Lower mtime than the untouched one, but it is the only file this
    # session actually wrote.
    assert duckstation._changed_resume_state(before) == mine
    assert stale.exists()


def test_changed_resume_state_is_none_with_no_resume_files(
    duckstation_dirs: dict[str, Path]
) -> None:
    """Changed resume state is None when no resume files exist."""
    assert duckstation._changed_resume_state({}) is None


def test_changed_resume_state_finds_the_file_that_appeared(duckstation_dirs: dict[str, Path]) -> None:
    """Changed resume state finds the file that newly appeared."""
    before = duckstation._resume_snapshot()
    new = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    assert duckstation._changed_resume_state(before) == new


def test_changed_resume_state_finds_a_rewritten_file_by_size(duckstation_dirs: dict[str, Path]) -> None:
    """Changed resume state finds a file rewritten to a different size."""
    p = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    before = duckstation._resume_snapshot()
    p.write_bytes(b"a longer state than before")

    assert duckstation._changed_resume_state(before) == p


def test_changed_resume_state_is_none_when_nothing_moved(duckstation_dirs: dict[str, Path]) -> None:
    """Changed resume state is None when nothing in the snapshot changed."""
    _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    before = duckstation._resume_snapshot()

    assert duckstation._changed_resume_state(before) is None


# ── clear_working_slot ──────────────────────────────────────────────────


def test_clear_working_slot_is_a_noop_without_a_savestates_dir(duckstation_dirs: dict[str, Path]) -> None:
    """Clearing the working slot is a no-op when no savestates directory exists."""
    duckstation.Duckstation().clear_working_slot()  # must not raise

    assert not duckstation.SSTATE_DIR.exists()


def test_clear_working_slot_wipes_every_leftover_resume_state(duckstation_dirs: dict[str, Path]) -> None:
    """A stale resume state from another session must not outrank a fresh archive restore."""
    stale_a = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    stale_b = _touch(duckstation.SSTATE_DIR / "SLUS-00002_resume.sav")

    duckstation.Duckstation().clear_working_slot()

    assert not stale_a.exists()
    assert not stale_b.exists()
    assert duckstation.SSTATE_DIR.is_dir()


def test_clear_working_slot_wipes_every_leftover_save(duckstation_dirs: dict[str, Path]) -> None:
    """Nothing in a save subtree is another session's to inherit, whatever it is named."""
    card = _touch(duckstation_dirs["data_dir"] / "memcards" / "shared_card_1.mcd")
    unnamed = _touch(duckstation.SSTATE_DIR / "notes.txt")
    nested = _touch(duckstation.SSTATE_DIR / "backup" / "SLUS-00001_resume.sav")

    duckstation.Duckstation().clear_working_slot()

    assert not card.exists()
    assert not unnamed.exists()
    assert not nested.parent.exists()


def test_clear_working_slot_keeps_a_card_the_memory_route_just_synced(
    duckstation_dirs: dict[str, Path],
) -> None:
    """The card is hydrated before activate, so a clear that took it would drop it."""
    card = _touch(duckstation_dirs["data_dir"] / "memcards" / "shared_card_1.mcd")
    state = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    duckstation.Duckstation().clear_working_slot(("memcards",))

    assert card.exists()
    assert not state.exists()


def test_clear_working_slot_keeps_a_quarantined_state(duckstation_dirs: dict[str, Path]) -> None:
    """A state set aside as possibly torn is evidence, and no resume can pick it up."""
    aside = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav.untrusted")
    marker = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav.untrusted.rom")
    stale = _touch(duckstation.SSTATE_DIR / "SLUS-00002_resume.sav")

    duckstation.Duckstation().clear_working_slot()

    assert aside.exists()
    assert marker.exists()
    assert not stale.exists()


def test_clear_working_slot_tolerates_a_file_it_cannot_delete(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Clearing the working slot tolerates and logs a file it cannot delete."""
    stuck = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    def boom(self: Path) -> NoReturn:
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", boom)

    with caplog.at_level("WARNING"):
        duckstation.Duckstation().clear_working_slot()  # must not raise

    assert stuck.exists()
    assert "could not clear stale save data" in caplog.text


# ── launch ───────────────────────────────────────────────────────────────


def test_launch_stops_then_patches_ini_then_migrates_the_card_then_spawns(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch stops any running instance, patches the ini, carries the card over, then spawns.

    The card is carried over after the patch, so the settings that mount it
    are in place, and before the spawn, so DuckStation boots on it.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
        monkeypatch: Pytest's attribute patcher.
    """
    order: list[str] = []
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: order.append("stop"))
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: order.append("patch_ini"))
    monkeypatch.setattr(duckstation, "_migrate_memory_card", lambda: order.append("migrate_card"))

    def fake_spawn(
        self: duckstation.Duckstation, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        """Record the spawn's place in the order instead of starting DuckStation.

        Args:
            self: The emulator.
            cmd: The argv; unused.
            env: The environment; unused.
            stdin_pipe: Unused; matches `_spawn`.
        """
        order.append("spawn")

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    duckstation.Duckstation().launch(rom, resume_slot=None)

    assert order == ["stop", "patch_ini", "migrate_card", "spawn"]


def test_launch_does_not_boot_on_a_card_it_could_not_carry_over(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed card copy fails the launch; DuckStation would otherwise mount a blank card.

    The exit dump would then ship that blank card as the player's.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
        monkeypatch: Pytest's attribute patcher.
    """
    spawned: list[list[str]] = []
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)

    def failed_migration() -> NoReturn:
        """Fail the way a card copy onto a full disk would.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("could not carry memory card Game_1.mcd over to shared_card_1.mcd: disk full")

    def fake_spawn(
        self: duckstation.Duckstation, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        """Record a spawn that must not happen.

        Args:
            self: The emulator.
            cmd: The argv.
            env: The environment; unused.
            stdin_pipe: Unused; matches `_spawn`.
        """
        spawned.append(cmd)

    monkeypatch.setattr(duckstation, "_migrate_memory_card", failed_migration)
    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    with pytest.raises(RuntimeError, match="could not carry memory card"):
        duckstation.Duckstation().launch(rom, resume_slot=None)

    assert spawned == []


def test_launch_with_no_resume_slot_omits_statefile(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch without a resume slot omits the -statefile argument."""
    monkeypatch.delenv("DUCKSTATION_BIN", raising=False)
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    spawned = {}

    def fake_spawn(
        self: duckstation.Duckstation, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    duckstation.Duckstation().launch(rom, resume_slot=None)

    assert spawned["cmd"] == [
        "/opt/duckstation/AppRun",
        "-batch",
        "-fullscreen",
        "--",
        str(rom),
    ]


def test_the_data_root_ignores_the_xdg_data_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no XDG_CONFIG_HOME the root is DuckStation's own fallback, not the data home.

    Probed against the container's build: a run with only XDG_DATA_HOME set
    still wrote to `$HOME/.local/share/duckstation`, so following the data
    variable would leave the broker patching a settings.ini nothing opens.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert duckstation._data_root() == tmp_path / "home" / ".local/share" / "duckstation"


def test_the_data_root_follows_the_xdg_config_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DuckStation picks its root from XDG_CONFIG_HOME despite what the tree holds."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    assert duckstation._data_root() == tmp_path / "cfg" / "duckstation"


def test_a_launch_sends_duckstation_to_the_data_root_the_broker_uses(
    duckstation_dirs: dict[str, Path], rom_root: Path,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch env lands DuckStation on the tree the broker patches and reads.

    Nothing on the command line names the data root, so the exported root is
    the only thing keeping the patched settings.ini and the resume state the
    exit reads back in the same tree this launch writes.
    """
    data_dir = tmp_path / "xdg" / "duckstation"
    monkeypatch.setattr(duckstation, "DATA_DIR", data_dir)
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    spawned = {}

    def fake_spawn(
        self: duckstation.Duckstation, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["env"] = env

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    duckstation.Duckstation().launch(rom, resume_slot=None)

    monkeypatch.setenv("XDG_CONFIG_HOME", spawned["env"]["XDG_CONFIG_HOME"])
    assert duckstation._data_root() == data_dir


def test_launch_with_a_resume_slot_boots_the_newest_resume_state(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch with a resume slot boots the newest resume state via -statefile."""
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    state = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    spawned = {}

    def fake_spawn(
        self: duckstation.Duckstation, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    duckstation.Duckstation().launch(rom, resume_slot=1)

    assert "-statefile" in spawned["cmd"]
    assert str(state) in spawned["cmd"]
    assert spawned["cmd"][-2:] == ["--", str(rom)]


def test_launch_with_a_resume_slot_but_no_state_boots_fresh(
    duckstation_dirs: dict[str, Path],
    rom_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Launch with a resume slot but no resume state boots fresh and logs a warning."""
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    spawned = {}

    def fake_spawn(
        self: duckstation.Duckstation, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    with caplog.at_level("WARNING"):
        duckstation.Duckstation().launch(rom, resume_slot=1)

    assert "-statefile" not in spawned["cmd"]
    assert "resume requested but no resume state" in caplog.text


# ── save_and_exit ────────────────────────────────────────────────────────


def test_exit_without_a_slot_reports_nothing_but_still_stops(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exiting without a slot reports nothing saved but still stops the process."""
    stopped = []
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: stopped.append(True))
    emu = duckstation.Duckstation()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert stopped == [True]


def test_exit_with_a_slot_reports_the_state_the_shutdown_wrote(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exiting with a slot reports the resume state the shutdown wrote."""
    written = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    class FakeProc:
        returncode = 0  # a normal, non-SIGKILL exit

    def fake_stop(self: duckstation.Duckstation) -> None:
        # Simulates the graceful shutdown writing a fresh resume state.
        written.write_bytes(b"a fresh resume state")

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert report["state_slot"] == 1
    assert report["state_file"]["path"] == str(written)


def test_exit_with_a_slot_reports_no_save_when_no_new_state_appears(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Exiting with a slot reports no save when no new resume state appears."""

    class FakeProc:
        returncode = 0  # a normal, non-SIGKILL exit

    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert "no resume state written during shutdown" in caplog.text


def test_exit_with_a_slot_sets_aside_a_state_from_a_force_killed_process(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """SIGKILL escalation (term_timeout exceeded) can cut the resume state write off mid-flight.

    Such a file must not just go unreported: the save archive dump sweeps up anything with a
    fresh mtime regardless of what this method reports, so it has to leave the resume path.
    It is only suspected of being torn, though, and can be the player's only copy, so it is
    renamed to a sidecar rather than deleted.
    """
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        returncode = -9  # -signal.SIGKILL

    def fake_stop(self: duckstation.Duckstation) -> None:
        # Simulates a partial write landing before the kill lands.
        _touch(written, content=b"maybe torn, maybe a whole session")

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not written.exists()
    aside = written.with_name(written.name + duckstation.UNTRUSTED_SUFFIX)
    assert aside.read_bytes() == b"maybe torn, maybe a whole session"
    assert "force-killed" in caplog.text
    assert "set aside untrusted resume state" in caplog.text


@pytest.mark.parametrize("returncode", [-15, -6, -11])
def test_exit_with_a_slot_sets_aside_a_state_from_a_process_killed_by_any_signal(
    returncode: int,
    duckstation_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any signal death, not just the SIGKILL escalation, can cut the resume state write short.

    A negative returncode is the POSIX marker for that. SIGTERM's own graceful shutdown counts:
    stop() escalates to an OS-level SIGKILL once term_timeout expires, and the wait then reports
    the signal that actually landed.
    """
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        pass

    proc = FakeProc()
    proc.returncode = returncode

    def fake_stop(self: duckstation.Duckstation) -> None:
        # Simulates a partial write landing before the signal lands.
        _touch(written)

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = proc
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not written.exists()
    assert written.with_name(written.name + duckstation.UNTRUSTED_SUFFIX).exists()
    assert "force-killed" in caplog.text


@pytest.mark.parametrize("returncode", [0, 1])
def test_exit_with_a_slot_trusts_a_state_from_a_process_that_exited_on_its_own(
    returncode: int, duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-negative returncode means the shutdown ran to completion, so its write is kept."""
    written = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    class FakeProc:
        pass

    proc = FakeProc()
    proc.returncode = returncode

    def fake_stop(self: duckstation.Duckstation) -> None:
        written.write_bytes(b"a fresh resume state")

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = proc
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert written.exists()


def test_exit_without_a_slot_still_sets_aside_a_state_from_a_killed_process(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The set-aside is independent of the slot: the dump sweeps by mtime whatever this reports."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        returncode = -15

    def fake_stop(self: duckstation.Duckstation) -> None:
        _touch(written)

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert not written.exists()
    assert written.with_name(written.name + duckstation.UNTRUSTED_SUFFIX).exists()


def test_exit_with_a_slot_sets_aside_a_state_when_stop_never_confirms_the_exit(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """proc.returncode left None by a timed-out wait is treated as a confirmed kill, not trusted."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        returncode = None

    def fake_stop(self: duckstation.Duckstation) -> None:
        _touch(written)

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not written.exists()
    assert written.with_name(written.name + duckstation.UNTRUSTED_SUFFIX).exists()


def test_exit_with_a_slot_but_not_alive_reports_nothing(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No process running means SIGTERM's shutdown write can never happen, so there is nothing to diff for."""
    _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    emu = duckstation.Duckstation()
    monkeypatch.setattr(emu, "alive", lambda: False)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


# ── untrusted sidecars ───────────────────────────────────────────────────


def _kill_after_writing(
    monkeypatch: pytest.MonkeyPatch, state: Path, content: bytes = b"state"
) -> duckstation.Duckstation:
    """Build an emulator whose stop() writes `state` and then reports a SIGKILL."""

    class FakeProc:
        returncode = -9  # -signal.SIGKILL

    def fake_stop(self: duckstation.Duckstation) -> None:
        _touch(state, content=content)

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)
    return emu


def test_a_set_aside_state_is_replaced_rather_than_piling_up(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second force-killed exit for the same serial replaces the earlier sidecar."""
    state = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"
    aside = state.with_name(state.name + duckstation.UNTRUSTED_SUFFIX)
    _touch(aside, content=b"from the last kill")

    _kill_after_writing(monkeypatch, state, b"from this kill").save_and_exit(1)

    assert aside.read_bytes() == b"from this kill"
    assert sorted(p.name for p in duckstation.SSTATE_DIR.iterdir()) == [aside.name]


def test_a_set_aside_state_survives_a_restore_and_is_invisible_to_a_resume(
    duckstation_dirs: dict[str, Path], rom_root: Path
) -> None:
    """A sidecar outlives clear_working_slot and is never offered back as a resume state."""
    state = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"
    aside = _touch(state.with_name(state.name + duckstation.UNTRUSTED_SUFFIX), content=b"kept")
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    duckstation.Duckstation().clear_working_slot()

    assert aside.read_bytes() == b"kept"
    assert duckstation._resume_snapshot() == {}
    assert duckstation._resume_state_for(rom) is None


def test_a_set_aside_state_takes_its_owner_marker_with_it(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker left behind would claim the next state DuckStation writes under that serial."""
    state = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"
    _touch(duckstation._owner_marker(state), content=b"/romm/psx/game.chd\n")

    _kill_after_writing(monkeypatch, state).save_and_exit(1)

    aside = state.with_name(state.name + duckstation.UNTRUSTED_SUFFIX)
    assert not duckstation._owner_marker(state).exists()
    assert duckstation._owner_marker(aside).read_text() == "/romm/psx/game.chd\n"


def test_a_state_that_cannot_be_set_aside_is_left_alone_and_logged(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rename that fails leaves the state in place rather than losing it, and is logged."""
    state = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"
    emu = _kill_after_writing(monkeypatch, state, b"still the player's only copy")

    def boom(self: Path, target: Path) -> NoReturn:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "replace", boom)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report["state_saved"] is False
    assert state.read_bytes() == b"still the player's only copy"
    assert "could not set aside untrusted resume state" in caplog.text


def test_save_file_kind_keeps_markers_and_sidecars_out_of_the_state_picker(
    duckstation_dirs: dict[str, Path]
) -> None:
    """Neither a marker nor a sidecar is loadable, so RomM must not offer either as a state."""
    emu = duckstation.Duckstation()

    assert emu.save_file_kind("savestates/SLUS-00001_resume.sav") == "state"
    assert emu.save_file_kind("savestates/SLUS-00001_resume.sav.rom") == "save"
    assert emu.save_file_kind("savestates/SLUS-00001_resume.sav.untrusted") == "save"
    assert emu.save_file_kind("savestates/SLUS-00001_resume.sav.untrusted.rom") == "save"


# ── owner markers ────────────────────────────────────────────────────────


def test_launch_records_the_disc_the_exits_marker_will_name(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the disc recorded at launch the exit has nothing to mark its state with."""
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    monkeypatch.setattr(
        duckstation.Duckstation,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: None,
    )
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    emu = duckstation.Duckstation()
    emu.launch(rom, resume_slot=None)

    assert emu._rom_path == rom


def test_a_graceful_exit_marks_its_state_as_belonging_to_the_booted_disc(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker, not the filename, is what a later resume matches on."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: _touch(written))
    emu = duckstation.Duckstation()
    emu._rom_path = rom
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    assert emu.save_and_exit(1)["state_saved"] is True
    assert duckstation._state_owner(written) == str(rom.resolve())
    assert duckstation._resume_state_for(rom) == written


def test_a_graceful_exit_marks_its_state_even_with_no_slot_requested(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DuckStation writes the state either way and the dump ships it, so it needs an owner either way."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: _touch(written))
    emu = duckstation.Duckstation()
    emu._rom_path = rom
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert duckstation._state_owner(written) == str(rom.resolve())


def test_an_unmarkable_exit_is_logged_and_leaves_the_state_usable(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No disc on the instance costs the state its marker, never the save itself."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: _touch(written))
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert not duckstation._owner_marker(written).exists()
    assert "no rom recorded for this session" in caplog.text


def test_resume_picks_the_state_marked_for_this_disc_out_of_several(
    duckstation_dirs: dict[str, Path], rom_root: Path
) -> None:
    """A flat savestates directory shared by every title is exactly what the marker disambiguates."""
    mine = rom_root / "mine.chd"
    mine.write_bytes(b"")
    theirs = rom_root / "theirs.chd"
    theirs.write_bytes(b"")
    wanted = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav", mtime=1000)
    other = _touch(duckstation.SSTATE_DIR / "SLUS-00002_resume.sav", mtime=9000)
    duckstation._write_owner_marker(wanted, mine)
    duckstation._write_owner_marker(other, theirs)

    assert duckstation._resume_state_for(mine) == wanted
    assert duckstation._resume_state_for(theirs) == other


def test_resume_refuses_a_lone_state_marked_for_another_disc(
    duckstation_dirs: dict[str, Path], rom_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Booting clean costs a resume; handing DuckStation another game's state costs the save."""
    mine = rom_root / "mine.chd"
    mine.write_bytes(b"")
    theirs = rom_root / "theirs.chd"
    theirs.write_bytes(b"")
    state = _touch(duckstation.SSTATE_DIR / "SLUS-00002_resume.sav")
    duckstation._write_owner_marker(state, theirs)

    with caplog.at_level("ERROR"):
        assert duckstation._resume_state_for(mine) is None

    assert "none is marked for" in caplog.text


def test_resume_still_takes_a_lone_unmarked_state(
    duckstation_dirs: dict[str, Path], rom_root: Path
) -> None:
    """Archives written before markers existed carry a single unmarked state and must still resume."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    assert duckstation._resume_state_for(rom) == state


def test_clear_working_slot_drops_a_marker_along_with_its_state(
    duckstation_dirs: dict[str, Path]
) -> None:
    """A marker outliving its state would claim the next state written under the same serial."""
    state = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    marker = _touch(duckstation._owner_marker(state), content=b"/romm/psx/game.chd\n")

    duckstation.Duckstation().clear_working_slot()

    assert not state.exists()
    assert not marker.exists()


# ── state_path: the exit state RomM files in its library ────────────────


def _exit_gracefully_after_writing(
    monkeypatch: pytest.MonkeyPatch, state: Path
) -> duckstation.Duckstation:
    """Build an emulator whose stop() writes `state` and then reports a clean exit."""

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: _touch(state))
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)
    return emu


def test_state_path_serves_the_resume_state_a_saving_exit_confirmed(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it the state-file GET 404s and RomM never files the exit state."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"
    emu = _exit_gracefully_after_writing(monkeypatch, written)

    assert emu.save_and_exit(10)["state_saved"] is True
    assert emu.state_path() == written


def test_state_path_is_empty_before_any_exit(duckstation_dirs: dict[str, Path]) -> None:
    """A state on disk before the exit came in with the archive and may be another disc's."""
    _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    assert duckstation.Duckstation().state_path() is None


def test_state_path_is_empty_after_an_exit_that_asked_for_no_state(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit report leaves that state unreported, so the GET must not hand it over either."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"
    emu = _exit_gracefully_after_writing(monkeypatch, written)

    emu.save_and_exit(None)

    assert written.exists()
    assert emu.state_path() is None


def test_state_path_is_empty_after_a_force_killed_exit(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state the kill may have torn must not reach RomM's library as a good one."""
    emu = _kill_after_writing(monkeypatch, duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    emu.save_and_exit(10)

    assert emu.state_path() is None


def test_state_path_is_empty_once_the_confirmed_state_is_gone(
    duckstation_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing file is an empty slot, so the GET answers 404 rather than 500."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"
    emu = _exit_gracefully_after_writing(monkeypatch, written)
    emu.save_and_exit(10)

    written.unlink()

    assert emu.state_path() is None


def test_a_launch_forgets_the_last_exits_state(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next session has confirmed nothing yet, so a mid-session GET must not serve the old exit state."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"
    emu = _exit_gracefully_after_writing(monkeypatch, written)
    emu.save_and_exit(10)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    monkeypatch.setattr(
        duckstation.Duckstation,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: None,
    )
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    emu.launch(rom, resume_slot=None)

    assert written.exists()
    assert emu.state_path() is None


# ── class attributes (API surface parity with the other exit-only emulators) ──


def test_save_subtrees_cover_both_memcards_and_savestates(duckstation_dirs: dict[str, Path]) -> None:
    """Dropping either save_subtrees entry would silently stop shipping that data to RomM."""
    emu = duckstation.Duckstation()

    assert emu.save_subtrees == ("memcards", "savestates")
    assert emu.rom_extensions == (
        ".m3u", ".chd", ".cue", ".pbp", ".ccd", ".mds",
        ".iso", ".img", ".ecm", ".bin", ".exe", ".psexe",
    )


# ── declared imports ─────────────────────────────────────────────────────


_CARD_BYTES = 131072
"""A raw memory card's size, spelled out so the tests do not lean on the module's constant."""

_CARD_SHAPE = f"raw {_CARD_BYTES}-byte .mcd/.mcr/.mc/.srm card"
"""How the spec describes a card, under either kind."""

_ROMM_SERIAL = imports.RomRef(1, "Game", "psx", title_id="SLUS-00594")
"""An activate's rom, carrying the serial RomM holds for it."""

_DECLARE_STATE = "a DuckStation save state: declare it as kind state"
"""The detail a state declared as a save is refused with."""


def _disc(rom_root: Path, name: str = "Game (USA).chd") -> Path:
    """Write a stand-in disc image under the rom root.

    Args:
        rom_root: The patched rom root.
        name: The image's file name.

    Returns:
        The image's path.
    """
    rom = rom_root / name
    rom.write_bytes(b"disc")
    return rom


def _wrong_size(size: int) -> str:
    """The detail a raw card of the wrong size is refused with.

    Args:
        size: The member's size.

    Returns:
        The detail.
    """
    return f"a raw memory card is {_CARD_BYTES} bytes, this one is {size}"


def _conversion(suffix: str) -> str:
    """The detail a card in a headered format is refused with.

    Args:
        suffix: The member's suffix, lower-cased.

    Returns:
        The detail.
    """
    return f"{suffix} is not a raw memory card; convert it to a raw {_CARD_BYTES}-byte .mcd"


def test_the_import_spec_declares_cards_and_one_archive_state(duckstation_dirs: dict[str, Path]) -> None:
    """DuckStation takes a raw card under either kind, and one state that rides the archive.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
    """
    spec = duckstation.Duckstation().import_spec()

    card = {"shapes": [_CARD_SHAPE], "requires_resume_slot": False, "max_members": None}
    assert spec.as_dict() == {
        "kinds": [
            {"kind": "save", **card},
            {
                "kind": "state",
                "shapes": ["<serial>_resume.sav", "<serial>_<n>.sav"],
                "requires_resume_slot": True,
                "max_members": 1,
            },
            {"kind": "memcard", **card},
        ],
        "state_channel": "archive",
        "card_subtree": None,
    }
    assert spec.protected == ("*.rom", "*.untrusted")
    state = spec.kind("state")
    assert state is not None and state.counts_v1 is True


@pytest.mark.parametrize(
    "name",
    [
        ".import/save/Game (USA)_1.mcd",
        ".import/save/EPSXE000.MCR",
        ".import/memcard/card.mc",
        ".import/memcard/Game (USA).srm",
    ],
)
def test_a_raw_card_lands_as_the_pinned_card(duckstation_dirs: dict[str, Path], name: str) -> None:
    """A raw card, under either kind and any raw suffix, becomes the one card DuckStation mounts.

    A RetroArch PlayStation `.srm` is the same raw 128 KiB card.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        name: The member's name.
    """
    body = import_zip({name: b"\0" * _CARD_BYTES})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=None)

    assert result.refusals == ()
    assert [(p.dest, p.sidecars) for p in result.placements] == [
        (PurePosixPath("memcards/shared_card_1.mcd"), ())
    ]


@pytest.mark.parametrize(
    ("name", "size", "reason", "detail"),
    [
        ("sub/card.mcd", _CARD_BYTES, "unrecognised_layout", "expected a single file"),
        ("card.mcd", 8192, "unrecognised_layout", _wrong_size(8192)),
        ("card.gme", 8192, "needs_conversion", _conversion(".gme")),
        ("card.PSM", 8192, "needs_conversion", _conversion(".psm")),
        ("card.vgs", _CARD_BYTES + 64, "needs_conversion", _conversion(".vgs")),
        ("SLUS-00594_resume.sav", 16, "unrecognised_layout", _DECLARE_STATE),
        ("Game.sav", 16, "unrecognised_layout", None),
    ],
)
def test_a_card_duckstation_cannot_mount_is_refused(
    duckstation_dirs: dict[str, Path], name: str, size: int, reason: str, detail: Optional[str]
) -> None:
    """A nested file, a wrong size, a headered format, a state or an unknown file is refused.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        name: The member's path below `.import/save/`.
        size: The member's size.
        reason: The refusal code.
        detail: The refusal's detail.
    """
    body = import_zip({f".import/save/{name}": b"\0" * size})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=None)

    assert [(r.reason, r.member, r.detail) for r in result.refusals] == [
        (reason, f".import/save/{name}", detail)
    ]
    assert result.placements == ()


@pytest.mark.parametrize(
    "name",
    [
        "SLUS-00594_resume.sav.rom",
        "SLUS-00594_resume.sav.untrusted",
        "SLUS-00594_resume.sav.untrusted.rom",
    ],
)
def test_a_save_may_not_overwrite_a_marker_or_a_set_aside_state(
    duckstation_dirs: dict[str, Path], name: str
) -> None:
    """Owner markers and set-aside states are the broker's, not the player's.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        name: The protected file name.
    """
    body = import_zip({f".import/save/{name}": b"x"})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=None)

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("protected_destination", f"savestates/{name} is emulator configuration")
    ]


def test_two_cards_in_one_import_are_both_refused(duckstation_dirs: dict[str, Path]) -> None:
    """DuckStation mounts one card, so two members landing on it are a conflict, not an overwrite.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
    """
    card = b"\0" * _CARD_BYTES
    body = import_zip({".import/memcard/a.mcd": card, ".import/save/b.mcr": card})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=None)

    assert sorted((r.reason, r.member) for r in result.refusals) == [
        ("destination_conflict", ".import/memcard/a.mcd"),
        ("destination_conflict", ".import/save/b.mcr"),
    ]


@pytest.mark.parametrize(
    ("archived", "carried"),
    [
        ("memcards/Game (USA)_1.mcd", "memcards/Game (USA)_1.mcd"),
        ("memcards/Game (USA)_2.mcd", "memcards/Game (USA)_2.mcd"),
        ("./memcards/Game (USA)_1.mcd", "memcards/Game (USA)_1.mcd"),
        ("memcards//Game (USA)_1.mcd", "memcards/Game (USA)_1.mcd"),
    ],
)
def test_a_card_beside_an_archived_per_game_card_is_refused(
    duckstation_dirs: dict[str, Path], archived: str, carried: str
) -> None:
    """An imported card beside an archived card, in either slot and any spelling, is refused.

    Once the imported card is the pinned one, an archived slot 1 card is
    never mounted again. A hand-built `./memcards/` entry extracts to the
    same file, so it is the same card.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        archived: The card's name in the archive.
        carried: The card the refusal names, as the path it extracts to.
    """
    card = b"\0" * _CARD_BYTES
    body = import_zip({".import/memcard/card.mcd": card}, v1={archived: card})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=None)

    detail = f"the archive already carries {carried}"
    assert [(r.reason, r.expected, r.detail) for r in result.refusals] == [
        ("destination_conflict", "one memory card per archive", detail)
    ]


@pytest.mark.parametrize(
    ("members", "v1", "detail"),
    [
        (
            (".import/memcard/a.mcd", ".import/save/b.mcr"),
            ("memcards/Game_1.mcd",),
            "another member lands on the same file",
        ),
        (
            (".import/memcard/a.mcd",),
            ("memcards/Game_1.mcd", "memcards/shared_card_1.mcd/foo"),
            "another member lands on a file this destination needs as a directory",
        ),
        (
            (".import/memcard/a.mcd",),
            ("memcards", "memcards/Game_1.mcd"),
            "another member lands on a file this destination needs as a directory",
        ),
    ],
)
def test_a_card_the_shared_check_refuses_is_not_refused_again(
    duckstation_dirs: dict[str, Path], members: tuple[str, ...], v1: tuple[str, ...], detail: str
) -> None:
    """A card that clashes with another member gets the shared refusal alone, beside an archived card too.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        members: The imported cards.
        v1: The archive's own members, a card among them.
        detail: The shared check's detail.
    """
    card = b"\0" * _CARD_BYTES
    body = import_zip(dict.fromkeys(members, card), v1=dict.fromkeys(v1, card))

    result = preflight_import(duckstation.Duckstation(), body, rom_file=None)

    assert sorted((r.reason, r.member, r.expected, r.detail) for r in result.refusals) == [
        ("destination_conflict", member, "one member per destination", detail) for member in members
    ]


@pytest.mark.parametrize("archived", ["memcards/shared_card_1.mcd", "./memcards/shared_card_1.mcd"])
def test_a_card_beside_an_archived_pinned_card_is_refused_once(
    duckstation_dirs: dict[str, Path], archived: str
) -> None:
    """A card the archive already holds at the pinned name is the shared collision, reported once.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        archived: The pinned card's name in the archive.
    """
    card = b"\0" * _CARD_BYTES
    body = import_zip({".import/memcard/card.mcd": card}, v1={archived: card})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=None)

    assert [(r.reason, r.expected, r.detail) for r in result.refusals] == [
        ("destination_conflict", "one member per destination", "another member lands on the same file")
    ]


def test_a_state_lands_as_the_session_s_resume_state_with_its_owner_marker(
    duckstation_dirs: dict[str, Path], rom_root: Path
) -> None:
    """The state is named for RomM's serial and marked as the booted disc's.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
    """
    rom = _disc(rom_root)
    body = import_zip({".import/state/SLUS-00594_resume.sav": b"progress"})

    emu = duckstation.Duckstation()
    result = preflight_import(emu, body, rom_file=rom, resume_slot=0, rom=_ROMM_SERIAL)

    assert result.refusals == ()
    (placement,) = result.placements
    assert placement.dest == PurePosixPath("savestates/SLUS-00594_resume.sav")
    assert placement.sidecars == (
        (PurePosixPath("savestates/SLUS-00594_resume.sav.rom"), f"{rom.resolve()}\n".encode()),
    )


@pytest.mark.parametrize(
    ("name", "rom", "dest"),
    [
        ("savestate_1.sav", _ROMM_SERIAL, "SLUS-00594_resume.sav"),
        ("Game (USA)_resume.sav", _ROMM_SERIAL, "SLUS-00594_resume.sav"),
        ("slus_005.94_resume.sav", None, "SLUS-00594_resume.sav"),
        ("SLUS-00594_3.sav", None, "SLUS-00594_resume.sav"),
        ("Game (USA)_resume.sav", None, "Game (USA)_resume.sav"),
        ("savestate_1.sav", None, "savestate_resume.sav"),
    ],
)
def test_a_state_is_named_for_romm_s_serial_then_its_own_then_its_base(
    duckstation_dirs: dict[str, Path], rom_root: Path, name: str, rom: Optional[imports.RomRef], dest: str
) -> None:
    """RomM's serial wins; without it, a serial in the member's name; failing that, the name's base.

    A numbered state becomes the resume state, since that is the only one
    the broker resumes.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
        name: The member's file name.
        rom: The activate's rom, or None when RomM sent none.
        dest: The name the state must land under in `savestates/`.
    """
    body = import_zip({f".import/state/{name}": b"x"})

    result = preflight_import(
        duckstation.Duckstation(), body, rom_file=_disc(rom_root), resume_slot=0, rom=rom
    )

    assert result.refusals == ()
    assert [p.dest for p in result.placements] == [PurePosixPath("savestates", dest)]


def test_a_state_named_for_another_serial_is_refused(
    duckstation_dirs: dict[str, Path], rom_root: Path
) -> None:
    """A state whose name carries another game's serial than RomM's is another game's state.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
    """
    body = import_zip({".import/state/SCUS-94163_resume.sav": b"x"})

    result = preflight_import(
        duckstation.Duckstation(), body, rom_file=_disc(rom_root), resume_slot=0, rom=_ROMM_SERIAL
    )

    detail = (
        "member SCUS-94163, session SLUS-00594 (from romm)"
        " - fix via PUT /api/roms/{id}/identity if RomM is wrong"
    )
    assert [(r.reason, r.detail) for r in result.refusals] == [("identity_mismatch", detail)]


@pytest.mark.parametrize(
    ("name", "data", "reason", "detail"),
    [
        ("Game.state1", b"x", "source_incompatible", "a RetroArch (libretro) state"),
        ("Game.srm", b"x", "source_incompatible", "a RetroArch save file"),
        ("SLUS-00594_resume.sav", b"", "incomplete_unit", "the file is empty"),
        ("Game.sav", b"x", "unrecognised_layout", None),
        ("sub/SLUS-00594_resume.sav", b"x", "unrecognised_layout", "expected a single file"),
    ],
)
def test_a_state_duckstation_cannot_resume_is_refused(
    duckstation_dirs: dict[str, Path],
    rom_root: Path,
    name: str,
    data: bytes,
    reason: str,
    detail: Optional[str],
) -> None:
    """A RetroArch file, an empty file, another name or a nested file is refused.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
        name: The member's path below `.import/state/`.
        data: The member's bytes.
        reason: The refusal code.
        detail: The refusal's detail.
    """
    body = import_zip({f".import/state/{name}": data})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=_disc(rom_root), resume_slot=0)

    assert [(r.reason, r.member, r.detail) for r in result.refusals] == [
        (reason, f".import/state/{name}", detail)
    ]


def test_a_state_with_no_rom_to_mark_it_for_is_refused(duckstation_dirs: dict[str, Path]) -> None:
    """Without a rom file there is no disc for the owner marker to name, so no resume would claim it.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
    """
    body = import_zip({".import/state/SLUS-00594_resume.sav": b"x"})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=None, resume_slot=0)

    assert [(r.reason, r.detail) for r in result.refusals] == [
        ("destination_unresolvable", "no rom file to mark the state for")
    ]


def test_a_state_whose_owner_marker_name_is_too_long_is_refused(
    duckstation_dirs: dict[str, Path], rom_root: Path
) -> None:
    """A state that fits the name limit but whose `.rom` marker does not is refused in preflight.

    With no serial from RomM or in its name, the state keeps its own base, so
    a long name reaches the marker. The marker is written only after the
    working slot is cleared, so a name only the marker overflows has to be
    caught before anything is touched.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
    """
    state = f"{'A' * 244}_resume.sav"
    assert len(state.encode()) == 255
    body = import_zip({f".import/state/{state}": b"x"})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=_disc(rom_root), resume_slot=0)

    assert result.placements == ()
    assert [(r.reason, r.member, r.detail) for r in result.refusals] == [
        (
            "unsafe_path",
            f".import/state/{state}",
            f"renamed to {state!r}: component longer than 251 bytes",
        )
    ]


def test_a_state_without_a_resume_slot_is_refused(duckstation_dirs: dict[str, Path], rom_root: Path) -> None:
    """Without `resume_slot` the state is never loaded, and the exit would overwrite it.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
    """
    body = import_zip({".import/state/SLUS-00594_resume.sav": b"x"})

    result = preflight_import(duckstation.Duckstation(), body, rom_file=_disc(rom_root), resume_slot=None)

    assert [r.reason for r in result.refusals] == ["resume_slot_required"]


def test_an_imported_state_beside_an_archived_one_is_refused(
    duckstation_dirs: dict[str, Path], rom_root: Path
) -> None:
    """DuckStation resumes one state; one in the archive already counts toward that one.

    The archived state's marker is protected, so it does not count as a second state.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
    """
    body = import_zip(
        {".import/state/SLUS-00594_resume.sav": b"x"},
        v1={
            "savestates/SCUS-94163_resume.sav": b"old",
            "savestates/SCUS-94163_resume.sav.rom": b"/romm/Other.chd\n",
        },
    )

    result = preflight_import(duckstation.Duckstation(), body, rom_file=_disc(rom_root), resume_slot=0)

    assert [(r.reason, r.expected, r.detail) for r in result.refusals] == [
        ("destination_conflict", "at most 1 state member(s)", "2 state members in the archive")
    ]


def test_discovery_names_the_slot_an_archive_state_resumes_through(client: TestClient) -> None:
    """DuckStation has no mid-session states, but RomM still needs the slot to send as resume_slot.

    Args:
        client: The app, served without a secret.
    """
    params = {"emulator": "duckstation", "platform": "psx"}
    response = client.get(f"{PREFIX}/api/session/import-spec", params=params)

    assert response.status_code == 200
    assert (response.json()["state_channel"], response.json()["state_slot"]) == ("archive", 0)


def test_an_imported_card_is_written_where_duckstation_mounts_it(duckstation_dirs: dict[str, Path]) -> None:
    """The restore writes the card as the pinned card, byte for byte.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
    """
    emu = duckstation.Duckstation()
    card = bytes(range(256)) * (_CARD_BYTES // 256)
    body = import_zip({".import/memcard/EPSXE000.MCR": card})

    report = restore_import(emu, body, preflight_import(emu, body, rom_file=None))

    assert (report["imported"], report["failed"]) == (1, 0)
    assert (duckstation_dirs["memcard_dir"] / "shared_card_1.mcd").read_bytes() == card


def test_an_imported_state_is_resumed_by_the_next_launch(
    duckstation_dirs: dict[str, Path], rom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import, restore and launch: the state and its marker land, and the launch passes it to DuckStation.

    Args:
        duckstation_dirs: The patched DuckStation dirs.
        rom_root: The patched rom root.
        monkeypatch: Pytest's attribute patcher.
    """
    emu = duckstation.Duckstation()
    rom = _disc(rom_root)
    body = import_zip({".import/state/savestate_1.sav": b"progress"})

    report = restore_import(
        emu, body, preflight_import(emu, body, rom_file=rom, resume_slot=0, rom=_ROMM_SERIAL)
    )

    state = duckstation_dirs["sstate_dir"] / "SLUS-00594_resume.sav"
    assert (report["imported"], report["failed"]) == (1, 0)
    assert state.read_bytes() == b"progress"
    assert duckstation._state_owner(state) == str(rom.resolve())

    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    spawned: dict[str, list[str]] = {}

    def fake_spawn(
        self: duckstation.Duckstation, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        """Record the command instead of starting DuckStation.

        Args:
            self: The emulator.
            cmd: The argv.
            env: The environment; unused.
            stdin_pipe: Unused; matches `_spawn`.
        """
        spawned["cmd"] = cmd

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)

    emu.launch(rom, resume_slot=0)

    cmd = spawned["cmd"]
    assert cmd[cmd.index("-statefile") + 1] == str(state)
