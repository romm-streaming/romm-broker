"""Azahar (3DS) ROM resolution, qt-config.ini patching, launch, and save-dump mtime restamping."""

import configparser
import fnmatch
import os
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, Optional, Tuple

import pytest

from webstation_broker import imports
from webstation_broker.emulators import azahar

from .conftest import import_zip, preflight_import, restore_import


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Provide an isolated ROM library root patched onto azahar.ROM_ROOT."""
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(azahar, "ROM_ROOT", root)
    return root


def test_class_declares_no_save_state_or_disc_swap_support() -> None:
    """Azahar declares neither save-state nor disc-swap support."""
    assert azahar.Azahar.supports_states is False
    assert azahar.Azahar.supports_disc_swap is False


# The XDG resolution these directories are built from is shared with the other
# launchers and covered in tests/test_emulators.py.

# ---- resolve_rom_file / _pick_rom_file ----


def test_resolve_takes_a_direct_file_as_given(rom_root: Path) -> None:
    """A direct file path is returned unchanged."""
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(rom) == rom


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(rom_root: Path) -> None:
    """A path that is neither a file nor a folder resolves to nothing."""
    missing = rom_root / "nope"

    assert azahar.Azahar().resolve_rom_file(missing) is None


def test_resolve_returns_none_when_the_folder_has_no_candidates(rom_root: Path) -> None:
    """An empty folder resolves to no ROM."""
    folder = rom_root / "Empty"
    folder.mkdir()

    assert azahar.Azahar().resolve_rom_file(folder) is None


def test_resolve_finds_a_rom_directly_inside_a_folder(rom_root: Path) -> None:
    """A ROM sitting directly inside a folder is found."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    rom = folder / "game.3ds"
    rom.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) == rom


def test_resolve_finds_a_rom_one_level_deeper_in_a_wrapper_folder(rom_root: Path) -> None:
    """A ROM nested one level deeper in a wrapper folder is found."""
    folder = rom_root / "MyGame"
    inner = folder / "disc"
    inner.mkdir(parents=True)
    rom = inner / "game.cxi"
    rom.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) == rom


def test_resolve_ignores_hidden_files(rom_root: Path) -> None:
    """Hidden files are not considered ROM candidates."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / ".game.3ds").write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) is None


def test_resolve_ignores_non_rom_extensions(rom_root: Path) -> None:
    """Files with a non-ROM extension are not considered candidates."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) is None


def test_resolve_prefers_the_earlier_extension_in_priority_order(rom_root: Path) -> None:
    """A ROM with a higher-priority extension is preferred over a lower one."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "game.cci").write_bytes(b"")
    threeds = folder / "game.3ds"
    threeds.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) == threeds


def test_resolve_deprioritizes_update_and_dlc_files(rom_root: Path) -> None:
    """An update or DLC file is ranked below the base game ROM."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "update.3ds").write_bytes(b"")
    base_rom = folder / "base.3ds"
    base_rom.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) == base_rom


def test_resolve_refuses_a_rom_that_symlinks_outside_rom_root(rom_root: Path, tmp_path: Path) -> None:
    """A ROM symlink that escapes ROM_ROOT is refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.3ds"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "game.3ds").symlink_to(secret)

    assert azahar.Azahar().resolve_rom_file(folder) is None


def test_resolve_accepts_a_rom_that_symlinks_inside_rom_root(rom_root: Path) -> None:
    """A ROM symlink that stays inside ROM_ROOT resolves to its real target."""
    shared = rom_root / "Shared"
    shared.mkdir()
    real = shared / "actual.3ds"
    real.write_bytes(b"game data")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "game.3ds").symlink_to(real)

    # _pick_rom_file ranks and returns the resolved real path, not the
    # symlink that was found; both point at the same bootable file.
    assert azahar.Azahar().resolve_rom_file(folder) == real


def test_resolve_keeps_candidates_when_one_search_pattern_fails(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable subdirectory is logged, and the other patterns' candidates still count."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    rom = folder / "game.3ds"
    rom.write_bytes(b"")
    real_glob = Path.glob

    def flaky_glob(self: Path, pattern: str) -> object:
        if pattern == "*/*":
            raise OSError("permission denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", flaky_glob)

    with caplog.at_level("WARNING"):
        resolved = azahar.Azahar().resolve_rom_file(folder)

    assert resolved == rom
    assert "search of" in caplog.text
    assert "permission denied" in caplog.text


def test_resolve_logs_when_every_search_pattern_fails(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A folder whose every search fails reports no ROM, but says why in the log."""
    folder = rom_root / "MyGame"
    folder.mkdir()

    def fail_glob(self: Path, pattern: str) -> NoReturn:
        raise OSError("stale file handle")

    monkeypatch.setattr(Path, "glob", fail_glob)

    with caplog.at_level("WARNING"):
        assert azahar.Azahar().resolve_rom_file(folder) is None

    assert "stale file handle" in caplog.text


def test_resolve_ignores_a_dangling_symlink(rom_root: Path) -> None:
    """A symlink pointing at a nonexistent target is not a candidate."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "game.3ds").symlink_to(rom_root / "does-not-exist")

    assert azahar.Azahar().resolve_rom_file(folder) is None


# ---- _patch_config ----


@pytest.fixture
def config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Provide an isolated qt-config.ini path patched onto azahar.CONFIG_PATH."""
    path = tmp_path / "azahar-config" / "qt-config.ini"
    monkeypatch.setattr(azahar, "CONFIG_PATH", path)
    return path


