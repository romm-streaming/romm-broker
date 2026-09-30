"""ScummVM target resolution, ini pinning, GMM macros and slot naming.

Covers registering a game folder, the settings pinned into scummvm.ini, the
keystroke sequences the Global Main Menu is driven with, the save naming
that decides what is a state and what is the game's own save, and the saves
and state an archive can declare. Nothing here needs a display, a binary or a
real ScummVM.
"""

import subprocess
import time
import unicodedata
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from webstation_broker import imports
from webstation_broker.emulators import scummvm
from webstation_broker.emulators.scummvm import Scummvm

from .conftest import import_zip, preflight_import, restore_import


@pytest.fixture(autouse=True)
def dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Redirect every location the launcher reads at import time into tmp_path.

    The module resolves its paths into globals when it is imported, so the
    redirect patches those globals rather than the environment.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
        tmp_path: The per-test temporary directory.

    Returns:
        The redirected directories, keyed as "roms", "config", "saves", and the
        ini path as "ini".
    """
    roms = tmp_path / "romm"
    config = tmp_path / "config"
    saves = tmp_path / "saves"
    for directory in (roms, config, saves):
        directory.mkdir()
    ini = config / "scummvm.ini"
    monkeypatch.setattr(scummvm, "ROM_ROOT", roms)
    monkeypatch.setattr(scummvm, "CONFIG_DIR", config)
    monkeypatch.setattr(scummvm, "INI_PATH", ini)
    monkeypatch.setattr(scummvm, "SAVE_DIR", saves)
    monkeypatch.setattr(scummvm, "CACHE_DIR", tmp_path / "extracted")
    monkeypatch.setattr(scummvm.settings, "SCUMMVM_CACHE_ENABLED", True)
    # The class resolves its root once at import, and the save subtree hangs off
    # it rather than off SAVE_DIR, so the clear would reach outside tmp_path.
    monkeypatch.setattr(scummvm.Scummvm, "save_root", tmp_path)
    # The macros sleep between steps to let the menu animate; nothing in a test
    # is waiting for an animation.
    monkeypatch.setattr(scummvm, "KEY_DELAY", 0.0)
    # Likewise the settle window: a test's write is already finished when it is
    # made, so the tests that assert on the window set their own.
    monkeypatch.setattr(scummvm, "STATE_STABLE", 0.0)
    return {"roms": roms, "config": config, "saves": saves, "ini": ini}


def write_ini(ini: Path, body: str) -> Path:
    """Write a scummvm.ini, trimming the leading indentation of a literal block.

    Args:
        ini: Where to write it.
        body: The file's contents.

    Returns:
        The path written.
    """
    ini.write_text("\n".join(line.strip() for line in body.strip().splitlines()) + "\n")
    return ini


def game_folder(roms: Path, name: str = "monkey") -> Path:
    """Create a ROM folder holding one data file.

    Args:
        roms: The ROM root to create it under.
        name: The folder's name.

    Returns:
        The created folder.
    """
    folder = roms / name
    folder.mkdir(parents=True)
    (folder / "monkey.000").write_bytes(b"data")
    return folder


# -- ROM resolution --


def test_a_game_folder_resolves_to_itself(dirs: dict[str, Path]) -> None:
    """A folder holding game files resolves to that folder."""
    folder = game_folder(dirs["roms"])

    assert Scummvm().resolve_rom_file(folder) == folder.resolve()


def test_a_file_resolves_to_the_folder_holding_it(dirs: dict[str, Path]) -> None:
    """A ROM pointing at a file registers the folder around it.

    ScummVM registers directories, so a library that points at a `.scummvm`
    marker inside the game folder still has to boot.
    """
    folder = game_folder(dirs["roms"])
    marker = folder / "Monkey Island.scummvm"
    marker.write_text("monkey")

    assert Scummvm().resolve_rom_file(marker) == folder.resolve()


def test_an_archive_resolves_to_itself_rather_than_its_folder(
    dirs: dict[str, Path],
) -> None:
    """A zipped game must not be read as "the folder it sits in".

    Libraries are laid out as <root>/<platform>/<game>, so the folder around a
    loose file is the platform folder. Registering that would `--add` every
    game on the platform and boot whichever target sorts first: the player
    asks for one game and gets another, which is worse than a refused launch.
    The archive itself is what `launch` extracts and registers instead.
    """
    game_folder(dirs["roms"])
    archive = zipped_game(dirs["roms"], {"WOODRUFF.000": b"data"}, "woodruff.zip")

    assert Scummvm().resolve_rom_file(archive) == archive.resolve()


def test_an_archive_is_refused_with_the_cache_off(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nowhere to extract it, an archived game cannot boot and is not advertised."""
    monkeypatch.setattr(scummvm.settings, "SCUMMVM_CACHE_ENABLED", False)
    archive = dirs["roms"] / "woodruff.zip"
    archive.write_bytes(b"PK\x03\x04")
    emu = Scummvm()

    assert emu.resolve_rom_file(archive) is None
    assert ".zip" not in emu.rom_extensions


def test_a_folder_holding_only_an_archive_resolves_to_the_archive(dirs: dict[str, Path]) -> None:
    """A game folder whose only content is its zip (and a marker) boots the zip."""
    folder = dirs["roms"] / "woodruff"
    folder.mkdir()
    archive = zipped_game(folder, {"WOODRUFF.000": b"data"}, "woodruff.zip")
    (folder / "woodruff.scummvm").write_text("woodruff")

    assert Scummvm().resolve_rom_file(folder) == archive.resolve()
    assert Scummvm().resolve_rom_file(folder / "woodruff.scummvm") == archive.resolve()


def test_a_folder_with_an_archive_beside_game_files_is_the_game(dirs: dict[str, Path]) -> None:
    """An extras zip next to the data files does not stand in for the game."""
    folder = game_folder(dirs["roms"])
    (folder / "extras.zip").write_bytes(b"PK\x03\x04")

    assert Scummvm().resolve_rom_file(folder) == folder.resolve()


def test_an_archive_linked_in_from_outside_the_library_is_refused(
    dirs: dict[str, Path], tmp_path: Path
) -> None:
    """The file about to be extracted must itself live under the ROM root."""
    outside = tmp_path / "elsewhere.zip"
    outside.write_bytes(b"PK\x03\x04")
    link = dirs["roms"] / "woodruff.zip"
    link.symlink_to(outside)

    assert Scummvm().resolve_rom_file(link) is None


def test_an_empty_folder_resolves_to_nothing(dirs: dict[str, Path]) -> None:
    """A folder with nothing in it has nothing to detect."""
    empty = dirs["roms"] / "empty"
    empty.mkdir()

    assert Scummvm().resolve_rom_file(empty) is None


def test_a_folder_outside_the_rom_root_resolves_to_nothing(
    dirs: dict[str, Path], tmp_path: Path
) -> None:
    """A folder outside the library is refused even when it holds a game."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "monkey.000").write_bytes(b"data")

    assert Scummvm().resolve_rom_file(outside) is None


def test_a_missing_path_resolves_to_nothing(dirs: dict[str, Path]) -> None:
    """A path that is not there resolves to nothing rather than raising."""
    assert Scummvm().resolve_rom_file(dirs["roms"] / "absent") is None


# -- Targets in scummvm.ini --


def test_a_registered_folder_resolves_to_its_target(dirs: dict[str, Path]) -> None:
    """A domain whose path matches the folder names the target to boot."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [scummvm]
        gui_saveload_chooser=list

        [monkey]
        gameid=monkey
        path={folder}
        """,
    )

    assert scummvm.target_for_path(folder) == "monkey"


def test_a_section_without_a_game_is_not_a_target(dirs: dict[str, Path]) -> None:
    """Only a section carrying both a path and a gameid/engineid is a game.

    The application section and the keymap sections carry neither, and a
    savepath pointing at the folder must not make `[scummvm]` look like one.
    """
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [scummvm]
        savepath={folder}

        [keymapper]
        path={folder}
        """,
    )

    assert scummvm.target_for_path(folder) is None


def test_a_multilingual_folder_picks_one_variant_every_time(dirs: dict[str, Path]) -> None:
    """One domain per detected language still boots one stable target.

    The save files are named after whichever is picked, so the pick has to
    survive a relaunch.
    """
    folder = game_folder(dirs["roms"], "gob1")
    write_ini(
        dirs["ini"],
        f"""
        [gob1-cd-fr]
        gameid=gob1
        path={folder}

        [gob1-cd-de]
        gameid=gob1
        path={folder}
        """,
    )

    assert scummvm.target_for_path(folder) == "gob1-cd-de"
    assert scummvm.target_for_path(folder) == "gob1-cd-de"


def test_an_unregistered_folder_has_no_target(dirs: dict[str, Path]) -> None:
    """A folder no domain points at has no target."""
    write_ini(dirs["ini"], "[scummvm]\ngui_saveload_chooser=list")

    assert scummvm.target_for_path(dirs["roms"] / "monkey") is None


def test_a_missing_ini_has_no_targets(dirs: dict[str, Path]) -> None:
    """A container that has never run ScummVM has no ini and no targets."""
    assert scummvm.target_for_path(dirs["roms"] / "monkey") is None


