"""ScummVM target resolution, ini pinning, GMM macros and slot naming.

Covers registering a game folder, the settings pinned into scummvm.ini, the
keystroke sequences the Global Main Menu is driven with, and the save naming
that decides what is a state and what is the game's own save. Nothing here
needs a display, a binary or a real ScummVM.
"""

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from webstation_broker.emulators import scummvm
from webstation_broker.emulators.scummvm import Scummvm


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
    # The macros sleep between steps to let the menu animate; nothing in a test
    # is waiting for an animation.
    monkeypatch.setattr(scummvm, "KEY_DELAY", 0.0)
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


# ── ROM resolution ────────────────────────────────────────────────────────────


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


def test_an_archive_resolves_to_nothing_rather_than_its_folder(
    dirs: dict[str, Path],
) -> None:
    """A zipped game must not be read as "the folder it sits in".

    Libraries are laid out as <root>/<platform>/<game>, so the folder around a
    loose file is the platform folder. Registering that would `--add` every
    game on the platform and boot whichever target sorts first: the player
    asks for one game and gets another, which is worse than a refused launch.
    """
    folder = game_folder(dirs["roms"])
    archive = folder.parent / "woodruff.zip"
    archive.write_bytes(b"PK")

    assert Scummvm().resolve_rom_file(archive) is None


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


# ── Targets in scummvm.ini ────────────────────────────────────────────────────


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
    assert calls[0][1:] == ["--add", f"--path={folder}"]


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


# ── The pinned ini ────────────────────────────────────────────────────────────


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


# ── GMM hotkeys ───────────────────────────────────────────────────────────────


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


# ── Slot naming ───────────────────────────────────────────────────────────────


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


# ── State routes ──────────────────────────────────────────────────────────────


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
    ["", ".", "..", "monkey", "monkey.sav", "../monkey.s01", "sub/monkey.s01", "monkey.s1"],
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


def test_the_thumbnail_travels_inside_the_state(dirs: dict[str, Path]) -> None:
    """ScummVM embeds the frame in the save, so there is no file to point at."""
    assert booted().state_screenshot_path() is None


def test_the_working_slot_is_emptied_before_a_session(dirs: dict[str, Path]) -> None:
    """Every game's working slot goes, since a leftover cannot be told apart.

    The target only exists once a game has booted, which is after this runs.
    """
    stale = (dirs["saves"] / "monkey.s01", dirs["saves"] / "indy3.001")
    kept = (dirs["saves"] / "monkey.s02", dirs["saves"] / "monkey.s00")
    for path in stale + kept:
        path.write_bytes(b"save")

    Scummvm().clear_working_slot()

    assert not any(path.exists() for path in stale)
    assert all(path.exists() for path in kept)


def test_an_empty_save_dir_is_nothing_to_clear(dirs: dict[str, Path], tmp_path: Path) -> None:
    """A container whose save directory does not exist yet clears cleanly."""
    scummvm.SAVE_DIR = tmp_path / "absent"

    Scummvm().clear_working_slot()


# ── Archive classification ────────────────────────────────────────────────────


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


# ── Launching ─────────────────────────────────────────────────────────────────


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


# ── The save and load macros ──────────────────────────────────────────────────


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


# ── Exit ──────────────────────────────────────────────────────────────────────


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


# ── The deferred resume ───────────────────────────────────────────────────────


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

# ── Language ──────────────────────────────────────────────────────────────────


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