def _read_ini(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def test_patch_config_creates_missing_parent_directories(config_path: Path) -> None:
    """Patching creates the config file's missing parent directories."""
    assert not config_path.parent.exists()

    azahar._patch_config()

    assert config_path.exists()


def test_patch_config_seeds_a_missing_file_with_every_forced_key(config_path: Path) -> None:
    """A missing config file is seeded with every forced key."""
    azahar._patch_config()

    parser = _read_ini(config_path)
    assert parser["UI"]["confirmClose"] == "false"
    assert parser["UI"]["check_for_update_on_start"] == "false"
    assert parser["UI"]["enable_discord_presence"] == "false"


def test_patch_config_overwrites_a_conflicting_value_but_keeps_the_rest(config_path: Path) -> None:
    """A conflicting forced value is overwritten while other settings survive."""
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "[UI]\nconfirmClose=true\ncustomSetting=keepme\n\n[Other]\nfoo=bar\n"
    )

    azahar._patch_config()

    parser = _read_ini(config_path)
    assert parser["UI"]["confirmClose"] == "false"
    assert parser["UI"]["customSetting"] == "keepme"
    assert parser["Other"]["foo"] == "bar"


def test_patch_config_preserves_key_case(config_path: Path) -> None:
    """Patching preserves the case of existing keys."""
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[UI]\nMixedCaseKey=Value\n")

    azahar._patch_config()

    assert "MixedCaseKey" in config_path.read_text()


def test_patch_config_reseeds_a_file_that_fails_to_decode(config_path: Path) -> None:
    """A config file that fails to decode is reseeded from scratch."""
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b"\x80\x81\x82 not valid utf-8")

    azahar._patch_config()

    parser = _read_ini(config_path)
    assert parser["UI"]["confirmClose"] == "false"


def test_patch_config_raises_when_the_directory_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch, config_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure to create the config directory is logged and raised, not swallowed."""

    def fail_mkdir(*a: object, **k: object) -> NoReturn:
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with caplog.at_level("ERROR"), pytest.raises(OSError):
        azahar._patch_config()

    assert "refusing to launch" in caplog.text


def test_patch_config_raises_when_the_file_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, config_path: Path
) -> None:
    """A failure to write the patched config is raised rather than launched past."""
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[UI]\nconfirmClose=true\n")

    def fail_open(*a: object, **k: object) -> NoReturn:
        raise OSError("read-only file system")

    monkeypatch.setattr("builtins.open", fail_open)

    with pytest.raises(OSError):
        azahar._patch_config()


# ---- launch ----


def test_launch_stops_first_patches_config_and_spawns(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, config_path: Path
) -> None:
    """Launch stops any running instance, patches the config, then spawns."""
    order = []
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: order.append("stop"))
    patched = []
    real_patch = azahar._patch_config

    def tracking_patch() -> None:
        patched.append(True)
        real_patch()

    monkeypatch.setattr(azahar, "_patch_config", tracking_patch)
    spawned = {}

    def fake_spawn(self: azahar.Azahar, cmd: list[str], env: dict[str, str]) -> None:
        order.append("spawn")
        spawned["cmd"] = cmd
        spawned["env"] = env

    monkeypatch.setattr(azahar.Azahar, "_spawn", fake_spawn)
    monkeypatch.setenv("AZAHAR_BIN", "/opt/azahar/AppRun")
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")
    emu = azahar.Azahar()

    emu.launch(rom, resume_slot=None)

    assert order == ["stop", "spawn"]
    assert patched == [True]
    assert spawned["cmd"] == ["/opt/azahar/AppRun", "-w", str(rom)]


def test_launch_uses_the_default_binary_path_when_unset(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, config_path: Path
) -> None:
    """Launch falls back to the default binary path when AZAHAR_BIN is unset."""
    monkeypatch.delenv("AZAHAR_BIN", raising=False)
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        azahar.Azahar, "_spawn", lambda self, cmd, env: spawned.update(cmd=cmd)
    )
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")
    emu = azahar.Azahar()

    emu.launch(rom, resume_slot=None)

    assert spawned["cmd"][0] == "/opt/azahar/AppRun"


def test_launch_logs_and_ignores_a_resume_slot(
    monkeypatch: pytest.MonkeyPatch,
    rom_root: Path,
    config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A requested resume slot is logged and otherwise ignored."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    monkeypatch.setattr(azahar.Azahar, "_spawn", lambda self, cmd, env: None)
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")
    emu = azahar.Azahar()

    with caplog.at_level("INFO"):
        emu.launch(rom, resume_slot=4)

    assert "resume_slot 4 ignored" in caplog.text


def test_launch_logs_and_ignores_the_zero_resume_slot(
    monkeypatch: pytest.MonkeyPatch,
    rom_root: Path,
    config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slot 0 is a requested slot like any other, so it is logged rather than passed over."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    monkeypatch.setattr(azahar.Azahar, "_spawn", lambda self, cmd, env: None)
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")

    with caplog.at_level("INFO"):
        azahar.Azahar().launch(rom, resume_slot=0)

    assert "resume_slot 0 ignored" in caplog.text


def test_launch_does_not_spawn_when_the_config_patch_fails(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, config_path: Path
) -> None:
    """A failed config patch aborts the launch instead of booting on unpatched settings."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)

    def fail_patch() -> NoReturn:
        raise OSError("read-only file system")

    monkeypatch.setattr(azahar, "_patch_config", fail_patch)
    spawned = []
    monkeypatch.setattr(
        azahar.Azahar, "_spawn", lambda self, cmd, env: spawned.append(cmd)
    )
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")

    with pytest.raises(OSError):
        azahar.Azahar().launch(rom, resume_slot=None)

    assert spawned == []