def test_registering_reads_the_target_back_out_of_the_ini(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--add` is trusted for the write, and the ini for the answer.

    Its exit code reports success having added nothing, so the domain landing
    in the ini is the only honest signal that a game was detected.
    """
    folder = game_folder(dirs["roms"])
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Record the argv and write the domain `--add` would have written.

        Args:
            cmd: The argv the launcher ran.
            **kwargs: The rest of the subprocess arguments, ignored.

        Returns:
            A successful result carrying ScummVM's own "Game Added" line.
        """
        calls.append(cmd)
        write_ini(dirs["ini"], f"[monkey]\ngameid=monkey\npath={folder}")
        return subprocess.CompletedProcess(cmd, 0, "Game Added\n", "")

    monkeypatch.setattr(scummvm.subprocess, "run", fake_run)

    assert scummvm.register_target(folder) == "monkey"
    assert calls[0][1:] == [f"--config={dirs['ini']}", "--add", f"--path={folder}"]


def test_a_folder_scummvm_detects_nothing_in_has_no_target(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--add` exiting 0 without writing a domain is a folder with no game."""
    folder = game_folder(dirs["roms"])

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Report success having added nothing, the way ScummVM does.

        Args:
            cmd: The argv the launcher ran.
            **kwargs: The rest of the subprocess arguments, ignored.

        Returns:
            A successful result that added no game.
        """
        return subprocess.CompletedProcess(cmd, 0, "Added 0 games\n", "")

    monkeypatch.setattr(scummvm.subprocess, "run", fake_run)

    assert scummvm.register_target(folder) is None


def test_a_failing_add_has_no_target(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `--add` that could not run at all leaves the folder unregistered."""
    folder = game_folder(dirs["roms"])

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Fail the way a missing binary or a timeout does.

        Args:
            cmd: The argv the launcher ran.
            **kwargs: The rest of the subprocess arguments, ignored.

        Raises:
            OSError: Always.
        """
        raise OSError("no such binary")

    monkeypatch.setattr(scummvm.subprocess, "run", fake_run)

    assert scummvm.register_target(folder) is None


class AddRuns:
    """A stubbed `scummvm --add` that answers the way ScummVM would.

    Attributes:
        attempts: The rom_dir each call was made against, in order.
    """

    def __init__(self, folder: Path) -> None:
        """Set up the stub.

        Args:
            folder: The folder the caller is trying to register.
        """
        self.folder = folder
        self.attempts: list[str] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Answer one `--add`, registering the folder once nothing blocks it.

        Detection deduplicates by game, so a domain already carrying this
        gameid makes the scan skip the folder entirely.

        Args:
            cmd: The argv the launcher ran.
            **kwargs: The rest of the subprocess arguments, ignored.

        Returns:
            ScummVM's own output for the case being simulated.
        """
        self.attempts.append(cmd[-1])
        blocked = any(
            keys.get("gameid") == "monkey" for keys in scummvm._game_domains().values()
        )
        if blocked:
            return subprocess.CompletedProcess(
                cmd,
                0,
                "Found scumm:monkey, but has already been added, skipping\nAdded 0 games\n",
                "",
            )
        ini = scummvm.INI_PATH
        ini.write_text(
            ini.read_text() + f"\n[monkey-fr]\ngameid=monkey\npath={self.folder}\n"
        )
        return subprocess.CompletedProcess(cmd, 0, "Game Added\n", "")


def test_a_moved_library_can_be_registered_again(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain left behind by a library that moved must not block the new path.

    Detection deduplicates by game rather than by folder, so the stale entry
    made the same game unregisterable at its new path forever: the scan
    skipped it, no domain appeared, and the launch had nothing to boot.
    """
    folder = game_folder(dirs["roms"])
    write_ini(dirs["ini"], "[monkey-fr]\ngameid=monkey\npath=/gone/MONKEY")
    add = AddRuns(folder)
    monkeypatch.setattr(scummvm.subprocess, "run", add)

    assert scummvm.register_target(folder) == "monkey-fr"
    # Once for the blocked scan, once after the dead domain was cleared.
    assert len(add.attempts) == 2


def test_another_live_copy_of_the_same_game_is_left_alone(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a domain whose path is gone is cleared, never one still on disk.

    A library that is merely unmounted keeps its registrations, and with them
    the save files named after their targets.
    """
    folder = game_folder(dirs["roms"])
    other = dirs["roms"] / "monkey-copy"
    other.mkdir()
    write_ini(dirs["ini"], f"[monkey-en]\ngameid=monkey\npath={other}")
    monkeypatch.setattr(scummvm.subprocess, "run", AddRuns(folder))

    scummvm.register_target(folder)

    assert "monkey-en" in scummvm._ini_domains()


def test_a_dead_domain_for_another_game_is_left_alone(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing is scoped to the game that was actually in the way."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        """
        [monkey-fr]
        gameid=monkey
        path=/gone/MONKEY

        [indy3-vga]
        gameid=indy3
        path=/gone/INDY3
        """,
    )
    monkeypatch.setattr(scummvm.subprocess, "run", AddRuns(folder))

    scummvm.register_target(folder)

    domains = scummvm._ini_domains()
    assert "indy3-vga" in domains
    assert domains["monkey-fr"]["path"] == str(folder)


def test_a_genuinely_undetectable_folder_still_reports_nothing(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dead domain to clear means the folder simply holds no game."""
    folder = game_folder(dirs["roms"])

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Report a scan that found nothing at all.

        Args:
            cmd: The argv the launcher ran.
            **kwargs: The rest of the subprocess arguments, ignored.

        Returns:
            A scan that added nothing and blocked on nothing.
        """
        return subprocess.CompletedProcess(cmd, 0, "Added 0 games\n", "")

    monkeypatch.setattr(scummvm.subprocess, "run", fake_run)

    assert scummvm.register_target(folder) is None


# -- The pinned ini --


def test_a_missing_ini_is_created_with_the_pins(dirs: dict[str, Path]) -> None:
    """A fresh container gets an ini holding the settings the macros need."""
    scummvm.patch_ini()

    domains = scummvm._ini_domains()
    assert domains["scummvm"]["gui_saveload_chooser"] == "list"
    assert domains["scummvm"]["savepath"] == str(dirs["saves"])
    assert domains["scummvm"]["fullscreen"] == "false"


def test_pinned_keys_are_rewritten_and_the_rest_is_left_alone(dirs: dict[str, Path]) -> None:
    """The broker's settings win; everything else the user set survives."""
    write_ini(
        dirs["ini"],
        """
        [scummvm]
        gui_saveload_chooser=grid
        fullscreen=true
        music_volume=192

        [monkey]
        gameid=monkey
        path=/romm/monkey
        """,
    )

    scummvm.patch_ini()

    domains = scummvm._ini_domains()
    assert domains["scummvm"]["gui_saveload_chooser"] == "list"
    assert domains["scummvm"]["fullscreen"] == "false"
    assert domains["scummvm"]["music_volume"] == "192"
    # A game domain the broker did not write must come out of this untouched.
    assert domains["monkey"]["path"] == "/romm/monkey"


def test_a_pin_the_ini_never_had_is_added(dirs: dict[str, Path]) -> None:
    """An ini written before a pin existed gains it rather than keeping the default."""
    write_ini(dirs["ini"], "[scummvm]\nmusic_volume=192")

    scummvm.patch_ini()

    assert scummvm._ini_domains()["scummvm"]["gfx_mode"] == "surfacesdl"


def test_a_written_ini_ends_in_a_newline(dirs: dict[str, Path]) -> None:
    """A final line without one is not parsed by every reader."""
    scummvm.patch_ini()

    assert dirs["ini"].read_text().endswith("\n")


def test_an_ini_write_that_fails_leaves_the_previous_one_whole(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncating write would leave ScummVM reading a half file for its savepath.

    An ini that stops short of `savepath` sends the session's saves to the
    default directory, where the dump does not look for them.
    """
    write_ini(dirs["ini"], "[scummvm]\nsavepath=/keep/me\nmusic_volume=192")
    before = dirs["ini"].read_text()

    def no_rename(src: object, dst: object) -> None:
        """Fail the rename that publishes the file.

        Args:
            src: The temp file.
            dst: The ini it would replace.

        Raises:
            OSError: Always.
        """
        raise OSError("no space left on device")

    monkeypatch.setattr(scummvm.os, "replace", no_rename)

    scummvm.patch_ini()  # must not raise

    assert dirs["ini"].read_text() == before
    assert not list(dirs["config"].glob("*.tmp"))


def test_an_ini_without_an_application_section_gains_one(dirs: dict[str, Path]) -> None:
    """An ini holding only game domains still gets the settings the macros need."""
    write_ini(dirs["ini"], "[monkey]\ngameid=monkey\npath=/romm/monkey")

    scummvm.patch_ini()

    domains = scummvm._ini_domains()
    assert domains["scummvm"]["gui_saveload_chooser"] == "list"
    assert domains["monkey"]["gameid"] == "monkey"


def test_the_menu_key_is_pinned_on_the_global_keymap(dirs: dict[str, Path]) -> None:
    """The macros open the menu with a key they bound themselves.

    ScummVM's own binding carries a modifier, which does not survive injection
    into the container's Xwayland, and the unmodified key the menu also answers
    to belongs to the engine keymap, which an engine may take for itself.
    """
    scummvm.patch_ini()

    assert scummvm._ini_domains()["keymapper"]["keymap_global_MENU"] == scummvm.MENU_KEY


def test_other_keymaps_the_user_bound_survive(dirs: dict[str, Path]) -> None:
    """Only the menu action is pinned; the rest of the keymapper is the user's."""
    write_ini(
        dirs["ini"],
        """
        [keymapper]
        keymap_global_MENU=C+F5
        keymap_engine-default_SKIP=SPACE
        """,
    )

    scummvm.patch_ini()

    keymapper = scummvm._ini_domains()["keymapper"]
    assert keymapper["keymap_global_MENU"] == scummvm.MENU_KEY
    assert keymapper["keymap_engine-default_SKIP"] == "SPACE"


def test_a_fresh_ini_carries_both_pinned_sections(dirs: dict[str, Path]) -> None:
    """A container that has never run ScummVM still gets a usable menu key."""
    scummvm.patch_ini()

    domains = scummvm._ini_domains()
    assert domains["scummvm"]["gui_saveload_chooser"] == "list"
    assert domains["keymapper"]["keymap_global_MENU"] == scummvm.MENU_KEY


def test_the_macro_opens_the_menu_with_the_pinned_key(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key the macro sends is the one the ini binds, with no modifier."""
    (dirs["saves"] / "monkey.s01").write_bytes(b"state")
    xdo = Xdo()
    emu = running(monkeypatch, xdo)

    emu.load_state(1)

    assert ("key", "--clearmodifiers", scummvm.MENU_KEY) in xdo.calls


def test_a_macro_focuses_the_window_before_sending_keys(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """XTEST delivers to whatever holds focus, so an unfocused game loses the keys."""
    (dirs["saves"] / "monkey.s01").write_bytes(b"state")
    xdo = Xdo()
    emu = running(monkeypatch, xdo)

    emu.load_state(1)

    activate = [c for c in xdo.calls if c and c[0] == "windowactivate"]
    assert activate and activate[0][1] == "--sync"
    assert xdo.calls.index(activate[0]) < xdo.calls.index(
        ("key", "--clearmodifiers", scummvm.MENU_KEY)
    )


def test_the_window_is_grown_to_the_display(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filling the stream is the window manager's job, not SDL's.

    The move comes first: a window the WM placed at an offset would otherwise
    be sized to the display and hang off the bottom right of it.
    """
    xdo = Xdo()
    emu = running(monkeypatch, xdo)
    monkeypatch.setattr(scummvm, "FILL_SCREEN_POLL", 0.0)
    alive = iter([True, False])
    monkeypatch.setattr(Scummvm, "alive", lambda self: next(alive, False))

    emu._fill_screen(emu._launch_seq)

    assert ("windowmove", "4242", "0", "0") in xdo.calls
    assert ("windowsize", "4242", "1280", "720") in xdo.calls
    assert xdo.calls.index(("windowmove", "4242", "0", "0")) < xdo.calls.index(
        ("windowsize", "4242", "1280", "720")
    )


def test_the_window_follows_a_display_that_changes_size(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The display is sized by the streaming client, not by the launch.

    A game started before a browser connects is grown to whatever the last
    session left behind; when the client then resizes the display, a window
    sized once would sit in the top left corner of a bigger screen.
    """
    xdo = Xdo()
    emu = running(monkeypatch, xdo)
    monkeypatch.setattr(scummvm, "FILL_SCREEN_POLL", 0.0)
    sizes = iter(["1024 768", "1024 768", "1920 888"])
    alive = iter([True, True, True, False])
    monkeypatch.setattr(Scummvm, "alive", lambda self: next(alive, False))

    def display(*args: str, **kwargs: Any) -> Optional[str]:
        """Answer a moving display size, everything else the way Xdo does."""
        if args and args[0] == "getdisplaygeometry":
            return next(sizes, "1920 888")
        return xdo(*args, **kwargs)

    monkeypatch.setattr(Scummvm, "_xdotool", lambda self, *a, **k: display(*a, **k))

    emu._fill_screen(emu._launch_seq)

    assert ("windowsize", "4242", "1024", "768") in xdo.calls
    assert ("windowsize", "4242", "1920", "888") in xdo.calls


def test_an_unchanged_display_is_not_resized_again(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Following the display must not mean an xdotool call every tick."""
    xdo = Xdo()
    emu = running(monkeypatch, xdo)
    monkeypatch.setattr(scummvm, "FILL_SCREEN_POLL", 0.0)
    alive = iter([True, True, True, False])
    monkeypatch.setattr(Scummvm, "alive", lambda self: next(alive, False))

    emu._fill_screen(emu._launch_seq)

    assert len([c for c in xdo.calls if c and c[0] == "windowsize"]) == 1


def test_an_unreadable_display_size_leaves_the_window_alone(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Growing the window is cosmetic, so a failure never touches the game."""
    xdo = Xdo()
    xdo.display = "not a size"
    emu = running(monkeypatch, xdo)
    monkeypatch.setattr(scummvm, "FILL_SCREEN_POLL", 0.0)
    alive = iter([True, True, False])
    monkeypatch.setattr(Scummvm, "alive", lambda self: next(alive, False))

    emu._fill_screen(emu._launch_seq)

    assert not [c for c in xdo.calls if c and c[0] in ("windowmove", "windowsize")]


def test_a_superseded_launch_does_not_resize(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relaunch must not have the previous launch's resize land on it."""
    xdo = Xdo()
    emu = running(monkeypatch, xdo)

    emu._fill_screen(emu._launch_seq - 1)

    assert not [c for c in xdo.calls if c and c[0] in ("windowmove", "windowsize")]


def test_fullscreen_is_pinned_off_whatever_the_ini_said(dirs: dict[str, Path]) -> None:
    """ScummVM's own fullscreen is never used.

    It makes SDL grab and confine the pointer, which is fatal against an
    injected absolute pointer; the window manager grows the window instead.
    """
    write_ini(dirs["ini"], "[scummvm]\nfullscreen=true")

    scummvm.patch_ini()

    assert scummvm._ini_domains()["scummvm"]["fullscreen"] == "false"


# -- GMM hotkeys --


def test_an_untranslated_gui_uses_the_english_hotkeys(dirs: dict[str, Path]) -> None:
    """With no GUI language set the buttons answer to their English letters."""
    assert scummvm.gmm_hotkeys() == ("s", "l")


def test_a_translated_gui_uses_its_own_hotkeys(dirs: dict[str, Path]) -> None:
    """The button letters follow the translated label's markup.

    French turns `~L~oad` into `~C~harger`, so the load button answers to `c`.
    """
    write_ini(dirs["ini"], "[scummvm]\ngui_language=fr")

    assert scummvm.gmm_hotkeys() == ("s", "c")


def test_a_language_with_no_table_entry_falls_back_to_english(dirs: dict[str, Path]) -> None:
    """A GUI language that keeps the English letters needs no entry of its own."""
    write_ini(dirs["ini"], "[scummvm]\ngui_language=nl")

    assert scummvm.gmm_hotkeys() == ("s", "l")


# -- Slot naming --


def test_a_slot_has_both_canonical_names() -> None:
    """A slot is spelled either way, depending on the engine that wrote it."""
    assert scummvm.slot_names("monkey", 1) == ("monkey.s01", "monkey.001")


def test_the_newest_of_the_two_spellings_wins(dirs: dict[str, Path]) -> None:
    """An engine writes one spelling, so the newest file is the slot's save."""
    old = dirs["saves"] / "monkey.001"
    new = dirs["saves"] / "monkey.s01"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    assert scummvm.slot_file("monkey", 1) == new


def test_another_game_save_never_answers_for_this_one(dirs: dict[str, Path]) -> None:
    """The slot is read by target, so another game's save in it is not this one's."""
    (dirs["saves"] / "indy3.s01").write_bytes(b"other")

    assert scummvm.slot_file("monkey", 1) is None


def test_nothing_booted_means_no_slot_file(dirs: dict[str, Path]) -> None:
    """Without a target there is no name to look for."""
    (dirs["saves"] / "monkey.s01").write_bytes(b"save")

    assert scummvm.slot_file(None, 1) is None


# -- State routes --


def booted(target: str = "monkey") -> Scummvm:
    """Build a launcher that has already booted `target`.

    Args:
        target: The target the session is running.

    Returns:
        The launcher, with its target set the way a launch sets it.
    """
    emu = Scummvm()
    emu._target = target
    return emu


def test_a_pushed_state_is_renamed_onto_the_booted_target(dirs: dict[str, Path]) -> None:
    """ScummVM finds a save by name, so a state captured elsewhere is renamed.

    A multilingual folder registers one target per language, and RomM stores
    whichever was booted at the time.
    """
    emu = booted("gob1-cd-fr")

    assert emu.state_target("gob1-cd-de.s07") == dirs["saves"] / "gob1-cd-fr.s01"


def test_a_pushed_state_keeps_the_spelling_it_arrived_in(dirs: dict[str, Path]) -> None:
    """Which spelling an engine writes is the engine's business, so it is kept."""
    emu = booted()

    assert emu.state_target("monkey.007") == dirs["saves"] / "monkey.001"


@pytest.mark.parametrize(
    "filename",
    [
        "",
        ".",
        "..",
        "monkey",
        "monkey.sav",
        "../monkey.s01",
        "sub/monkey.s01",
        "monkey.s1",
        "monkey.s01\n",
        "monkey.s\u0660\u0661",
    ],
)
def test_a_name_that_is_not_a_save_is_refused(dirs: dict[str, Path], filename: str) -> None:
    """Only a ScummVM save name is written, which is what bounds the push.

    Args:
        dirs: The redirected directories.
        filename: A name no ScummVM engine would have written.
    """
    assert booted().state_target(filename) is None


def test_a_state_push_needs_a_booted_target(dirs: dict[str, Path]) -> None:
    """With nothing booted there is no target to name the file after."""
    assert Scummvm().state_target("monkey.s01") is None


def test_the_working_slot_is_served_for_the_booted_game(dirs: dict[str, Path]) -> None:
    """The state route serves this game's working slot, not the newest save."""
    (dirs["saves"] / "monkey.s01").write_bytes(b"state")
    (dirs["saves"] / "monkey.s02").write_bytes(b"a manual save")

    assert booted().state_path() == dirs["saves"] / "monkey.s01"


def test_every_leftover_save_is_emptied_before_a_session(dirs: dict[str, Path]) -> None:
    """The whole save directory goes, not just the slot the broker writes.

    A save the last player made from inside the game's own menu is named for the
    slot they picked, and nothing in that name says whose session wrote it, so
    leaving it behind hands it to this player and to their dump.

    The target only exists once a game has booted, which is after this runs.
    """
    stale = (
        dirs["saves"] / "monkey.s01",
        dirs["saves"] / "indy3.001",
        dirs["saves"] / "monkey.s02",
        dirs["saves"] / "monkey.s00",
    )
    for path in stale:
        path.write_bytes(b"save")
    nested = dirs["saves"] / "timbre" / "timbre.s00"
    nested.parent.mkdir()
    nested.write_bytes(b"save")

    Scummvm().clear_working_slot()

    assert not any(path.exists() for path in stale)
    assert not nested.parent.exists()
    # The directory itself stays, so a launch has somewhere to write.
    assert dirs["saves"].is_dir()


def test_an_empty_save_dir_is_nothing_to_clear(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A container whose save directory does not exist yet clears cleanly."""
    monkeypatch.setattr(scummvm.Scummvm, "save_root", tmp_path / "absent")

    Scummvm().clear_working_slot()


# -- Archive classification --


def test_the_working_slot_is_the_only_state_in_the_archive(dirs: dict[str, Path]) -> None:
    """Saves and states share a directory, so the name is what separates them."""
    emu = booted()

    assert emu.save_file_kind("saves/monkey.s01") == "state"
    assert emu.save_file_kind("saves/monkey.001") == "state"
    assert emu.save_file_kind("saves/monkey.s02") == "save"
    # ScummVM's own autosave is the game's, not the broker's state.
    assert emu.save_file_kind("saves/monkey.s00") == "save"


def test_another_game_working_slot_is_not_this_session_state(dirs: dict[str, Path]) -> None:
    """A save in the same slot under another target is the player's, not the state."""
    assert booted().save_file_kind("saves/indy3.s01") == "save"


def test_with_nothing_booted_every_member_is_a_save(dirs: dict[str, Path]) -> None:
    """Without a target nothing can be claimed as this session's state."""
    assert Scummvm().save_file_kind("saves/monkey.s01") == "save"


# -- Launching --


class Spawned:
    """The argv and environment a stubbed `_spawn` was called with.

    Attributes:
        cmd: The argv, or None when nothing was spawned.
        env: The environment, or None when nothing was spawned.
    """

    def __init__(self) -> None:
        """Start with nothing spawned."""
        self.cmd: Optional[list[str]] = None
        self.env: Optional[dict[str, str]] = None


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> Spawned:
    """Record what `launch` would have spawned instead of spawning it.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.

    Returns:
        The recorder the launch writes into.
    """
    record = Spawned()

    def fake_spawn(self: Scummvm, cmd: list[str], env: dict[str, str], **kwargs: Any) -> None:
        """Record the launch and set a process handle the way a spawn does.

        Args:
            self: The launcher spawning.
            cmd: The argv.
            env: The environment.
            **kwargs: The rest of the spawn arguments, ignored.
        """
        record.cmd = cmd
        record.env = env
        self._proc = SimpleNamespace(pid=4242, poll=lambda: None)

    monkeypatch.setattr(Scummvm, "_spawn", fake_spawn)
    # Growing the window is a launch side effect that reaches for the real
    # xdotool; the tests that care drive `_fill_screen` themselves.
    monkeypatch.setattr(scummvm, "FILL_SCREEN", False)
    return record


def registered(dirs: dict[str, Path], target: str = "monkey") -> Path:
    """Create a game folder and register it in the ini.

    Args:
        dirs: The redirected directories.
        target: The target name to register it under.

    Returns:
        The game folder.
    """
    folder = game_folder(dirs["roms"])
    write_ini(dirs["ini"], f"[{target}]\ngameid=monkey\npath={folder.resolve()}")
    return folder


def test_a_launch_boots_the_target_with_the_broker_savepath(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """The target boots the game, and it comes last on the command line.

    ScummVM's option parsing stops at the first non-option argument, so an
    option after the target is read as a stray argument and nothing launches.
    """
    folder = registered(dirs)
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), None)

    assert spawned.cmd[-1] == "monkey"
    assert f"--savepath={dirs['saves']}" in spawned.cmd
    assert not any(arg.startswith("--save-slot") for arg in spawned.cmd)


def test_a_launch_tells_scummvm_which_ini_the_broker_pinned(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """The launch names the config the broker patched.

    Nothing else states it, so without the flag ScummVM resolves its own and
    the pinned settings and the registered target sit in a file this run never
    opens.
    """
    folder = registered(dirs)
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), None)

    assert f"--config={dirs['ini']}" in spawned.cmd


def test_a_launch_forces_sdl_onto_x11(dirs: dict[str, Path], spawned: Spawned) -> None:
    """SDL would pick Wayland, where the menu macros could never be injected."""
    folder = registered(dirs)
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), None)

    assert spawned.env["SDL_VIDEODRIVER"] == "x11"


def test_a_resume_with_its_state_on_disk_loads_at_boot(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """Every engine reads the boot slot in its startup path, so no menu is needed."""
    folder = registered(dirs)
    (dirs["saves"] / "monkey.s01").write_bytes(b"state")
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), 3)

    assert "--save-slot=1" in spawned.cmd


def test_a_resume_whose_state_has_not_arrived_defers(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RomM pushes its pick after activate returns, so that one goes in over the menu."""
    folder = registered(dirs)
    deferred: list[int] = []
    monkeypatch.setattr(
        Scummvm, "_deferred_load_state", lambda self, seq: deferred.append(seq)
    )
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), 3)

    assert not any(arg.startswith("--save-slot") for arg in spawned.cmd)
    assert deferred == [1]


def test_a_launch_registers_a_folder_the_ini_does_not_know(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder with no domain is registered before it can be booted."""
    folder = game_folder(dirs["roms"])
    monkeypatch.setattr(scummvm, "register_target", lambda rom_dir, language=None: "monkey")
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), None)

    assert spawned.cmd[-1] == "monkey"


def test_a_folder_with_no_detectable_game_fails_the_launch(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing to boot the launch fails rather than starting an empty session."""
    folder = game_folder(dirs["roms"])
    monkeypatch.setattr(scummvm, "register_target", lambda rom_dir, language=None: None)
    emu = Scummvm()

    with pytest.raises(RuntimeError):
        emu.launch(emu.resolve_rom_file(folder), None)


def test_a_launch_pins_the_ini_first(dirs: dict[str, Path], spawned: Spawned) -> None:
    """The chooser the macros walk is pinned before the game can open one."""
    folder = registered(dirs)
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), None)

    assert scummvm._ini_domains()["scummvm"]["gui_saveload_chooser"] == "list"


# -- The save and load macros --


class Xdo:
    """A stubbed xdotool that records its calls and can write the slot's save.

    Attributes:
        calls: Every argument list the launcher passed, in order.
        writes: The file a `Return` creates, standing in for ScummVM's write.
        display: What `getdisplaygeometry` answers.
    """

    def __init__(self, writes: Optional[Path] = None) -> None:
        """Start with nothing recorded.

        Args:
            writes: The save file a confirming keystroke should create, if any.
        """
        self.calls: list[tuple[str, ...]] = []
        self.writes = writes
        self.display = "1280 720"

    def __call__(self, *args: str, **kwargs: Any) -> Optional[str]:
        """Record one xdotool call and answer the way the real one would.

        Args:
            *args: The xdotool arguments.
            **kwargs: The real helper's keyword options, ignored here.

        Returns:
            A window id for a search, an empty string for anything else.
        """
        self.calls.append(args)
        if args and args[0] == "search":
            return "4242\n"
        if args and args[0] == "getdisplaygeometry":
            return self.display
        if self.writes is not None and "Return" in args:
            self.writes.write_bytes(b"state")
        return ""

    def keys(self) -> list[tuple[str, ...]]:
        """The key-sending calls only.

        Returns:
            Every call whose first argument is `key`.
        """
        return [call for call in self.calls if call and call[0] == "key"]


def running(monkeypatch: pytest.MonkeyPatch, xdo: Xdo, target: str = "monkey") -> Scummvm:
    """Build a launcher with a running game and a stubbed xdotool.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
        xdo: The xdotool stub to install.
        target: The target the session is running.

    Returns:
        The launcher, ready for a macro.
    """
    monkeypatch.setattr(
        Scummvm, "_xdotool", lambda self, *args, **kwargs: xdo(*args, **kwargs)
    )
    emu = booted(target)
    emu._proc = SimpleNamespace(pid=4242, poll=lambda: None)
    return emu


def test_a_save_walks_to_the_slot_and_commits_the_description(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chooser opens with nothing selected, so slot N takes N+1 downs.

    In save mode the first Return starts editing the slot's description and the
    second commits it, which is also the save.
    """
    xdo = Xdo(writes=dirs["saves"] / "monkey.s01")
    emu = running(monkeypatch, xdo)

    assert emu.save_state(7) is True
    assert xdo.keys()[-1][-4:] == ("Down", "Down", "Return", "Return")


def test_a_save_is_only_confirmed_by_the_write(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The macro is silent, so an engine that declines the save must not read as one.

    Engines without runtime save support put up a message dialog instead, which
    the macro then has to dismiss so the game is not left paused in a menu.
    """
    monkeypatch.setattr(scummvm, "STATE_WAIT", 0.0)
    xdo = Xdo()
    emu = running(monkeypatch, xdo)

    assert emu.save_state(1) is False
    assert xdo.keys()[-1][-1] == "Escape"


def test_a_load_walks_to_the_slot_and_activates_it(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """In load mode the list is not editable, so one Return activates the slot."""
    (dirs["saves"] / "monkey.s01").write_bytes(b"state")
    xdo = Xdo()
    emu = running(monkeypatch, xdo)

    assert emu.load_state(4) is True
    assert xdo.keys()[-1][-3:] == ("Down", "Down", "Return")


def test_a_load_of_an_empty_slot_is_refused(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walking to an empty row would report success having loaded nothing."""
    xdo = Xdo()
    emu = running(monkeypatch, xdo)

    assert emu.load_state(1) is False
    assert xdo.calls == []


def test_a_macro_without_a_window_fails(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no window to send to there is nothing to drive."""
    monkeypatch.setattr(Scummvm, "_xdotool", lambda self, *args, **kwargs: None)
    emu = booted()
    emu._proc = SimpleNamespace(pid=4242, poll=lambda: None)

    assert emu.save_state(1) is False


def test_the_save_hotkey_follows_the_gui_language(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A translated GUI moves the button letter, and the macro has to follow."""
    write_ini(dirs["ini"], "[scummvm]\ngui_language=fr")
    (dirs["saves"] / "monkey.s01").write_bytes(b"state")
    xdo = Xdo()
    emu = running(monkeypatch, xdo)

    emu.load_state(1)

    assert ("type", "c") in xdo.calls


def test_a_save_still_being_written_is_not_confirmed(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that keeps growing is a write in progress, not a finished save.

    The caller stops ScummVM the moment this returns and the archive is zipped
    right after, so confirming the first differing stat sends SIGTERM into the
    write and ships the fragment to RomM as the player's progress.
    """
    monkeypatch.setattr(scummvm, "STATE_WAIT", 0.5)
    monkeypatch.setattr(scummvm, "STATE_STABLE", 10.0)
    slot = dirs["saves"] / "monkey.s01"
    xdo = Xdo(writes=slot)
    emu = running(monkeypatch, xdo)

    growing = [b"partial", b"partial and more", b"partial and more still"]

    def keep_growing(target: Optional[str], number: int) -> dict[str, tuple[float, int]]:
        """Report a slot whose size never holds still.

        Args:
            target: The booted target.
            number: The slot number.

        Returns:
            A stamp for the slot, one size larger on each call.
        """
        if growing:
            slot.write_bytes(growing.pop(0))
        return {"monkey.s01": (time.time(), slot.stat().st_size)}

    monkeypatch.setattr(scummvm, "_slot_stamp", keep_growing)

    assert emu.save_state(1) is False


def test_a_save_is_confirmed_once_its_write_holds_still(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slot that stops changing for the settle window is a finished save."""
    monkeypatch.setattr(scummvm, "STATE_WAIT", 5.0)
    monkeypatch.setattr(scummvm, "STATE_STABLE", 0.2)
    xdo = Xdo(writes=dirs["saves"] / "monkey.s01")
    emu = running(monkeypatch, xdo)

    assert emu.save_state(1) is True


def test_an_empty_slot_file_is_not_a_finished_save(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file created but not yet written to would archive as an empty save."""
    monkeypatch.setattr(scummvm, "STATE_WAIT", 0.5)
    slot = dirs["saves"] / "monkey.s01"

    class TouchOnly(Xdo):
        """An xdotool whose confirming keystroke only creates the file."""

        def __call__(self, *args: str, **kwargs: Any) -> Optional[str]:
            """Create an empty slot file rather than writing a save into it.

            Args:
                *args: The xdotool arguments.
                **kwargs: The real helper's keyword options, ignored here.

            Returns:
                Whatever the base stub answers.
            """
            result = super().__call__(*args, **kwargs)
            if "Return" in args:
                slot.write_bytes(b"")
            return result

    emu = running(monkeypatch, TouchOnly())

    assert emu.save_state(1) is False


# -- Exit --


def test_an_exit_that_saves_reports_the_file(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit report names the state RomM is about to pull."""
    state = dirs["saves"] / "monkey.s01"
    xdo = Xdo(writes=state)
    emu = running(monkeypatch, xdo)
    monkeypatch.setattr(Scummvm, "stop", lambda self: None)

    report = emu.save_and_exit(0)

    assert report["state_saved"] is True
    assert report["state_slot"] == 1
    assert report["state_file"]["path"] == str(state)


def test_an_exit_without_a_slot_writes_no_state(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exiting without saving still stops, and still ships the game's own saves."""
    xdo = Xdo()
    emu = running(monkeypatch, xdo)
    stopped: list[bool] = []
    monkeypatch.setattr(Scummvm, "stop", lambda self: stopped.append(True))

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert xdo.calls == []
    assert stopped == [True]


def test_an_exit_whose_save_failed_reports_it(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine that cannot save from the menu must not report a state that is not there."""
    monkeypatch.setattr(scummvm, "STATE_WAIT", 0.0)
    xdo = Xdo()
    emu = running(monkeypatch, xdo)
    monkeypatch.setattr(Scummvm, "stop", lambda self: None)

    report = emu.save_and_exit(1)

    assert report["state_saved"] is False
    assert report["state_file"] is None


# -- The deferred resume --


def test_a_deferred_resume_loads_once_the_state_arrives(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push lands after activate returns, and the load follows it."""
    monkeypatch.setattr(scummvm, "RESUME_LOAD_SETTLE", 0.0)
    (dirs["saves"] / "monkey.s01").write_bytes(b"state")
    loaded: list[int] = []
    monkeypatch.setattr(Scummvm, "load_state", lambda self, slot: loaded.append(slot) is None)
    emu = booted()
    emu._launch_seq = 1

    emu._deferred_load_state(1)

    assert loaded == [1]


def test_a_deferred_resume_that_never_gets_a_state_gives_up(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume nobody ever pushed leaves the game running rather than hanging."""
    monkeypatch.setattr(scummvm, "RESUME_LOAD_WAIT", 0.0)
    loaded: list[int] = []
    monkeypatch.setattr(Scummvm, "load_state", lambda self, slot: loaded.append(slot) is None)

    booted()._deferred_load_state(1)

    assert loaded == []


def test_a_superseded_launch_gets_no_stray_load(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second launch during the wait must not be loaded into by the first."""
    monkeypatch.setattr(scummvm, "RESUME_LOAD_SETTLE", 0.0)
    (dirs["saves"] / "monkey.s01").write_bytes(b"state")
    loaded: list[int] = []
    monkeypatch.setattr(Scummvm, "load_state", lambda self, slot: loaded.append(slot) is None)
    emu = booted()
    emu._launch_seq = 2

    emu._deferred_load_state(1)

    assert loaded == []


def test_waiting_for_a_state_returns_as_soon_as_it_lands(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait is what keeps a deferred load off a slot that is still empty."""
    emu = booted()

    assert emu.wait_for_state(time.monotonic() + 0.2, poll=0.05) is False

    (dirs["saves"] / "monkey.s01").write_bytes(b"state")

    assert emu.wait_for_state(time.monotonic() + 0.2, poll=0.05) is True

# -- Language --


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fr", "fr"),
        ("FR", "fr"),
        ("  de  ", "de"),
        # ScummVM spells a few of these its own way.
        ("pt-br", "br"),
        ("pt_BR", "br"),
        ("jp", "ja"),
        ("zh-hans", "cn"),
        ("no", "nb"),
        # A locale tag keeps its language when the region says nothing extra.
        ("fr_FR", "fr"),
        ("en_GB", "en"),
        ("zh_TW", "tw"),
        ("fr-CA", "fr-ca"),
        # No preference, rather than a failure, for anything unusable.
        ("", None),
        ("xx", None),
        ("klingon", None),
        (None, None),
        (123, None),
    ],
)
def test_a_language_reduces_to_what_scummvm_accepts(raw: object, expected: object) -> None:
    """Callers send ISO-ish codes; ScummVM has its own spellings.

    Args:
        raw: The language as the payload carried it.
        expected: The code ScummVM should be given, or None for no preference.
    """
    assert scummvm.normalize_language(raw) == expected


def test_a_multilingual_folder_boots_the_language_asked_for(dirs: dict[str, Path]) -> None:
    """The domain decides which variant's resources load, so it has to match.

    `--language` alone does not reroute a launch to another variant.
    """
    folder = game_folder(dirs["roms"], "gob1")
    write_ini(
        dirs["ini"],
        f"""
        [gob1-cd-de]
        gameid=gob1
        language=de
        path={folder}

        [gob1-cd-fr]
        gameid=gob1
        language=fr
        path={folder}
        """,
    )

    assert scummvm.target_for_path(folder, "fr") == "gob1-cd-fr"
    assert scummvm.target_for_path(folder, "de") == "gob1-cd-de"


def test_a_near_enough_variant_beats_a_foreign_one(dirs: dict[str, Path]) -> None:
    """A `us` variant answers an `en` request; a `de` one does not."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [monkey-de]
        gameid=monkey
        language=de
        path={folder}

        [monkey-us]
        gameid=monkey
        language=us
        path={folder}
        """,
    )

    assert scummvm.target_for_path(folder, "en") == "monkey-us"


def test_a_language_no_variant_has_still_boots_the_game(dirs: dict[str, Path]) -> None:
    """A folder with nothing in the wanted language still plays, deterministically."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [monkey-de]
        gameid=monkey
        language=de
        path={folder}

        [monkey-it]
        gameid=monkey
        language=it
        path={folder}
        """,
    )

    assert scummvm.target_for_path(folder, "fr") == "monkey-de"


def test_without_a_language_the_pick_is_stable(dirs: dict[str, Path]) -> None:
    """No preference must still mean the same target every relaunch.

    The save files are named after it, so a pick that moved would lose them.
    """
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [monkey-fr]
        gameid=monkey
        language=fr
        path={folder}

        [monkey-de]
        gameid=monkey
        language=de
        path={folder}
        """,
    )

    assert scummvm.target_for_path(folder) == "monkey-de"
    assert scummvm.target_for_path(folder) == "monkey-de"


def test_a_launch_boots_the_variant_for_the_session_language(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """The language the activate route set picks the target and the flag."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [monkey-de]
        gameid=monkey
        language=de
        path={folder.resolve()}

        [monkey-fr]
        gameid=monkey
        language=fr
        path={folder.resolve()}
        """,
    )
    emu = Scummvm()
    emu.language = "fr"

    emu.launch(emu.resolve_rom_file(folder), None)

    assert spawned.cmd[-1] == "monkey-fr"
    assert "--language=fr" in spawned.cmd


def test_a_launch_without_a_language_sends_no_flag(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """No language means the game keeps whatever detection gave it."""
    folder = registered(dirs)
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), None)

    assert not any(arg.startswith("--language=") for arg in spawned.cmd)


def test_an_unusable_language_does_not_fail_the_launch(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """A code ScummVM would reject is dropped, not passed on to fail the boot."""
    folder = registered(dirs)
    emu = Scummvm()
    emu.language = "klingon"

    emu.launch(emu.resolve_rom_file(folder), None)

    assert not any(arg.startswith("--language=") for arg in spawned.cmd)
    assert spawned.cmd[-1] == "monkey"


def _multilingual(dirs: dict[str, Path]) -> Path:
    """A folder registered as one German and one French variant of the same game."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [monkey-de]
        gameid=monkey
        language=de
        path={folder.resolve()}

        [monkey-fr]
        gameid=monkey
        language=fr
        path={folder.resolve()}
        """,
    )
    return folder


def test_the_gui_language_picks_the_variant_when_the_rom_names_none(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """A rom with no language of its own boots in the player's own language.

    RomM only knows a game's language when the library says so, and a
    multilingual folder is exactly the case where it usually does not. Without
    this the name breaks the tie and a French player gets the German variant.
    """
    folder = _multilingual(dirs)
    emu = Scummvm()
    emu.gui_language = "fr"

    emu.launch(emu.resolve_rom_file(folder), None)

    assert spawned.cmd[-1] == "monkey-fr"
    assert "--language=fr" in spawned.cmd


def test_the_rom_language_beats_the_gui_language(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """The game's own language wins: the fallback only fills a gap."""
    folder = _multilingual(dirs)
    emu = Scummvm()
    emu.language = "de"
    emu.gui_language = "fr"

    emu.launch(emu.resolve_rom_file(folder), None)

    assert spawned.cmd[-1] == "monkey-de"
    assert "--language=de" in spawned.cmd


def test_the_gui_language_is_pinned_in_the_ini(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """ScummVM's own interface follows the player, and so do the GMM hotkeys.

    `gmm_hotkeys` reads the letters back out of the file, so pinning it here is
    what makes the save and load macros press the translated buttons.
    """
    folder = registered(dirs)
    emu = Scummvm()
    emu.gui_language = "fr"

    emu.launch(emu.resolve_rom_file(folder), None)

    assert scummvm._ini_domains()["scummvm"]["gui_language"] == "fr"
    assert scummvm.gmm_hotkeys() == scummvm._GMM_HOTKEYS["fr"]


def test_no_gui_language_leaves_the_ini_setting_alone(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """An absent language must not overwrite what the user configured."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [scummvm]
        gui_language=it

        [monkey]
        gameid=monkey
        path={folder.resolve()}
        """,
    )
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(folder), None)

    assert scummvm._ini_domains()["scummvm"]["gui_language"] == "it"


def test_an_unusable_gui_language_is_not_pinned(
    dirs: dict[str, Path], spawned: Spawned
) -> None:
    """A code ScummVM would reject never reaches the ini or the target pick."""
    folder = _multilingual(dirs)
    emu = Scummvm()
    emu.gui_language = "klingon"

    emu.launch(emu.resolve_rom_file(folder), None)

    assert "gui_language" not in scummvm._ini_domains()["scummvm"]
    assert not any(arg.startswith("--language=") for arg in spawned.cmd)

# -- declared imports --

_STATE_NAME = f"s{scummvm.STATE_SLOT:02d}"
"""The working slot's `.sNN` spelling."""

_STATE_NUM = f"{scummvm.STATE_SLOT:03d}"
"""The working slot's `.NNN` spelling."""


def _preflight(
    emu: Scummvm, folder: Path, members: dict[str, bytes], v1: Optional[dict[str, bytes]] = None
) -> imports.PreflightResult:
    """Preflight an archive against a game folder, with a resume slot declared.

    Args:
        emu: The emulator.
        folder: The game folder the session boots.
        members: The `.import/...` members.
        v1: Ordinary archive members to carry beside them, or None.

    Returns:
        What preflight decided.
    """
    return preflight_import(
        emu, import_zip(members, v1), rom_file=emu.resolve_rom_file(folder), resume_slot=emu.state_slot
    )


def _refused(result: imports.PreflightResult) -> list[tuple[str, str]]:
    """List a preflight's refusals as sorted reason and member pairs.

    Args:
        result: What preflight decided.

    Returns:
        The pairs.
    """
    return sorted((r.reason, r.member or "") for r in result.refusals)


def _dests(result: imports.PreflightResult) -> dict[str, str]:
    """Map each placed member to its destination.

    Args:
        result: What preflight decided.

    Returns:
        Member name to posix destination.
    """
    return {p.member.name: p.dest.as_posix() for p in result.placements}


def _detects_nothing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Stand in for a `scummvm --add` that finds no game and writes no domain.

    Args:
        cmd: The argv the launcher ran.
        **kwargs: The rest of the subprocess arguments, ignored.

    Returns:
        A clean exit that added nothing.
    """
    return subprocess.CompletedProcess(cmd, 0, "Added 0 games\n", "")


def _never_runs(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Fail the test that reaches for `scummvm --add` when it must not.

    Args:
        cmd: The argv the launcher ran.
        **kwargs: The rest of the subprocess arguments, ignored.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError(f"scummvm must not run here: {cmd}")


def test_saves_and_one_state_land_under_the_booted_target(dirs: dict[str, Path]) -> None:
    """Saves keep their slot, and the state takes the working slot of the booted target."""
    folder = registered(dirs)

    result = _preflight(
        Scummvm(),
        folder,
        {
            ".import/save/monkey.003": b"one",
            ".import/save/saves/monkey.s04": b"two",
            ".import/state/monkey.s07": b"three",
        },
    )

    assert result.refusals == ()
    assert _dests(result) == {
        ".import/save/monkey.003": "saves/monkey.003",
        ".import/save/saves/monkey.s04": "saves/monkey.s04",
        ".import/state/monkey.s07": f"saves/monkey.{_STATE_NAME}",
    }
    assert result.identity == imports.SessionIdentity("monkey", "rom")


@pytest.mark.parametrize(
    ("given", "placed"),
    [
        ("monkey.003", "saves/monkey.003"),
        ("saves/monkey.003", "saves/monkey.003"),
        ("monkey.s04", "saves/monkey.s04"),
        ("monkey.S02", "saves/monkey.s02"),
        ("monkey.s102", "saves/monkey.s102"),
        ("MONKEY.004", "saves/monkey.004"),
    ],
)
def test_a_save_may_be_spelled_in_either_slot_form_and_any_case(
    dirs: dict[str, Path], given: str, placed: str
) -> None:
    """A bare name or one under `saves/`, `.sNN` or `.NNN`, in any case, lands under the ini's spelling."""
    folder = registered(dirs)

    result = _preflight(Scummvm(), folder, {f".import/save/{given}": b"data"})

    assert result.refusals == ()
    assert list(_dests(result).values()) == [placed]


def test_a_save_may_carry_the_target_the_gameid_or_the_engineid(dirs: dict[str, Path]) -> None:
    """Any of the three names a domain answers to is a game the folder holds."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [monkey-fr]
        gameid=monkeyisland
        engineid=scumm
        path={folder.resolve()}
        """,
    )

    result = _preflight(
        Scummvm(),
        folder,
        {
            ".import/save/monkey-fr.003": b"target",
            ".import/save/monkeyisland.004": b"gameid",
            ".import/save/scumm.005": b"engineid",
        },
    )

    assert result.refusals == ()
    assert sorted(_dests(result).values()) == [
        "saves/monkey-fr.003",
        "saves/monkeyisland.004",
        "saves/scumm.005",
    ]


def test_a_save_takes_the_exact_case_the_ini_spells_its_game_in(dirs: dict[str, Path]) -> None:
    """ScummVM finds a save by exact name, so the archive's case gives way to the ini's."""
    folder = game_folder(dirs["roms"])
    write_ini(dirs["ini"], f"[Monkey]\ngameid=monkeyisland\npath={folder.resolve()}")

    result = _preflight(
        Scummvm(),
        folder,
        {".import/save/MONKEY.004": b"a", ".import/save/monkeyISLAND.005": b"b"},
    )

    assert result.refusals == ()
    assert _dests(result) == {
        ".import/save/MONKEY.004": "saves/Monkey.004",
        ".import/save/monkeyISLAND.005": "saves/monkeyisland.005",
    }


def test_a_save_for_another_variant_keeps_that_variants_name(dirs: dict[str, Path]) -> None:
    """A German save in a French session is placed as the German one, not renamed onto French."""
    folder = _multilingual(dirs)
    emu = Scummvm()
    emu.language = "fr"

    result = _preflight(emu, folder, {".import/save/monkey-de.003": b"data"})

    assert result.refusals == ()
    assert list(_dests(result).values()) == ["saves/monkey-de.003"]


def test_a_save_for_another_game_is_an_identity_mismatch(dirs: dict[str, Path]) -> None:
    """A stem that no domain of this folder answers to is refused, whatever else is registered."""
    folder = game_folder(dirs["roms"])
    other = game_folder(dirs["roms"], "other")
    write_ini(
        dirs["ini"],
        f"""
        [monkey]
        gameid=monkey
        path={folder.resolve()}

        [tentacle]
        gameid=tentacle
        path={other.resolve()}
        """,
    )

    result = _preflight(Scummvm(), folder, {".import/save/tentacle.003": b"data"})

    assert _refused(result) == [("identity_mismatch", ".import/save/tentacle.003")]
    assert result.refusals[0].detail == "member tentacle, this game monkey"


@pytest.mark.parametrize(
    "given",
    [
        "SAVEGAME.001",
        "savegame.003",
        "Game.srm",
        "monkey.state1",
        "monkey.state.auto",
        "scummvm.ini",
        "monkey.0001",
        "monkey.s1",
        "monkey.003.bak",
        "monkey.\u0663\u0663\u0663",
        "sub/monkey.003",
        "saves/sub/monkey.003",
    ],
)
def test_a_name_that_is_not_a_save_is_an_unrecognised_layout(dirs: dict[str, Path], given: str) -> None:
    """Only a single `<game>.NNN` or `<game>.sNN` file is a save, and `SAVEGAME` names no game."""
    folder = registered(dirs)

    result = _preflight(Scummvm(), folder, {f".import/save/{given}": b"data"})

    assert _refused(result) == [("unrecognised_layout", f".import/save/{given}")]


@pytest.mark.parametrize("given", ["monkey.s01", "monkey.001", "MONKEY.S01", "saves/monkey.001"])
def test_a_save_in_the_working_slot_is_a_destination_conflict(dirs: dict[str, Path], given: str) -> None:
    """The working slot is the broker's, so a save cannot be filed there; it is declared as the state."""
    folder = registered(dirs)

    result = _preflight(Scummvm(), folder, {f".import/save/{given}": b"data"})

    assert _refused(result) == [("destination_conflict", f".import/save/{given}")]
    assert result.refusals[0].detail == (
        f"slot {scummvm.STATE_SLOT} is the broker's working slot; declare it as a state"
    )


def test_the_working_slot_under_another_name_is_an_ordinary_save(dirs: dict[str, Path]) -> None:
    """Only the booted target's own slot is the broker's, so another variant's or the gameid's is not."""
    folder = _multilingual(dirs)
    emu = Scummvm()
    emu.language = "fr"

    result = _preflight(
        emu, folder, {".import/save/monkey-de.s01": b"variant", ".import/save/monkey.001": b"gameid"}
    )

    assert result.refusals == ()
    assert sorted(_dests(result).values()) == ["saves/monkey-de.s01", "saves/monkey.001"]


def test_an_unregistered_folder_is_registered_once_for_the_whole_archive(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight registers the folder as launch would, once, and places every member against the result."""
    folder = game_folder(dirs["roms"])
    add = AddRuns(folder.resolve())
    monkeypatch.setattr(scummvm.subprocess, "run", add)

    result = _preflight(
        Scummvm(),
        folder,
        {
            ".import/save/monkey-fr.003": b"one",
            ".import/save/monkey.004": b"two",
            ".import/state/monkey.s02": b"three",
        },
    )

    assert result.refusals == ()
    assert len(add.attempts) == 1
    assert sorted(_dests(result).values()) == [
        "saves/monkey-fr.003",
        "saves/monkey-fr.s01",
        "saves/monkey.004",
    ]


def test_a_folder_with_no_detectable_game_gives_identity_unknown(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing to boot there is no game to hold a save or a state to."""
    folder = game_folder(dirs["roms"])
    monkeypatch.setattr(scummvm.subprocess, "run", _detects_nothing)

    result = _preflight(
        Scummvm(), folder, {".import/save/monkey.003": b"one", ".import/state/monkey.s02": b"two"}
    )

    assert _refused(result) == [
        ("identity_unknown", ".import/save/monkey.003"),
        ("identity_unknown", ".import/state/monkey.s02"),
    ]


def test_no_rom_folder_gives_identity_unknown_without_running_scummvm(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch with no game folder has nothing to register, so nothing is run."""
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)

    result = preflight_import(
        Scummvm(),
        import_zip({".import/save/monkey.003": b"data"}),
        rom_file=None,
        resume_slot=scummvm.STATE_SLOT,
    )

    assert _refused(result) == [("identity_unknown", ".import/save/monkey.003")]


@pytest.mark.parametrize(
    ("given", "placed"),
    [
        ("monkey.s07", f"saves/monkey.{_STATE_NAME}"),
        ("monkey.s007", f"saves/monkey.{_STATE_NAME}"),
        ("monkey.005", f"saves/monkey.{_STATE_NUM}"),
        ("saves/monkey.005", f"saves/monkey.{_STATE_NUM}"),
        ("MONKEY.S02", f"saves/monkey.{_STATE_NAME}"),
    ],
)
def test_a_state_takes_the_working_slot_in_the_form_it_arrived_in(
    dirs: dict[str, Path], given: str, placed: str
) -> None:
    """The state's own slot is dropped; the `.sNN` or `.NNN` form is kept, as `state_target` keeps it."""
    folder = registered(dirs)

    result = _preflight(Scummvm(), folder, {f".import/state/{given}": b"data"})

    assert result.refusals == ()
    assert list(_dests(result).values()) == [placed]


def test_a_state_from_another_variant_is_renamed_onto_the_booted_target(dirs: dict[str, Path]) -> None:
    """A state captured under the German target resumes the French session."""
    folder = _multilingual(dirs)
    emu = Scummvm()
    emu.language = "fr"

    result = _preflight(emu, folder, {".import/state/monkey-de.s02": b"data"})

    assert result.refusals == ()
    assert list(_dests(result).values()) == [f"saves/monkey-fr.{_STATE_NAME}"]


def test_a_state_for_another_game_is_an_identity_mismatch(dirs: dict[str, Path]) -> None:
    """A state is held to the folder's game exactly as a save is."""
    folder = registered(dirs)

    result = _preflight(Scummvm(), folder, {".import/state/tentacle.s01": b"data"})

    assert _refused(result) == [("identity_mismatch", ".import/state/tentacle.s01")]


@pytest.mark.parametrize("given", ["savegame.001", "monkey.state1", "monkey.s1", "sub/monkey.s01"])
def test_a_state_that_is_not_a_save_name_is_an_unrecognised_layout(
    dirs: dict[str, Path], given: str
) -> None:
    """A state is one save-named file, so what is not a save name is not a state either."""
    folder = registered(dirs)

    result = _preflight(Scummvm(), folder, {f".import/state/{given}": b"data"})

    assert _refused(result) == [("unrecognised_layout", f".import/state/{given}")]


def test_a_state_needs_a_resume_slot(dirs: dict[str, Path]) -> None:
    """Without `save.resume_slot` the launch would not resume it, so it is refused."""
    folder = registered(dirs)
    emu = Scummvm()

    result = preflight_import(
        emu,
        import_zip({".import/state/monkey.s02": b"data"}),
        rom_file=emu.resolve_rom_file(folder),
        resume_slot=None,
    )

    assert _refused(result) == [("resume_slot_required", ".import/state/monkey.s02")]


def test_a_memory_card_is_not_accepted(dirs: dict[str, Path]) -> None:
    """ScummVM has no memory card."""
    folder = registered(dirs)

    result = _preflight(Scummvm(), folder, {".import/memcard/monkey.003": b"data"})

    assert _refused(result) == [("kind_not_accepted", ".import/memcard/monkey.003")]


@pytest.mark.parametrize("second", ["monkey.s03", "monkey.003"])
def test_two_states_are_each_refused(dirs: dict[str, Path], second: str) -> None:
    """There is one working slot, so two states compete for it and neither is placed."""
    folder = registered(dirs)

    result = _preflight(
        Scummvm(), folder, {".import/state/monkey.s02": b"one", f".import/state/{second}": b"two"}
    )

    assert _refused(result) == sorted(
        [
            ("destination_conflict", ".import/state/monkey.s02"),
            ("destination_conflict", f".import/state/{second}"),
        ]
    )


def test_a_v1_save_in_the_working_slot_conflicts_with_the_state(dirs: dict[str, Path]) -> None:
    """The archive's own working-slot file would be restored beside the state, so the state is refused.

    `save_file_kind` reads every file as a save until a game has booted, so
    the shared count cannot see this one and `validate_import_plan` does.
    """
    folder = registered(dirs)

    result = _preflight(
        Scummvm(),
        folder,
        {".import/state/monkey.s02": b"state"},
        v1={"saves/monkey.001": b"old"},
    )

    assert _refused(result) == [("destination_conflict", ".import/state/monkey.s02")]
    assert result.refusals[0].expected == "one state per archive"
    assert result.refusals[0].detail == "the archive already carries saves/monkey.001"


def test_a_v1_save_on_the_states_destination_is_refused_once(dirs: dict[str, Path]) -> None:
    """The shared destination check already refuses the state, and the slot check adds nothing."""
    folder = registered(dirs)

    result = _preflight(
        Scummvm(),
        folder,
        {".import/state/monkey.s02": b"state"},
        v1={f"saves/monkey.{_STATE_NAME}": b"old"},
    )

    assert _refused(result) == [("destination_conflict", ".import/state/monkey.s02")]


def test_a_v1_state_already_counted_is_refused_once(dirs: dict[str, Path]) -> None:
    """A booted target makes `save_file_kind` label the v1 slot file a state, and the shared count refuses."""
    folder = registered(dirs)
    emu = Scummvm()
    emu._target = "monkey"

    result = _preflight(
        emu, folder, {".import/state/monkey.s02": b"state"}, v1={"saves/monkey.001": b"old"}
    )

    assert _refused(result) == [("destination_conflict", ".import/state/monkey.s02")]


def test_v1_saves_outside_the_working_slot_do_not_count(dirs: dict[str, Path]) -> None:
    """Another slot, and another game's slot, are the game's own saves."""
    folder = registered(dirs)

    result = _preflight(
        Scummvm(),
        folder,
        {".import/state/monkey.s02": b"state"},
        v1={"saves/monkey.002": b"one", "saves/tentacle.001": b"two"},
    )

    assert result.refusals == ()


def test_an_imported_state_resumes_through_the_launch(dirs: dict[str, Path], spawned: Spawned) -> None:
    """The file lands where `slot_file` finds it, and the launch boots it with `--save-slot`."""
    folder = registered(dirs)
    emu = Scummvm()
    body = import_zip({".import/state/monkey.s07": b"progress", ".import/save/monkey.003": b"save"})
    rom = emu.resolve_rom_file(folder)

    result = preflight_import(emu, body, rom_file=rom, resume_slot=emu.state_slot)
    restore_import(emu, body, result)
    emu.launch(rom, 3)

    working = dirs["saves"] / f"monkey.{_STATE_NAME}"
    assert scummvm.slot_file("monkey", scummvm.STATE_SLOT) == working
    assert working.read_bytes() == b"progress"
    assert (dirs["saves"] / "monkey.003").read_bytes() == b"save"
    assert f"--save-slot={scummvm.STATE_SLOT}" in spawned.cmd
    assert spawned.cmd[-1] == "monkey"


@pytest.mark.parametrize(
    ("language", "gui_language", "expected"),
    [
        (None, None, "monkey-de"),
        ("fr", None, "monkey-fr"),
        (None, "fr", "monkey-fr"),
        ("de", "fr", "monkey-de"),
        ("klingon", "fr", "monkey-fr"),
        ("fr", "klingon", "monkey-fr"),
        ("en", None, "monkey-de"),
    ],
)
def test_preflight_picks_the_target_launch_boots(
    dirs: dict[str, Path],
    spawned: Spawned,
    language: Optional[str],
    gui_language: Optional[str],
    expected: str,
) -> None:
    """A state is renamed onto the target the launch boots, so the two must pick alike."""
    folder = _multilingual(dirs)
    emu = Scummvm()
    emu.language = language
    emu.gui_language = gui_language
    rom = emu.resolve_rom_file(folder)
    ctx = imports.ImportCtx(rom_file=rom, rom=None, memory_card_synced=False, excluded=(), resume_slot=None)

    target, _ = scummvm._session_game(emu, ctx)
    emu.launch(rom, None)

    assert target == expected == emu._target


def test_the_identity_is_the_registered_target_casefolded(dirs: dict[str, Path]) -> None:
    """An activate with no preflight reads the ini, and the value is compared casefolded."""
    folder = game_folder(dirs["roms"])
    write_ini(dirs["ini"], f"[Monkey]\ngameid=monkey\npath={folder.resolve()}")
    emu = Scummvm()

    identity = imports.resolve_activate_identity(emu, folder.resolve(), None)

    assert identity == imports.SessionIdentity("monkey", "rom")
    assert emu._read_target(folder.resolve()) == "Monkey"


def test_an_activate_with_no_preflight_never_registers(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity read is a lookup: registering can take minutes and runs in a worker thread."""
    folder = game_folder(dirs["roms"])
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)

    identity = imports.resolve_activate_identity(Scummvm(), folder.resolve(), None)

    assert identity == imports.SessionIdentity(None, "none")


def test_the_names_are_the_target_the_gameid_and_the_engineid_in_ini_order(
    dirs: dict[str, Path],
) -> None:
    """Each domain of the folder contributes its three names once, and another folder's none."""
    folder = game_folder(dirs["roms"])
    write_ini(
        dirs["ini"],
        f"""
        [monkey-de]
        gameid=monkey
        engineid=scumm
        path={folder.resolve()}

        [other]
        gameid=tentacle
        path=/elsewhere

        [monkey-fr]
        gameid=monkey
        path={folder.resolve()}
        """,
    )

    assert scummvm._folder_names(folder.resolve()) == ["monkey-de", "monkey", "scumm", "monkey-fr"]


def test_a_stem_matches_exactly_before_it_matches_folded() -> None:
    """The identical spelling wins over an earlier one that differs only in case."""
    names = ["monkey", "MONKEY"]

    assert scummvm._match_name("MONKEY", names) == "MONKEY"
    assert scummvm._match_name("Monkey", names) == "monkey"
    assert scummvm._match_name("tentacle", names) is None


def test_the_hotkey_table_holds_the_letters_its_names_say() -> None:
    """The non-Latin hotkeys are written as escapes, so their names are the check that they are right."""
    expected = {
        "be": ("CYRILLIC SMALL LETTER ZE", "CYRILLIC SMALL LETTER A"),
        "el": ("GREEK SMALL LETTER ALPHA", "GREEK SMALL LETTER PHI"),
        "he": ("HEBREW LETTER SHIN", "HEBREW LETTER TET"),
        "nb": ("LATIN SMALL LETTER L", "LATIN SMALL LETTER A WITH RING ABOVE"),
        "ru": ("CYRILLIC SMALL LETTER A", "CYRILLIC SMALL LETTER ZE"),
    }

    for code, names in expected.items():
        assert tuple(unicodedata.name(key) for key in scummvm._GMM_HOTKEYS[code]) == names
    assert Path(scummvm.__file__).read_text(encoding="utf-8").isascii()


# -- Archived games --


def zipped_game(roms: Path, members: dict[str, bytes], name: str = "monkey.zip") -> Path:
    """Write a zip of a game under the ROM root.

    Args:
        roms: The ROM root.
        members: Member path to its contents.
        name: The archive's file name.

    Returns:
        The archive.
    """
    archive = roms / name
    with zipfile.ZipFile(archive, "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    return archive


class AddsWhatItScans:
    """A `scummvm --add` stand-in registering `monkey` at whatever path it was given.

    Like ScummVM, it deduplicates by game: while any `monkey` domain is
    registered, a scan adds nothing and says the game was already added.

    Attributes:
        paths: The `--path` of every scan, in order.
    """

    def __init__(self) -> None:
        """Start with no scans."""
        self.paths: list[Path] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Register the scanned folder the way a detecting ScummVM would.

        Args:
            cmd: The argv the launcher ran.
            **kwargs: The rest of the subprocess arguments, ignored.

        Returns:
            ScummVM's output for a detected game.
        """
        path = Path(next(a for a in cmd if a.startswith("--path="))[len("--path="):])
        self.paths.append(path)
        ini = scummvm.INI_PATH
        existing = ini.read_text() if ini.exists() else ""
        if "[monkey]" in existing:
            return subprocess.CompletedProcess(
                cmd, 0, "Found scumm:monkey, but has already been added, skipping\nAdded 0 games\n", ""
            )
        ini.write_text(existing + f"\n[monkey]\ngameid=monkey\npath={path}\n")
        return subprocess.CompletedProcess(cmd, 0, "Game Added\n", "")


def _count_extractions(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every archive the cache actually extracts, and still extract it.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.

    Returns:
        The archives extracted, appended to as they are.
    """
    extractions: list[Path] = []
    real_extract = scummvm.extraction_cache._extract_archive

    def counting(rom: Path, dest: Path, timeout: float) -> None:
        """Record an extraction and run it.

        Args:
            rom: The archive.
            dest: Where it goes.
            timeout: The extractor timeout.
        """
        extractions.append(rom)
        real_extract(rom, dest, timeout)

    monkeypatch.setattr(scummvm.extraction_cache, "_extract_archive", counting)
    return extractions


def test_an_archived_game_boots_from_its_extraction(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zip is extracted, its wrapper folder walked into, and that folder registered."""
    archive = zipped_game(
        dirs["roms"], {"Monkey Island/MONKEY.000": b"data", "Monkey Island/MONKEY.001": b"d"}
    )
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(archive), None)

    assert spawned.cmd[-1] == "monkey"
    [scanned] = add.paths
    assert scanned.name == "Monkey Island"
    assert scanned.is_relative_to(scummvm.CACHE_DIR)
    assert (scanned / "MONKEY.000").read_bytes() == b"data"
    assert emu.extraction_phase is None


def test_a_second_launch_reuses_the_extraction_and_its_target(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same folder comes back, so the target, and the saves named after it, carry over."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    extractions = _count_extractions(monkeypatch)
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(archive), None)
    emu.launch(emu.resolve_rom_file(archive), None)

    assert len(extractions) == 1
    assert len(add.paths) == 1
    assert spawned.cmd[-1] == "monkey"


def test_a_replaced_archive_is_extracted_again(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-uploaded archive under the same name never boots the old extraction.

    The old extraction is still on disk waiting for eviction, and its domain
    would block the new one's scan. It gives way, so the new extraction
    registers under the same target and the saves named after it still load.
    """
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"old"})
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    extractions = _count_extractions(monkeypatch)
    emu = Scummvm()
    emu.launch(emu.resolve_rom_file(archive), None)
    first = add.paths[0]

    archive.unlink()
    zipped_game(dirs["roms"], {"MONKEY.000": b"new data"})
    emu.launch(emu.resolve_rom_file(archive), None)

    assert len(extractions) == 2
    assert first.is_dir()
    assert scummvm.target_for_path(add.paths[-1]) == "monkey"
    assert scummvm.target_for_path(first) is None
    assert spawned.cmd[-1] == "monkey"


def test_a_live_library_copy_never_gives_way_to_an_extraction(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the broker's own older extractions are superseded, never a folder in the library."""
    loose = dirs["roms"] / "Monkey Loose"
    loose.mkdir()
    (loose / "MONKEY.000").write_bytes(b"data")
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    emu = Scummvm()
    emu.launch(emu.resolve_rom_file(loose), None)
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})

    with pytest.raises(RuntimeError):
        emu.launch(emu.resolve_rom_file(archive), None)

    assert scummvm.target_for_path(loose) == "monkey"


def test_macos_metadata_does_not_hide_the_wrapper_folder(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Mac-made zip's `__MACOSX` fork is not a second top-level entry."""
    archive = zipped_game(
        dirs["roms"], {"Monkey/MONKEY.000": b"data", "__MACOSX/Monkey/._MONKEY.000": b"fork"}
    )
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    emu = Scummvm()

    emu.launch(emu.resolve_rom_file(archive), None)

    assert add.paths[0].name == "Monkey"


def test_an_archive_escaping_the_cache_never_launches(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Zip Slip member fails the launch before anything is written or spawned."""
    archive = zipped_game(dirs["roms"], {"../../escaped.txt": b"x", "MONKEY.000": b"data"})
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)
    emu = Scummvm()

    with pytest.raises(RuntimeError, match="escapes"):
        emu.launch(emu.resolve_rom_file(archive), None)

    assert spawned.cmd is None
    assert not (scummvm.CACHE_DIR.parent / "escaped.txt").exists()


@pytest.mark.parametrize(
    "members",
    [{}, {"__MACOSX/._x": b"fork"}, {".DS_Store": b"x", "Monkey/": b""}],
    ids=["empty", "mac-fork-only", "hidden-and-folders-only"],
)
def test_an_archive_with_no_game_files_is_refused_at_activate(
    dirs: dict[str, Path], members: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its listing already shows there is nothing to boot, so nothing is extracted to find out."""
    archive = zipped_game(dirs["roms"], members)
    extractions = _count_extractions(monkeypatch)

    assert Scummvm().resolve_rom_file(archive) is None
    assert extractions == []


def test_a_corrupt_archive_is_refused_at_activate(dirs: dict[str, Path]) -> None:
    """An archive that cannot even be listed never reaches the launch."""
    archive = dirs["roms"] / "monkey.zip"
    archive.write_bytes(b"PK\x03\x04 not really")

    assert Scummvm().resolve_rom_file(archive) is None


def test_an_archive_with_no_game_in_it_fails_and_caches_nothing(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the activate check, an extraction holding nothing is a launch failure, not a cache entry."""
    archive = zipped_game(dirs["roms"], {"__MACOSX/._x": b"fork"})
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)
    emu = Scummvm()

    with pytest.raises(RuntimeError, match="held no game files"):
        emu.launch(archive.resolve(), None)

    assert spawned.cmd is None
    assert [p.name for p in scummvm.CACHE_DIR.iterdir()] == [".scratch"]
    assert not any((scummvm.CACHE_DIR / ".scratch").iterdir())


def test_an_extraction_cut_short_leaves_nothing_to_boot_from(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure mid-extraction leaves no half-written game for the next launch to trust."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})

    def dies(rom: Path, dest: Path, timeout: float) -> None:
        """Write part of the game, then fail the way a full disk would.

        Args:
            rom: The archive.
            dest: Where it goes.
            timeout: The extractor timeout.

        Raises:
            RuntimeError: Always.
        """
        (dest / "MONKEY.000").write_bytes(b"da")
        raise RuntimeError("no space left on device")

    monkeypatch.setattr(scummvm.extraction_cache, "_extract_archive", dies)
    emu = Scummvm()

    with pytest.raises(RuntimeError, match="no space"):
        emu.launch(emu.resolve_rom_file(archive), None)

    assert [p.name for p in scummvm.CACHE_DIR.iterdir() if p.name != ".scratch"] == []
    assert not any((scummvm.CACHE_DIR / ".scratch").iterdir())
    assert spawned.cmd is None


def test_saves_imported_for_an_archived_game_land_under_its_target(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight registers the extraction, not the zip, so the saves find their target.

    Scanning the archive file itself detects nothing, which would refuse every
    save the session carries in.
    """
    archive = zipped_game(dirs["roms"], {"Monkey/MONKEY.000": b"data"})
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)

    result = _preflight(Scummvm(), archive, {".import/save/monkey.003": b"one"})

    assert result.refusals == ()
    assert _dests(result) == {".import/save/monkey.003": "saves/monkey.003"}
    assert add.paths[0].is_relative_to(scummvm.CACHE_DIR)


def test_the_launch_after_an_archived_preflight_reuses_its_extraction(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight already extracted and registered the game, so the launch does neither again."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    extractions = _count_extractions(monkeypatch)
    emu = Scummvm()
    _preflight(emu, archive, {".import/save/monkey.003": b"one"})

    emu.launch(emu.resolve_rom_file(archive), None)

    assert len(extractions) == 1
    assert len(add.paths) == 1
    assert spawned.cmd[-1] == "monkey"


def test_the_identity_of_an_extracted_archive_is_its_target(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once extracted and registered, an archived game reads back its target without scanning."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    monkeypatch.setattr(scummvm.subprocess, "run", AddsWhatItScans())
    emu = Scummvm()
    rom = emu.resolve_rom_file(archive)
    emu.launch(rom, None)
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)

    assert imports.resolve_activate_identity(Scummvm(), rom, None).value == "monkey"


def test_the_identity_of_an_archive_not_yet_extracted_is_none(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity read never extracts: that is the launch's work, off this lookup."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)

    identity = imports.resolve_activate_identity(Scummvm(), archive.resolve(), None)

    assert identity == imports.SessionIdentity(None, "none")
    assert not scummvm.CACHE_DIR.exists()


def test_another_archive_of_the_same_game_never_takes_its_target(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two archived copies of one game never end up sharing one target and one set of saves.

    Only a re-upload of the same archive supersedes its old extraction. A
    second copy is refused the way a second library folder would be.
    """
    floppy = zipped_game(dirs["roms"], {"MONKEY.000": b"floppy"}, "monkey-floppy.zip")
    cd = zipped_game(dirs["roms"], {"MONKEY.000": b"cd"}, "monkey-cd.zip")
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    emu = Scummvm()
    emu.launch(emu.resolve_rom_file(floppy), None)
    first = add.paths[0]

    with pytest.raises(RuntimeError):
        emu.launch(emu.resolve_rom_file(cd), None)

    assert scummvm.target_for_path(first) == "monkey"


def test_a_preflight_whose_archive_will_not_extract_says_so(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names the extraction, not a detection miss the player cannot act on."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)

    def fails(rom: Path, dest: Path, timeout: float) -> None:
        """Fail the way a full disk would.

        Args:
            rom: The archive.
            dest: Where it goes.
            timeout: The extractor timeout.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("No space left on device")

    monkeypatch.setattr(scummvm.extraction_cache, "_extract_archive", fails)

    result = _preflight(Scummvm(), archive, {".import/save/monkey.003": b"one"})

    assert [r.reason for r in result.refusals] == ["identity_unknown"]
    assert "could not be extracted" in (result.refusals[0].detail or "")
    assert "No space left on device" in (result.refusals[0].detail or "")


def test_a_preflight_whose_cache_dir_cannot_be_made_refuses_rather_than_fails(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError out of the cache is a refusal naming it, not an unhandled preflight error."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)
    blocker = dirs["roms"].parent / "not-a-dir"
    blocker.write_bytes(b"x")
    monkeypatch.setattr(scummvm, "CACHE_DIR", blocker / "extracted")

    result = _preflight(Scummvm(), archive, {".import/save/monkey.003": b"one"})

    assert [r.reason for r in result.refusals] == ["identity_unknown"]
    assert "could not be extracted" in (result.refusals[0].detail or "")


@pytest.mark.parametrize("replacement", ["renamed", "extracted"])
def test_an_extraction_whose_archive_is_gone_gives_way(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """An archive renamed, or swapped for its loose folder, still boots.

    Its old extraction waits on disk for eviction and no launch can reach it
    any more, yet its domain would block the new copy's scan until then.
    """
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    emu = Scummvm()
    emu.launch(emu.resolve_rom_file(archive), None)
    first = add.paths[0]

    if replacement == "renamed":
        rom = archive.rename(dirs["roms"] / "Monkey Island.zip")
    else:
        archive.unlink()
        rom = dirs["roms"] / "Monkey"
        rom.mkdir()
        (rom / "MONKEY.000").write_bytes(b"data")
    emu.launch(emu.resolve_rom_file(rom), None)

    assert spawned.cmd[-1] == "monkey"
    assert scummvm.target_for_path(first) is None
    assert first.is_dir()


def test_an_extraction_whose_archive_is_still_there_keeps_its_target(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a vanished archive frees its extraction's target; a live one is another copy."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    add = AddsWhatItScans()
    monkeypatch.setattr(scummvm.subprocess, "run", add)
    emu = Scummvm()
    emu.launch(emu.resolve_rom_file(archive), None)
    first = add.paths[0]
    loose = dirs["roms"] / "Monkey"
    loose.mkdir()
    (loose / "MONKEY.000").write_bytes(b"data")

    with pytest.raises(RuntimeError):
        emu.launch(emu.resolve_rom_file(loose), None)

    assert scummvm.target_for_path(first) == "monkey"


def test_an_extracted_archive_is_not_listed_again(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its extraction already showed game files, so a later activate skips the listing."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    monkeypatch.setattr(scummvm.subprocess, "run", AddsWhatItScans())
    emu = Scummvm()
    emu.launch(emu.resolve_rom_file(archive), None)

    def never_lists(archive: Path, timeout: float) -> list[str]:
        """Fail the test if the archive is listed.

        Args:
            archive: The archive.
            timeout: The lister timeout.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(f"listed {archive.name} again")

    monkeypatch.setattr(scummvm.extraction_cache, "list_members", never_lists)

    assert Scummvm().resolve_rom_file(archive) == archive.resolve()


def test_an_archive_deleted_before_its_launch_fails_on_the_archive(
    dirs: dict[str, Path], spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error names the archive, rather than `--add` scanning a path that is no folder."""
    archive = zipped_game(dirs["roms"], {"MONKEY.000": b"data"})
    monkeypatch.setattr(scummvm.subprocess, "run", _never_runs)
    emu = Scummvm()
    rom = emu.resolve_rom_file(archive)
    archive.unlink()

    with pytest.raises(RuntimeError, match="monkey.zip"):
        emu.launch(rom, None)

    assert spawned.cmd is None


def test_a_startup_sweep_it_cannot_read_never_stops_the_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable cache dir skips the sweep instead of failing startup."""

    def unreadable() -> None:
        """Fail the way an unreadable scratch dir would.

        Raises:
            PermissionError: Always.
        """
        raise PermissionError("permission denied")

    monkeypatch.setattr(scummvm._CACHE, "_clear_scratch", unreadable)

    scummvm.sweep_stale_extractions()