def test_launch_records_the_session_start_time(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, config_path: Path
) -> None:
    """Launch records the session start time around the call."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    monkeypatch.setattr(azahar.Azahar, "_spawn", lambda self, cmd, env: None)
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")
    emu = azahar.Azahar()
    before = time.time()

    emu.launch(rom, resume_slot=None)

    assert before <= emu._session_start <= time.time()


def test_launch_uses_windowed_not_fullscreen(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, config_path: Path
) -> None:
    """Launch spawns Azahar windowed, never with a fullscreen flag."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        azahar.Azahar, "_spawn", lambda self, cmd, env: spawned.update(cmd=cmd)
    )
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")

    azahar.Azahar().launch(rom, resume_slot=None)

    assert "-w" in spawned["cmd"]
    assert "-f" not in spawned["cmd"]
    assert "--fullscreen" not in spawned["cmd"]


def test_launch_sends_azahar_to_the_directories_the_broker_uses(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, config_path: Path, tmp_path: Path
) -> None:
    """The spawned emulator resolves the same config and data roots the broker patches and dumps.

    Azahar's command line names neither, so the exported XDG roots are the
    whole of the agreement: let them drift and the broker patches a config
    Azahar never opens and dumps saves the session never wrote.
    """
    monkeypatch.setattr(azahar, "CONFIG_DIR", tmp_path / "cfg" / "azahar-emu")
    monkeypatch.setattr(azahar, "USER_DIR", tmp_path / "data" / "azahar-emu")
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    spawned: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(
        azahar.Azahar, "_spawn", lambda self, cmd, env: spawned.update(env=env)
    )
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")

    azahar.Azahar().launch(rom, resume_slot=None)

    env = spawned["env"]
    assert Path(env["XDG_CONFIG_HOME"]) / "azahar-emu" == azahar.CONFIG_DIR
    assert Path(env["XDG_DATA_HOME"]) / "azahar-emu" == azahar.USER_DIR


# ---- prepare_restore ----


def test_prepare_restore_stops_the_emulator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preparing a restore stops the running emulator."""
    stopped = []
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: stopped.append(True))

    azahar.Azahar().prepare_restore()

    assert stopped == [True]


# ---- clear_working_slot ----


def test_the_clear_empties_every_declared_save_subtree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each of the SD and NAND save trees is emptied before a restore.

    Azahar files a save under the title id alone, so the previous session's
    saves sit exactly where this one's belong, and the restore only writes the
    members the incoming archive names.
    """
    monkeypatch.setattr(azahar.Azahar, "save_root", tmp_path)
    stale = []
    for subtree in azahar.Azahar.save_subtrees:
        save = tmp_path / subtree / "00040000" / "00081e00" / "save.bin"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"last player")
        stale.append(save)

    azahar.Azahar().clear_working_slot()

    assert not any(s.exists() for s in stale)
    # The trees themselves are where the restore extracts to.
    assert all((tmp_path / s).is_dir() for s in azahar.Azahar.save_subtrees)


def test_the_clear_leaves_the_rest_of_the_data_root_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Config, cache and system titles are container setup, not one session's data."""
    monkeypatch.setattr(azahar.Azahar, "save_root", tmp_path)
    config = tmp_path / "config" / "qt-config.ini"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"settings")

    azahar.Azahar().clear_working_slot()

    assert config.exists()


# ---- _modified_title_saves / save_and_exit ----


@pytest.fixture
def save_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Tuple[Path, Path, Path, Path]:
    """Provide isolated save-group roots patched onto azahar._SAVE_GROUP_ROOTS."""
    sdmc_title = tmp_path / "sdmc_title"
    sdmc_extdata = tmp_path / "sdmc_extdata"
    nand_extdata = tmp_path / "nand_extdata"
    nand_sysdata = tmp_path / "nand_sysdata"
    for d in (sdmc_title, sdmc_extdata, nand_extdata, nand_sysdata):
        d.mkdir()
    roots = (sdmc_title, sdmc_extdata, nand_extdata, nand_sysdata)
    monkeypatch.setattr(azahar, "_SAVE_GROUP_ROOTS", roots)
    return roots


def test_modified_title_saves_includes_a_title_touched_this_session(
    save_roots: Tuple[Path, Path, Path, Path],
) -> None:
    """A title touched during the current session is included."""
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert emu._modified_title_saves() == [title]


def test_modified_title_saves_excludes_a_title_not_touched_this_session(
    save_roots: Tuple[Path, Path, Path, Path],
) -> None:
    """A title not touched during the current session is excluded."""
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    emu = azahar.Azahar()
    emu._session_start = time.time() + 10_000

    assert emu._modified_title_saves() == []


def test_modified_title_saves_skips_a_non_hex_title_high_dir(
    save_roots: Tuple[Path, Path, Path, Path],
) -> None:
    """A title-high directory that is not hex is skipped."""
    root = save_roots[0]
    title = root / "not-hex-8" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert emu._modified_title_saves() == []


def test_modified_title_saves_skips_a_non_hex_title_low_dir(
    save_roots: Tuple[Path, Path, Path, Path],
) -> None:
    """A title-low directory that is not hex is skipped."""
    root = save_roots[0]
    title = root / "00010032" / "not-hex-8"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert emu._modified_title_saves() == []


def test_modified_title_saves_ignores_a_missing_root(
    save_roots: Tuple[Path, Path, Path, Path],
) -> None:
    """A missing save-group root is ignored rather than raising."""
    for root in save_roots:
        root.rmdir()
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert emu._modified_title_saves() == []


def test_modified_title_saves_covers_every_save_group_root(
    save_roots: Tuple[Path, Path, Path, Path],
) -> None:
    """Touched titles are found across every save-group root."""
    titles = []
    for root in save_roots:
        title = root / "00010032" / "00040000"
        title.mkdir(parents=True)
        (title / "save.bin").write_bytes(b"data")
        titles.append(title)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert sorted(emu._modified_title_saves()) == sorted(titles)


def test_modified_title_saves_logs_when_a_save_root_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch,
    save_roots: Tuple[Path, Path, Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A save root that cannot be listed is logged, since its saves silently miss the dump."""

    def fail_iterdir(self: Path) -> NoReturn:
        raise OSError("stale file handle")

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    with caplog.at_level("WARNING"):
        assert emu._modified_title_saves() == []

    assert "could not list the save tree" in caplog.text


def test_modified_title_saves_logs_when_a_title_dir_cannot_be_scanned(
    monkeypatch: pytest.MonkeyPatch,
    save_roots: Tuple[Path, Path, Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A title dir that cannot be walked is logged rather than dropped in silence."""
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")

    def fail_rglob(self: Path, pattern: str) -> NoReturn:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    with caplog.at_level("WARNING"):
        assert emu._modified_title_saves() == []

    assert "could not scan the title save dir" in caplog.text


def test_instance_keeps_no_restamp_log(save_roots: Tuple[Path, Path, Path, Path]) -> None:
    """The exit route keeps no restamp log, so nothing promises a revert that does not exist."""
    emu = azahar.Azahar()

    assert not hasattr(emu, "_restamped")
    assert not hasattr(emu, "revert_restamps")


def test_save_and_exit_stops_the_emulator(
    monkeypatch: pytest.MonkeyPatch, save_roots: Tuple[Path, Path, Path, Path]
) -> None:
    """Saving and exiting stops the running emulator."""
    stopped = []
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: stopped.append(True))
    emu = azahar.Azahar()
    emu._session_start = 0.0

    emu.save_and_exit(slot=1)

    assert stopped == [True]


def test_save_and_exit_returns_no_state_shape(
    monkeypatch: pytest.MonkeyPatch, save_roots: Tuple[Path, Path, Path, Path]
) -> None:
    """Saving and exiting reports no save-state shape, since Azahar has none."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    result = emu.save_and_exit(slot=1)

    assert result == {"state_saved": None, "state_slot": None, "state_file": None}


def test_save_and_exit_accepts_a_none_slot_and_still_restamps(
    monkeypatch: pytest.MonkeyPatch, save_roots: Tuple[Path, Path, Path, Path]
) -> None:
    """A None slot, the base contract's exit-without-saving, still ships this session's save data."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    f = title / "save.bin"
    f.write_bytes(b"data")
    old = time.time() - 10_000
    os.utime(f, (old, old))
    emu = azahar.Azahar()
    emu._session_start = 0.0

    result = emu.save_and_exit(slot=None)

    assert result == {"state_saved": None, "state_slot": None, "state_file": None}
    assert os.stat(f).st_mtime > old


def test_save_and_exit_restamps_every_file_in_a_touched_title_dir(
    monkeypatch: pytest.MonkeyPatch, save_roots: Tuple[Path, Path, Path, Path]
) -> None:
    """Every file in a touched title dir is restamped, not just the touched one."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    touched = title / "save.bin"
    untouched_sibling = title / "misc.bin"
    touched.write_bytes(b"data")
    untouched_sibling.write_bytes(b"data2")
    old = time.time() - 10_000
    os.utime(untouched_sibling, (old, old))
    emu = azahar.Azahar()
    emu._session_start = time.time() - 1  # before touched's just-written mtime

    emu.save_and_exit(slot=1)

    assert os.stat(untouched_sibling).st_mtime > old


def test_save_and_exit_leaves_untouched_title_dirs_alone(
    monkeypatch: pytest.MonkeyPatch, save_roots: Tuple[Path, Path, Path, Path]
) -> None:
    """A title dir untouched this session is left with its original mtime."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    f = title / "save.bin"
    f.write_bytes(b"data")
    old = time.time() - 10_000
    os.utime(f, (old, old))
    emu = azahar.Azahar()
    emu._session_start = time.time()  # after f's mtime: not touched this session

    emu.save_and_exit(slot=1)

    assert os.stat(f).st_mtime == pytest.approx(old, abs=1)


def test_save_and_exit_logs_and_continues_when_a_restamp_fails(
    monkeypatch: pytest.MonkeyPatch,
    save_roots: Tuple[Path, Path, Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restamp failure is logged and does not stop the exit route."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")

    def fail_utime(path: Path, times: Tuple[float, float]) -> NoReturn:
        raise OSError("boom")

    monkeypatch.setattr(azahar.os, "utime", fail_utime)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    with caplog.at_level("WARNING"):
        result = emu.save_and_exit(slot=1)

    assert "could not restamp" in caplog.text
    assert result == {"state_saved": None, "state_slot": None, "state_file": None}


def test_save_and_exit_logs_and_continues_when_the_walk_itself_raises(
    monkeypatch: pytest.MonkeyPatch,
    save_roots: Tuple[Path, Path, Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A title dir vanishing mid-walk must not crash the exit route, since stop is already committed."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    monkeypatch.setattr(azahar.Azahar, "_modified_title_saves", lambda self: [title])

    def fail_rglob(self: Path, pattern: str) -> NoReturn:
        raise OSError("directory vanished mid-walk")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    with caplog.at_level("WARNING"):
        result = emu.save_and_exit(slot=1)

    assert "could not walk" in caplog.text
    assert result == {"state_saved": None, "state_slot": None, "state_file": None}


# -- declared imports --

_ROMM = imports.RomRef(1, "Game", "3ds", title_id="0004000000033500", save_target="00040000/00033500")
"""The rom a 3DS activate carries, with RomM's title id and its save target."""
_ROMM_NO_TARGET = imports.RomRef(1, "Game", "3ds", title_id="0004000000033500")
"""A rom whose RomM entry has a title id and no save target."""
_ID = "0" * 32
"""The console and SD card ids Azahar files saves under."""
_HW0 = "a1b2c3d4" * 4
"""A hardware console id, which is not all zeros."""
_HW1 = "e5f6a7b8" * 4
"""A hardware SD card id."""
_SD = f"sdmc/Nintendo 3DS/{_ID}/{_ID}"
"""An SD card's per-console folder as Azahar names it."""
_DEST_SD = f"sdmc/Nintendo 3DS/{azahar.SYSTEM_ID}/{azahar.SDCARD_ID}"
"""Where the SD card's per-console folder is, below the data root."""
_TITLE = "title/00040000/00033500"
"""The session's title folder under an SD card."""
_SAVE_FILE = f"{_TITLE}/data/00000001.sav"
"""The save file a title folder holds."""


@pytest.fixture
def user_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point Azahar's save root, and the save-group roots the exit restamp walks, into tmp_path.

    A restore lands under `save_root`, which the class resolves once at
    import, and the exit route reads the module's group roots, so a round
    trip would reach outside tmp_path without both.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The patched data root.
    """
    root = tmp_path / "azahar-emu"
    sdmc = root / "sdmc" / "Nintendo 3DS" / azahar.SYSTEM_ID / azahar.SDCARD_ID
    nand = root / "nand" / "data" / azahar.SYSTEM_ID
    monkeypatch.setattr(azahar.Azahar, "save_root", root)
    monkeypatch.setattr(
        azahar,
        "_SAVE_GROUP_ROOTS",
        (sdmc / "title", sdmc / "extdata", nand / "extdata", nand / "sysdata"),
    )
    return root


def _preflight(
    members: dict[str, bytes],
    *,
    rom: Optional[imports.RomRef] = _ROMM,
) -> imports.PreflightResult:
    """Preflight an archive of import members against Azahar.

    Args:
        members: `.import/<kind>/...` names mapped to bytes.
        rom: The activate body's rom, or None.

    Returns:
        What preflight decided.
    """
    return preflight_import(azahar.Azahar(), import_zip(members), rom_file=None, rom=rom)


def _save(rel: str) -> dict[str, bytes]:
    """One save member's archive, named by its path below `.import/save/`.

    Args:
        rel: The path below `.import/save/`.

    Returns:
        The members mapping `_preflight` takes.
    """
    return {f".import/save/{rel}": b"x"}


@pytest.mark.parametrize(
    ("rel", "dest"),
    [
        (f"{_SD}/{_SAVE_FILE}", f"{_DEST_SD}/{_SAVE_FILE}"),
        (f"saves/Azahar/Azahar/{_SD}/{_SAVE_FILE}", f"{_DEST_SD}/{_SAVE_FILE}"),
        (f"saves/Azahar/Azahar/{_ID}/{_ID}/{_SAVE_FILE}", f"{_DEST_SD}/{_SAVE_FILE}"),
        (f"saves/Azahar/Azahar/sdmc/Nintendo 3DS/{_ID}/{_ID}/{_SAVE_FILE}", f"{_DEST_SD}/{_SAVE_FILE}"),
        (f"Nintendo 3DS/{_ID}/{_ID}/{_SAVE_FILE}", f"{_DEST_SD}/{_SAVE_FILE}"),
        (f"{_ID}/{_ID}/{_SAVE_FILE}", f"{_DEST_SD}/{_SAVE_FILE}"),
        (f"sdmc/Nintendo 3DS/{_HW0}/{_HW1}/{_SAVE_FILE}", f"{_DEST_SD}/{_SAVE_FILE}"),
        (f"{_SD}/extdata/00048000/00001234/00000001", f"{_DEST_SD}/extdata/00048000/00001234/00000001"),
        (f"{_SD}/extdata/00048000/0000ABCD/x", f"{_DEST_SD}/extdata/00048000/0000abcd/x"),
        (
            f"nand/data/{_ID}/extdata/00048000/00001234/00000001",
            f"nand/data/{azahar.SYSTEM_ID}/extdata/00048000/00001234/00000001",
        ),
        (
            f"saves/Azahar/Azahar/nand/data/{_HW0}/extdata/00048000/0000ABCD/x",
            f"nand/data/{azahar.SYSTEM_ID}/extdata/00048000/0000abcd/x",
        ),
    ],
    ids=[
        "verbatim",
        "saves/Azahar/Azahar",
        "saves/Azahar/Azahar with no SD folders",
        "saves/Azahar/Azahar with sdmc",
        "Nintendo 3DS",
        "bare ids",
        "hardware ids are rewritten",
        "sd extdata",
        "sd extdata, hex lower-cased",
        "nand extdata",
        "nand extdata, wrapped in saves/Azahar/Azahar, hex lower-cased",
    ],
)
@pytest.mark.usefixtures("user_dir")
def test_a_save_lands_under_the_ids_azahar_uses(rel: str, dest: str) -> None:
    """Every accepted spelling lands under the fixed console and SD card ids, in lower case.

    Args:
        rel: The member's path below `.import/save/`.
        dest: Where it lands, below the data root.
    """
    result = _preflight(_save(rel))

    assert result.refusals == ()
    assert [str(p.dest) for p in result.placements] == [dest]


@pytest.mark.usefixtures("user_dir")
def test_an_extdata_folder_is_not_held_to_the_sessions_title() -> None:
    """Extdata is keyed by its own id, not the title's, so no identity check applies."""
    result = _preflight(_save(f"{_SD}/extdata/00048000/00009999/00000001"))

    assert result.refusals == ()


@pytest.mark.usefixtures("user_dir")
def test_a_title_folder_for_another_game_is_refused() -> None:
    """A title save is held strictly to the game the session runs."""
    result = _preflight(_save(f"{_SD}/title/00040000/00099999/data/00000001.sav"))

    assert [r.reason for r in result.refusals] == ["identity_mismatch"]
    assert result.placements == ()


@pytest.mark.usefixtures("user_dir")
def test_a_title_id_alone_still_names_the_title_a_save_must_match() -> None:
    """RomM's title id is the save target's two halves run together, so it keys the title too."""
    result = _preflight(_save(_SD + "/" + _SAVE_FILE), rom=_ROMM_NO_TARGET)

    assert result.refusals == ()
    assert [str(p.dest) for p in result.placements] == [f"{_DEST_SD}/{_SAVE_FILE}"]


@pytest.mark.usefixtures("user_dir")
def test_another_titles_save_is_refused_against_a_title_id_alone() -> None:
    """With no save target the title id is read in its place, so a foreign title still mismatches."""
    result = _preflight(
        _save(f"{_SD}/title/00040000/00099999/data/00000001.sav"), rom=_ROMM_NO_TARGET
    )

    assert [r.reason for r in result.refusals] == ["identity_mismatch"]
    assert result.placements == ()


@pytest.mark.usefixtures("user_dir")
def test_a_save_target_outranks_a_title_id_that_disagrees() -> None:
    """The save target is read first, so a title id naming another game is never consulted."""
    rom = imports.RomRef(1, "Game", "3ds", title_id="0004000000099999", save_target="00040000/00033500")

    result = _preflight(_save(_SD + "/" + _SAVE_FILE), rom=rom)

    assert result.refusals == ()
    assert [str(p.dest) for p in result.placements] == [f"{_DEST_SD}/{_SAVE_FILE}"]


@pytest.mark.usefixtures("user_dir")
def test_a_title_save_is_taken_on_trust_without_a_rom() -> None:
    """A route that carries no rom names no title, so there is nothing to compare a save with."""
    result = _preflight(_save(f"{_SD}/title/00040000/00099999/data/00000001.sav"), rom=None)

    assert result.refusals == ()


@pytest.mark.usefixtures("user_dir")
def test_a_save_target_in_another_case_still_matches() -> None:
    """The comparison is on the normalised id, so RomM's spelling does not matter."""
    rom = imports.RomRef(1, "Game", "3ds", save_target="00040000/0003350A")

    result = _preflight(_save(f"{_SD}/title/00040000/0003350a/data/00000001.sav"), rom=rom)

    assert result.refusals == ()


@pytest.mark.parametrize(
    ("rel", "reason"),
    [
        (f"nand/data/{_ID}/sysdata/00010026/00000000/x", "protected_destination"),
        (f"saves/Azahar/Azahar/nand/data/{_ID}/sysdata/x", "protected_destination"),
        (f"saves/Azahar/{_SD}/{_SAVE_FILE}", "unrecognised_layout"),
        (f"saves/Azahar/{_ID}/{_ID}/{_SAVE_FILE}", "unrecognised_layout"),
        (f"saves/Azahar/sdmc/Nintendo 3DS/{_ID}/{_ID}/{_SAVE_FILE}", "unrecognised_layout"),
        (
            f"saves/Azahar/nand/data/{_HW0}/extdata/00048000/0000ABCD/x",
            "unrecognised_layout",
        ),
        (f"saves/Azahar/nand/data/{_ID}/sysdata/x", "unrecognised_layout"),
        ("3ds/JKSM/Saves/Game/00000001.sav", "shape_unverified"),
        ("JKSM/Saves/Game/00000001.sav", "shape_unverified"),
        ("3ds/Checkpoint/saves/0x00033500 Game/00000001.sav", "shape_unverified"),
        ("Checkpoint/extdata/0x00001234 Game/x", "shape_unverified"),
        ("00000001.sav", "destination_unresolvable"),
        ("data/00000001.sav", "destination_unresolvable"),
        ("title/00040000/00033500/data/00000001.sav", "unrecognised_layout"),
        ("extdata/00048000/00001234/x", "unrecognised_layout"),
        (f"{_SD}/Nintendo DSiWare/x", "unrecognised_layout"),
        (f"{_SD}/dbs/title.db", "unrecognised_layout"),
        ("sdmc/Nintendo 3DS/Private/x", "unrecognised_layout"),
        ("nand/rw/x", "unrecognised_layout"),
        (f"nand/data/{_ID}/x", "unrecognised_layout"),
        ("readme/notes.txt", "unrecognised_layout"),
        (f"{_SD}/title/00040000/00033500", "unrecognised_layout"),
        (f"{_SD}/title/00040000/data.bin", "unrecognised_layout"),
        (f"{_SD}/title/notes/x/y", "unrecognised_layout"),
        (f"{_SD}/extdata/00048000/00001234", "unrecognised_layout"),
        (f"nand/data/{_ID}/extdata/00048000/00001234", "unrecognised_layout"),
        (f"nand/data/{_ID}/sysdata", "unrecognised_layout"),
    ],
    ids=[
        "sysdata",
        "wrapped sysdata",
        "saves/Azahar single-level",
        "saves/Azahar single-level with no SD folders",
        "saves/Azahar single-level with sdmc",
        "saves/Azahar single-level nand extdata, wrapped, hex lower-cased",
        "saves/Azahar single-level wrapped sysdata",
        "JKSM in 3ds",
        "JKSM at the top",
        "Checkpoint saves",
        "Checkpoint extdata",
        "loose file",
        "loose data folder",
        "bare title tree",
        "bare extdata tree",
        "DSiWare sibling",
        "dbs sibling on a zero-id card",
        "Private sibling",
        "nand rw sibling",
        "nand data with no group",
        "unrelated file",
        "title folder with no file",
        "title low is not hex",
        "title high is not hex",
        "extdata folder with no file",
        "nand extdata folder with no file",
        "a sysdata folder with no file",
    ],
)
@pytest.mark.usefixtures("user_dir")
def test_a_member_azahar_would_not_read_is_refused(rel: str, reason: str) -> None:
    """Each shape the spec names is refused with its own code, and nothing is placed.

    Args:
        rel: The member's path below `.import/save/`.
        reason: The refusal code.
    """
    result = _preflight(_save(rel))

    assert [r.reason for r in result.refusals] == [reason]
    assert result.placements == ()


@pytest.mark.parametrize(
    ("rom", "reason"),
    [
        (_ROMM, "destination_unresolvable"),
        (_ROMM_NO_TARGET, "destination_unresolvable"),
        (None, "unrecognised_layout"),
    ],
    ids=["save target", "title id only", "neither"],
)
@pytest.mark.usefixtures("user_dir")
def test_a_loose_file_is_unresolvable_only_when_romm_knows_the_game(
    rom: Optional[imports.RomRef], reason: str
) -> None:
    """A file with no title in its path is not placed from RomM's id, but RomM knowing the game is said.

    Args:
        rom: The activate body's rom.
        reason: The refusal code.
    """
    result = _preflight(_save("00000001.sav"), rom=rom)

    assert [r.reason for r in result.refusals] == [reason]


@pytest.mark.parametrize("marker", ["dbs/title.db", "backups/movable.sed"])
@pytest.mark.usefixtures("user_dir")
def test_a_hardware_sd_card_is_refused_whole(marker: str) -> None:
    """Non-zero ids beside a `dbs` or `backups` folder are an encrypted SD card, all refused.

    Args:
        marker: The sibling folder's file, below the SD card's ids.
    """
    members = {
        **_save(f"sdmc/Nintendo 3DS/{_HW0}/{_HW1}/{_SAVE_FILE}"),
        **_save(f"sdmc/Nintendo 3DS/{_HW0}/{_HW1}/{marker}"),
    }

    result = _preflight(members)

    assert [r.reason for r in result.refusals] == ["source_incompatible", "source_incompatible"]
    assert result.placements == ()


@pytest.mark.usefixtures("user_dir")
def test_a_second_hardware_card_without_the_marker_is_not_refused() -> None:
    """The marker names one pair of ids; a decrypted export of another pair is still taken.

    Preflight places nothing while any member is refused, so the second
    card's file shows as taken by not being among the refusals.
    """
    marker = f".import/save/sdmc/Nintendo 3DS/{_HW0}/{_HW1}/dbs/title.db"
    members = {
        marker: b"x",
        **_save(f"sdmc/Nintendo 3DS/{_HW1}/{_HW0}/{_SAVE_FILE}"),
    }

    result = _preflight(members)

    assert [(r.member, r.reason) for r in result.refusals] == [(marker, "source_incompatible")]


@pytest.mark.usefixtures("user_dir")
def test_two_cards_for_one_title_are_a_destination_conflict() -> None:
    """Two cards' ids rewritten to one pair put two members on one file; the shared check refuses it."""
    members = {
        **_save(f"sdmc/Nintendo 3DS/{_HW0}/{_HW1}/{_SAVE_FILE}"),
        **_save(f"{_SD}/{_SAVE_FILE}"),
    }

    result = _preflight(members)

    assert {r.reason for r in result.refusals} == {"destination_conflict"}


@pytest.mark.usefixtures("user_dir")
def test_a_state_or_memory_card_is_not_taken() -> None:
    """Azahar has no states and no cards, so the kind gate stops them before the hook."""
    result = _preflight({".import/state/game.sav": b"x", ".import/memcard/card.bin": b"x"})

    assert sorted(r.reason for r in result.refusals) == ["kind_not_accepted", "kind_not_accepted"]


def test_an_imported_save_is_where_azahars_own_lookups_find_it(user_dir: Path) -> None:
    """A placed title and extdata folder are the ones the exit restamp walks, and the clear empties them.

    The read-back is by literal lower-case path: on a case-sensitive
    filesystem, a destination Azahar never opens would be written and no
    refusal could catch it.

    Args:
        user_dir: The patched data root.
    """
    emu = azahar.Azahar()
    body = import_zip(
        {
            f".import/save/{_SD}/{_SAVE_FILE}": b"progress",
            f".import/save/{_SD}/extdata/00048000/00001234/00000001": b"photos",
        }
    )
    result = preflight_import(emu, body, rom_file=None, rom=_ROMM)
    restore_import(emu, body, result)

    sdmc = user_dir / _DEST_SD
    assert (sdmc / _SAVE_FILE).read_bytes() == b"progress"
    assert (sdmc / "extdata/00048000/00001234/00000001").read_bytes() == b"photos"
    emu._session_start = time.time() - 100
    assert emu._modified_title_saves() == [
        sdmc / "title/00040000/00033500",
        sdmc / "extdata/00048000/00001234",
    ]

    emu.clear_working_slot()

    assert not any(p.is_file() for p in user_dir.rglob("*"))


def test_azahar_declares_a_save_kind_only() -> None:
    """The spec names the save kind alone, with no state channel, and sysdata as protected."""
    spec = azahar.Azahar().import_spec()

    assert [k.kind for k in spec.kinds] == ["save"]
    assert spec.state_channel == "none"
    assert spec.protected == ("nand/data/*/sysdata/*",)
    assert spec.case_insensitive_dest is False


def test_azahars_session_id_is_romms_save_target_then_its_title_id() -> None:
    """A 3DS title is keyed by the `high/low` pair, which RomM carries in either field."""
    source = azahar.Azahar().identity_source()

    assert source == imports.IdentitySource("hex16", use_save_target=True, fall_back_to_title_id=True)


@pytest.mark.usefixtures("user_dir")
def test_two_spellings_of_one_folder_are_a_destination_conflict() -> None:
    """A title folder's hex is lower-cased, so two cases of one id are one destination."""
    members = {
        **_save(f"{_SD}/extdata/00048000/0000ABCD/x"),
        **_save(f"{_SD}/extdata/00048000/0000abcd/x"),
    }

    result = _preflight(members)

    assert {r.reason for r in result.refusals} == {"destination_conflict"}


def _member(rel: str) -> imports.ImportMember:
    """One save member, built without an archive, for a hook that reads only its path.

    Args:
        rel: The path below `.import/save/`.

    Returns:
        The member.
    """
    name = f".import/save/{rel}"
    return imports.ImportMember(
        name, "save", "unknown", PurePosixPath(rel), tuple(rel.split("/")), 1, zipfile.ZipInfo(name)
    )


def _ctx(*rels: str) -> imports.ImportCtx:
    """A launch context holding the given save members.

    Args:
        *rels: The members' paths below `.import/save/`.

    Returns:
        The context.
    """
    return imports.ImportCtx(
        rom_file=None,
        rom=None,
        memory_card_synced=False,
        excluded=(),
        resume_slot=None,
        members=tuple(_member(rel) for rel in rels),
    )


def test_the_hardware_cards_are_the_lower_cased_pairs_with_a_marker() -> None:
    """Only a non-zero pair with a `dbs` or `backups` folder counts, and it is compared in lower case."""
    ctx = _ctx(
        f"sdmc/Nintendo 3DS/{_HW0.upper()}/{_HW1.upper()}/dbs/title.db",
        f"sdmc/Nintendo 3DS/{_HW1}/{_HW0}/{_SAVE_FILE}",
        f"{_SD}/dbs/title.db",
    )

    assert azahar._hardware_sd_ids(ctx) == frozenset({(_HW0, _HW1)})


def test_the_hardware_cards_are_found_once_per_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan reads every member once, however many members ask, and the answer is kept in the memo.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    ctx = _ctx(
        f"sdmc/Nintendo 3DS/{_HW0}/{_HW1}/dbs/title.db",
        f"sdmc/Nintendo 3DS/{_HW0}/{_HW1}/{_SAVE_FILE}",
        f"{_SD}/{_SAVE_FILE}",
    )
    seen: list[tuple[str, ...]] = []
    real = imports.match_anchored

    def spy(parts: Sequence[str], **kwargs: Any) -> Optional[imports.AnchoredMatch]:
        """Record which member the scan matched, then match it.

        Args:
            parts: The member's components.
            **kwargs: The keyword arguments `match_anchored` takes.

        Returns:
            What `match_anchored` returns.
        """
        seen.append(tuple(parts))
        return real(parts, **kwargs)

    monkeypatch.setattr(imports, "match_anchored", spy)

    first = azahar._hardware_sd_ids(ctx)
    scanned = len(seen)
    second = azahar._hardware_sd_ids(ctx)

    assert scanned == len(ctx.members)
    assert len(seen) == scanned
    assert second is first


def test_the_import_paths_are_the_save_subtrees_azahar_dumps_and_restores() -> None:
    """The hook's destinations sit in the trees the dump, the restore and the clear walk."""
    subtrees = azahar.Azahar.save_subtrees

    assert f"{azahar._SD_ROOT}/title" == subtrees[0]
    assert f"{azahar._SD_ROOT}/extdata" == subtrees[1]
    assert azahar._NAND_EXTDATA == subtrees[2]
    assert fnmatch.fnmatchcase(f"{subtrees[3]}/00010026/00000000/x", azahar._PROTECTED[0])
