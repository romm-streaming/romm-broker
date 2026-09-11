"""RetroArch's platform table: the map that decides which core a claim loads.

Also covers the core asset links, the per-launch config overlay, the resume
gate, and playlist-driven disc swapping.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional, Union

import pytest

from webstation_broker.emulators import retroarch


def test_the_table_is_the_one_on_disk() -> None:
    """PLATFORMS holds exactly the platforms listed in the JSON file on disk.

    The map used to be duplicated inline, and the copy silently shadowed the
    file, so every platform added to the file did nothing.
    """
    on_disk = json.loads(retroarch._PLATFORMS_FILE.read_text())

    assert set(retroarch.PLATFORMS) == set(on_disk)


@pytest.mark.parametrize("slug", ["psp", "nes", "gba", "n64", "snes", "genesis", "dc"])
def test_the_common_platforms_are_mapped(slug: str) -> None:
    """Each everyday platform maps to a core with a non-empty extension list."""
    info = retroarch._platform_info(slug)

    assert info is not None
    assert info["core"]
    assert info["extensions"]


def test_psp_boots_on_the_ppsspp_core() -> None:
    """The psp platform maps to the ppsspp core and accepts .iso and .cso."""
    info = retroarch._platform_info("psp")

    assert info["core"] == "ppsspp"
    assert ".iso" in info["extensions"] and ".cso" in info["extensions"]


def test_a_platform_slug_is_matched_case_insensitively() -> None:
    """A slug in any case resolves to the same platform info."""
    assert retroarch._platform_info("PSP") == retroarch._platform_info("psp")


def test_an_unmapped_platform_has_no_core() -> None:
    """An unmapped slug, or no slug at all, yields no platform info."""
    assert retroarch._platform_info("ps2") is None
    assert retroarch._platform_info(None) is None


@pytest.mark.parametrize("slug", ["ngc", "wii"])
def test_the_dolphin_core_keeps_state_thumbnails_off(slug: str) -> None:
    """The Dolphin core's platforms turn state thumbnails off.

    It renders on the GPU, and the framebuffer grab after a save deadlocks
    RetroArch's runloop, taking the command channel down with it.
    """
    assert retroarch._platform_info(slug)["thumbnail"] is False


def test_psp_declares_where_the_core_finds_its_assets() -> None:
    """The psp platform points the PPSSPP asset link straight at the assets tree.

    The ppsspp core will not boot without PPSSPP's own asset tree, which the
    buildbot .so does not carry. It reads the files straight out of PPSSPP/, so
    linking the tree one level deeper hides them from it.
    """
    assets = retroarch._platform_info("psp")["assets"]

    assert assets["PPSSPP"].endswith("/assets")


class TestCoreAssets:
    """Linking a core's asset tree into RetroArch's system directory."""

    def test_a_declared_source_is_linked_into_the_system_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A declared asset source is symlinked at its path under the system dir."""
        source = tmp_path / "share" / "ppsspp" / "assets"
        source.mkdir(parents=True)
        (source / "ppge_atlas.zim").write_bytes(b"atlas")
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({"PPSSPP/assets": str(source)})

        linked = system / "PPSSPP" / "assets"
        assert linked.is_symlink()
        assert (linked / "ppge_atlas.zim").read_bytes() == b"atlas"

    def test_linking_twice_is_a_no_op(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Linking the same source twice leaves one link pointing at it."""
        source = tmp_path / "assets"
        source.mkdir()
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)
        assets = {"PPSSPP/assets": str(source)}

        retroarch._ensure_core_assets(assets)
        retroarch._ensure_core_assets(assets)

        assert (system / "PPSSPP" / "assets").readlink() == source

    def test_a_stale_link_is_repointed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A link left over from another source is repointed at the new one."""
        old = tmp_path / "old"
        old.mkdir()
        new = tmp_path / "new"
        new.mkdir()
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({"PPSSPP/assets": str(old)})
        retroarch._ensure_core_assets({"PPSSPP/assets": str(new)})

        assert (system / "PPSSPP" / "assets").readlink() == new

    def test_a_real_directory_already_there_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real directory already at the link path is not replaced.

        A user who installed the assets by hand keeps them.
        """
        source = tmp_path / "assets"
        source.mkdir()
        system = tmp_path / "system"
        theirs = system / "PPSSPP" / "assets"
        theirs.mkdir(parents=True)
        (theirs / "theirs.zim").write_bytes(b"mine")
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({"PPSSPP/assets": str(source)})

        assert not theirs.is_symlink()
        assert (theirs / "theirs.zim").exists()

    def test_a_missing_source_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A source that does not exist is skipped without raising.

        The core's own complaint about the missing asset is the better error.
        """
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({"PPSSPP/assets": str(tmp_path / "nope")})

        assert not (system / "PPSSPP" / "assets").exists()

    def test_a_platform_with_no_assets_touches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty asset map does not even create the system directory."""
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({})

        assert not system.exists()


class TestBrokerConfig:
    """The per-launch overlay written on top of the user's own retroarch.cfg."""

    @pytest.fixture(autouse=True)
    def _dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point the RetroArch data, state, and save directories and the overlay at tmp_path.

        Args:
            tmp_path: The per-test temporary directory.
            monkeypatch: The pytest monkeypatch fixture.
        """
        monkeypatch.setattr(retroarch, "RA_DATA_DIR", tmp_path)
        monkeypatch.setattr(retroarch, "STATE_DIR", tmp_path / "states")
        monkeypatch.setattr(retroarch, "SAVE_DIR", tmp_path / "saves")
        monkeypatch.setattr(retroarch, "BROKER_CFG", tmp_path / "broker.cfg")

    def test_the_joypad_driver_is_pinned_off_udev(self) -> None:
        """The overlay pins the joypad driver to linuxraw by default.

        The Selkies pads all look like one device to udev, so it registers
        none of them; linuxraw opens the js nodes the interposer hooks.
        """
        cfg = retroarch._write_broker_cfg().read_text()

        assert 'input_joypad_driver = "linuxraw"' in cfg

    def test_an_empty_driver_leaves_the_user_config_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty JOYPAD_DRIVER writes no joypad driver key into the overlay."""
        monkeypatch.setattr(retroarch, "JOYPAD_DRIVER", "")

        assert "input_joypad_driver" not in retroarch._write_broker_cfg().read_text()

    def test_the_driver_is_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A configured JOYPAD_DRIVER is written into the overlay verbatim."""
        monkeypatch.setattr(retroarch, "JOYPAD_DRIVER", "sdl2")

        assert 'input_joypad_driver = "sdl2"' in retroarch._write_broker_cfg().read_text()

    def test_the_stdin_channel_and_save_dirs_are_still_there(self) -> None:
        """The overlay enables the stdin command channel and names the state and save dirs.

        The overlay is what makes the session controllable at all.
        """
        cfg = retroarch._write_broker_cfg().read_text()

        assert 'stdin_cmd_enable = "true"' in cfg
        assert f'savestate_directory = "{retroarch.STATE_DIR}"' in cfg
        assert f'savefile_directory = "{retroarch.SAVE_DIR}"' in cfg

    @pytest.mark.parametrize("thumbnail,expected", [(True, "true"), (False, "false")])
    def test_thumbnails_follow_the_platform(self, thumbnail: bool, expected: str) -> None:
        """The thumbnail flag passed in is what the overlay writes."""
        cfg = retroarch._write_broker_cfg(thumbnail).read_text()

        assert f'savestate_thumbnail_enable = "{expected}"' in cfg


def test_extensions_and_save_subtrees_survive_the_load_as_tuples() -> None:
    """Every platform's extensions and save_subtrees load as tuples.

    The launcher treats extensions as an ordered preference list and the
    save logic iterates the subtrees, so neither may come back as a raw list.
    """
    for slug, info in retroarch.PLATFORMS.items():
        assert isinstance(info["extensions"], tuple), slug
        if "save_subtrees" in info:
            assert isinstance(info["save_subtrees"], tuple), slug


class TestResumeGate:
    """The gate deciding whether a launch schedules a deferred state load.

    Slot 0 is this broker's only working slot, so a launch asking to resume
    it must still start the deferred load.
    """

    @pytest.fixture(autouse=True)
    def _stub_launch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
        """Stub everything launch() touches and capture the threads it starts.

        Args:
            tmp_path: The per-test temporary directory.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            The args tuple of every thread launch() started, in order.
        """
        monkeypatch.setattr(retroarch, "_ensure_core", lambda name, source=None: tmp_path / f"{name}.so")
        monkeypatch.setattr(retroarch, "_ensure_core_assets", lambda assets: None)
        monkeypatch.setattr(retroarch, "_write_broker_cfg", lambda *a: tmp_path / "broker.cfg")
        monkeypatch.setattr(
            retroarch.shutil, "which", lambda binary, path=None: "/usr/bin/retroarch"
        )
        monkeypatch.setattr(retroarch.Retroarch, "stop", lambda self: None)
        monkeypatch.setattr(retroarch.Retroarch, "_spawn_ra", lambda self, cmd, env: None)

        started = []

        class FakeThread:
            """A threading.Thread stand-in that records its args instead of running.

            Attributes:
                args: The positional arguments the thread was built with.
            """

            def __init__(
                self,
                target: Optional[Callable[..., object]] = None,
                args: tuple[object, ...] = (),
                daemon: bool = False,
            ) -> None:
                """Remember the args; the target is never run.

                Args:
                    target: The callable the real thread would run.
                    args: Positional arguments for the target.
                    daemon: Whether the real thread would be a daemon.
                """
                self.args = args

            def start(self) -> None:
                """Record the args in place of starting a thread."""
                started.append(self.args)

        monkeypatch.setattr(retroarch.threading, "Thread", FakeThread)
        return started

    def _launch(self, tmp_path: Path, resume_slot: Optional[int]) -> retroarch.Retroarch:
        """Launch a snes session against a throwaway ROM path.

        Args:
            tmp_path: Directory the ROM path is made under.
            resume_slot: The slot to ask launch() to resume, if any.

        Returns:
            The launched Retroarch.
        """
        emu = retroarch.Retroarch()
        emu.platform = "snes"
        emu.launch(tmp_path / "game.sfc", resume_slot)
        return emu

    def test_slot_zero_still_defers_a_load(self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]]) -> None:
        """A resume request for slot 0 schedules a deferred load of slot 0."""
        self._launch(tmp_path, 0)

        assert [args[0] for args in _stub_launch] == [0]

    def test_a_nonzero_slot_defers_a_load(self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]]) -> None:
        """A resume request for a nonzero slot schedules a deferred load of that slot."""
        self._launch(tmp_path, 3)

        assert [args[0] for args in _stub_launch] == [3]

    def test_no_resume_request_defers_nothing(
        self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]]
    ) -> None:
        """A launch with no resume slot starts no deferred load."""
        self._launch(tmp_path, None)

        assert _stub_launch == []

    def test_a_core_without_states_defers_nothing(
        self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]]
    ) -> None:
        """A resume request for a core that cannot load states starts no deferred load."""
        emu = retroarch.Retroarch()
        emu.platform = "jaguar"
        emu.launch(tmp_path / "game.j64", 0)

        assert _stub_launch == []

    def test_a_core_without_states_logs_the_dropped_resume(
        self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dropping a resume request for a stateless core is logged, not silent."""
        emu = retroarch.Retroarch()
        emu.platform = "jaguar"

        with caplog.at_level(logging.WARNING):
            emu.launch(tmp_path / "game.j64", 0)

        assert "ignoring resume_slot" in caplog.text

    def test_launching_a_playlist_records_it_and_starts_on_disc_zero(self, tmp_path: Path) -> None:
        """Launching a .m3u records the playlist and resets the disc index to zero."""
        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("Game (Disc 1).chd\n")

        emulator.launch(playlist, None)

        assert emulator._playlist == playlist
        assert emulator._disc_index == 0

    def test_launching_a_bare_disc_records_no_playlist(self, tmp_path: Path) -> None:
        """Launching a single disc image records no playlist."""
        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        disc = tmp_path / "Game.chd"
        disc.write_bytes(b"x")

        emulator.launch(disc, None)

        assert emulator._playlist is None

    def test_a_platform_without_an_override_uses_the_default_settle(
        self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]]
    ) -> None:
        """A platform with no resume_settle entry keeps the module default."""
        emu = self._launch(tmp_path, 0)

        assert emu._resume_settle == retroarch.RESUME_LOAD_SETTLE

    def test_a_platform_override_replaces_the_default_settle(
        self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]]
    ) -> None:
        """PPSSPP's slower HLE boot needs longer than the default settle before a load.

        A load issued before the core finishes registering its HLE event
        table corrupts the resume instead of restoring it, so psp asks for
        a longer wait via its platform table entry.
        """
        emu = retroarch.Retroarch()
        emu.platform = "psp"
        emu.launch(tmp_path / "game.iso", 0)

        assert emu._resume_settle == retroarch.PLATFORMS["psp"]["resume_settle"]
        assert emu._resume_settle != retroarch.RESUME_LOAD_SETTLE

    def test_a_platform_without_an_override_uses_the_default_confirm_wait(
        self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]]
    ) -> None:
        """A platform with no state_confirm_wait entry keeps the module default."""
        emu = self._launch(tmp_path, 0)

        assert emu._state_confirm_wait == retroarch.STATE_CONFIRM_WAIT

    def test_a_platform_override_replaces_the_default_confirm_wait(
        self, tmp_path: Path, _stub_launch: list[tuple[Any, ...]]
    ) -> None:
        """PPSSPP's multi-megabyte states need longer than the default confirm wait.

        A save that has not finished landing on disk by the default window
        reads as a failed save, so psp asks for a longer wait via its
        platform table entry.
        """
        emu = retroarch.Retroarch()
        emu.platform = "psp"
        emu.launch(tmp_path / "game.iso", 0)

        assert emu._state_confirm_wait == retroarch.PLATFORMS["psp"]["state_confirm_wait"]
        assert emu._state_confirm_wait != retroarch.STATE_CONFIRM_WAIT


class TestPlaylistPreference:
    """A folder holding a playlist and its discs boots the playlist.

    The disc-swap commands step through playlist entries, so the playlist has
    to be the thing that was loaded.
    """

    @pytest.mark.parametrize(
        "platform", ["dc", "saturn", "segacd", "turbografx-cd", "dos"]
    )
    def test_m3u_is_the_first_choice_on_every_disc_platform(self, platform: str) -> None:
        """Every disc-based platform lists .m3u as its first extension."""
        info = retroarch._platform_info(platform)
        assert info is not None
        assert info["extensions"][0] == ".m3u"

    def test_a_folder_with_a_playlist_and_discs_picks_the_playlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A folder holding a .m3u beside its discs resolves to the .m3u."""
        monkeypatch.setattr(retroarch, "ROM_ROOT", tmp_path)
        game = tmp_path / "Game"
        game.mkdir()
        (game / "Game.m3u").write_text("Game (Disc 1).chd\nGame (Disc 2).chd\n")
        (game / "Game (Disc 1).chd").write_bytes(b"1")
        (game / "Game (Disc 2).chd").write_bytes(b"2")

        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        assert emulator.resolve_rom_file(game) == (game / "Game.m3u").resolve()

    def test_a_direct_path_that_is_a_symlink_out_of_the_rom_root_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A direct path that is a symlink out of the ROM root is rejected."""
        monkeypatch.setattr(retroarch, "ROM_ROOT", tmp_path / "romm")
        (tmp_path / "romm").mkdir()
        outside = tmp_path / "elsewhere.chd"
        outside.write_bytes(b"x")
        linked = tmp_path / "romm" / "Game.chd"
        linked.symlink_to(outside)

        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        assert emulator.resolve_rom_file(linked) is None


class TestPlaylistHelpers:
    """Reading a .m3u the way RetroArch does.

    One relative path per line, comments and blanks skipped, order is the
    disc order.
    """

    def test_entries_resolve_against_the_playlist_directory(self, tmp_path: Path) -> None:
        """Relative playlist entries resolve against the playlist's own directory."""
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("Game (Disc 1).chd\nGame (Disc 2).chd\n")
        assert retroarch._m3u_entries(playlist) == [
            (tmp_path / "Game (Disc 1).chd").resolve(),
            (tmp_path / "Game (Disc 2).chd").resolve(),
        ]

    def test_comments_and_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        """Comment lines and blank lines contribute no entries."""
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("# a comment\n\nGame (Disc 1).chd\n\n")
        assert retroarch._m3u_entries(playlist) == [
            (tmp_path / "Game (Disc 1).chd").resolve()
        ]

    def test_an_unreadable_playlist_yields_no_entries(self, tmp_path: Path) -> None:
        """A playlist that cannot be read yields an empty entry list."""
        assert retroarch._m3u_entries(tmp_path / "missing.m3u") == []

    def test_an_unreadable_playlist_logs_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A playlist that cannot be read is named in a logged warning."""
        missing = tmp_path / "missing.m3u"
        with caplog.at_level(logging.WARNING):
            retroarch._m3u_entries(missing)
        assert str(missing) in caplog.text

    def test_index_finds_the_matching_entry(self, tmp_path: Path) -> None:
        """The index lookup returns the position of the disc in the playlist."""
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("Game (Disc 1).chd\nGame (Disc 2).chd\n")
        target = tmp_path / "Game (Disc 2).chd"
        assert retroarch._m3u_index_for_path(playlist, target) == 1

    def test_index_is_none_for_a_disc_the_playlist_does_not_list(self, tmp_path: Path) -> None:
        """The index lookup is None for a disc the playlist does not list."""
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("Game (Disc 1).chd\n")
        assert retroarch._m3u_index_for_path(playlist, tmp_path / "Other.chd") is None


class TestSwapDisc:
    """Driving the tray over the command protocol."""

    @pytest.fixture
    def emulator(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> retroarch.Retroarch:
        """Build a Retroarch that looks alive and records commands instead of writing them.

        Args:
            tmp_path: Directory holding the three-disc playlist.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A Retroarch on disc 0 of a three-disc playlist, with the commands it
            sent collected in its `sent` list and the playlist directory in
            `tmp_path`.
        """
        monkeypatch.setattr(retroarch, "DISC_TRAY_SETTLE", 0)
        monkeypatch.setattr(retroarch, "DISC_STEP_DELAY", 0)
        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        emulator.sent = []

        def fake_send(
            cmd: str, wait_prefix: Optional[Union[str, tuple[str, ...]]] = None, timeout: float = 5.0
        ) -> Optional[str]:
            emulator.sent.append(cmd)
            if cmd == "GET_STATUS":
                return "GET_STATUS PLAYING dc,Game,0"
            return None

        def fake_write_cmd(cmd: str) -> bool:
            emulator.sent.append(cmd)
            return True

        monkeypatch.setattr(emulator, "_send", fake_send)
        monkeypatch.setattr(emulator, "_write_cmd", fake_write_cmd)
        monkeypatch.setattr(emulator, "alive", lambda: True)

        playlist = tmp_path / "Game.m3u"
        playlist.write_text(
            "Game (Disc 1).chd\nGame (Disc 2).chd\nGame (Disc 3).chd\n"
        )
        emulator._playlist = playlist
        emulator._disc_index = 0
        emulator.tmp_path = tmp_path
        return emulator

    def test_swapping_forward_ejects_steps_and_closes(self, emulator: retroarch.Retroarch) -> None:
        """A swap to the next disc ejects, steps once, closes, and commits the index."""
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is True
        assert emulator.sent == [
            "GET_STATUS",
            "DISK_EJECT_TOGGLE",
            "DISK_NEXT",
            "DISK_EJECT_TOGGLE",
        ]
        assert emulator._disc_index == 1

    def test_swapping_backward_wraps_around_the_playlist(self, emulator: retroarch.Retroarch) -> None:
        """A swap to an earlier disc steps forward around the end of the playlist."""
        emulator._disc_index = 2
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is True
        # From index 2 to index 1 is two forward steps through a 3-disc list.
        assert emulator.sent.count("DISK_NEXT") == 2
        assert emulator._disc_index == 1

    def test_swapping_to_the_mounted_disc_leaves_the_tray_alone(self, emulator: retroarch.Retroarch) -> None:
        """A swap to the disc already mounted sends no tray commands."""
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 1).chd") is True
        assert "DISK_EJECT_TOGGLE" not in emulator.sent

    def test_a_disc_outside_the_playlist_is_refused(self, emulator: retroarch.Retroarch) -> None:
        """A disc the playlist does not list is refused before the tray is touched."""
        assert emulator.swap_disc(emulator.tmp_path / "Other.chd") is False
        assert "DISK_EJECT_TOGGLE" not in emulator.sent

    def test_a_session_with_no_playlist_cannot_swap(self, emulator: retroarch.Retroarch) -> None:
        """A session launched without a playlist refuses every swap."""
        emulator._playlist = None
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False

    def test_a_core_that_never_reports_playing_is_refused(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swap is refused when the core never answers GET_STATUS with PLAYING."""
        monkeypatch.setattr(retroarch, "DISC_SWAP_WAIT", 0)
        monkeypatch.setattr(emulator, "_send", lambda *a, **k: None)
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False

    def test_a_core_that_dies_mid_swap_does_not_commit_the_index(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A core dying partway through the tray sequence leaves the disc index unchanged.

        The tray commands after PLAYING have no reply to confirm delivery,
        so a death partway through must not leave the tracked index pointing
        at a disc that was never actually mounted.
        """
        state = {"alive": True}
        monkeypatch.setattr(emulator, "alive", lambda: state["alive"])

        def fake_write_cmd(cmd: str) -> bool:
            emulator.sent.append(cmd)
            if cmd == "DISK_NEXT":
                state["alive"] = False
            return True

        monkeypatch.setattr(emulator, "_write_cmd", fake_write_cmd)
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        assert emulator._disc_index == 0

    def test_a_relaunch_during_the_wait_does_not_clobber_the_new_sessions_index(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swap that a relaunch overtakes during the PLAYING wait is abandoned.

        A swap outliving the session it was issued for must not stomp the
        disc index a fresh launch() already reset, the same hazard
        _deferred_load_state guards against with _launch_seq.
        """

        def fake_send(
            cmd: str, wait_prefix: Optional[Union[str, tuple[str, ...]]] = None, timeout: float = 5.0
        ) -> Optional[str]:
            emulator.sent.append(cmd)
            if cmd == "GET_STATUS":
                # A relaunch races the wait and wins: a new session starts and
                # resets tracking exactly as Retroarch.launch() does.
                emulator._launch_seq += 1
                emulator._disc_index = 0
                return "GET_STATUS PLAYING dc,Game,0"
            return None

        monkeypatch.setattr(emulator, "_send", fake_send)
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        assert emulator._disc_index == 0

    def test_the_class_advertises_disc_swap(self) -> None:
        """Retroarch declares support for disc swapping."""
        assert retroarch.Retroarch.supports_disc_swap is True

    def test_no_playlist_and_a_dead_core_are_reported_differently(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing playlist and a dead core produce different warnings.

        A dead core is a different failure than an unloaded playlist, and
        reporting "no playlist" for a dead core is actively misleading.
        """
        playlist = emulator._playlist

        with caplog.at_level(logging.WARNING):
            emulator._playlist = None
            assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        no_playlist_message = caplog.text
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            emulator._playlist = playlist
            monkeypatch.setattr(emulator, "alive", lambda: False)
            assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        dead_core_message = caplog.text

        assert no_playlist_message != dead_core_message
        assert "playlist" in no_playlist_message.lower()
        assert "playlist" not in dead_core_message.lower()

    def test_a_relaunch_during_the_tray_settle_aborts_before_the_next_command(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relaunch landing after the eject stops the sequence before its next command.

        A relaunch landing right after the eject must not let the sequence
        keep sending commands (DISK_NEXT, the closing eject) to the new
        process's stdin, which reads self._proc live.
        """

        def fake_write_cmd(cmd: str) -> bool:
            emulator.sent.append(cmd)
            if cmd == "DISK_EJECT_TOGGLE":
                emulator._launch_seq += 1
            return True

        monkeypatch.setattr(emulator, "_write_cmd", fake_write_cmd)
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        assert emulator.sent == ["GET_STATUS", "DISK_EJECT_TOGGLE"]
        assert emulator._disc_index == 0

    def test_a_second_concurrent_swap_is_refused(self, emulator: retroarch.Retroarch) -> None:
        """A swap issued while the tray lock is held fails fast without sending anything.

        A swap already holding the tray lock must make a second swap fail
        fast rather than queue up behind the multi-second sequence.
        """
        emulator._disc_lock.acquire()
        try:
            assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        finally:
            emulator._disc_lock.release()
        assert emulator.sent == []

    def test_concurrent_swaps_do_not_interleave_the_tray_sequence(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two racing swaps leave exactly the winner's tray sequence, uninterrupted.

        The loser of the race must fail before touching the tray, so the
        winner's EJECT / NEXT / EJECT sequence is never split up by another
        thread's commands landing in the middle of it.
        """
        entered_sequence = threading.Event()
        release_winner = threading.Event()

        def fake_write_cmd(cmd: str) -> bool:
            emulator.sent.append(cmd)
            if cmd == "DISK_EJECT_TOGGLE" and emulator.sent.count("DISK_EJECT_TOGGLE") == 1:
                # Mid-sequence: let the loser attempt its own swap here.
                entered_sequence.set()
                release_winner.wait(timeout=2)
            return True

        monkeypatch.setattr(emulator, "_write_cmd", fake_write_cmd)

        results = {}

        def winner() -> None:
            results["winner"] = emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd")

        t = threading.Thread(target=winner)
        t.start()
        assert entered_sequence.wait(timeout=2)

        results["loser"] = emulator.swap_disc(emulator.tmp_path / "Game (Disc 3).chd")
        release_winner.set()
        t.join(timeout=2)

        assert results["winner"] is True
        assert results["loser"] is False
        # The loser's refusal injected nothing: the recorded sequence is
        # exactly the winner's, uninterrupted.
        assert emulator.sent == [
            "GET_STATUS",
            "DISK_EJECT_TOGGLE",
            "DISK_NEXT",
            "DISK_EJECT_TOGGLE",
        ]
        assert emulator._disc_index == 1

    def test_a_swap_is_refused_while_a_deferred_resume_holds_the_lock(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swap is refused while a deferred resume load is holding the tray lock.

        The tray lock also excludes _deferred_load_state: a LOAD_STATE
        landing inside a swap's tray-settle window is the collision it
        guards against, and the same holds in reverse.
        """
        emulator._resume_settle = 0
        entered_lock = threading.Event()
        release_resume = threading.Event()

        def fake_wait_for_state(deadline: float) -> bool:
            entered_lock.set()
            release_resume.wait(timeout=2)
            return True

        monkeypatch.setattr(emulator, "wait_for_state", fake_wait_for_state)
        monkeypatch.setattr(emulator, "_load_state_locked", lambda slot: True)

        t = threading.Thread(
            target=emulator._deferred_load_state, args=(0, emulator._launch_seq)
        )
        t.start()
        assert entered_lock.wait(timeout=2)

        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False

        release_resume.set()
        t.join(timeout=2)


class TestStateSupport:
    """Whether the loaded platform's core claims savestates, and which core it is."""

    def test_a_mapped_platform_supports_states(self) -> None:
        """A platform whose entry says nothing about states supports them."""
        emu = retroarch.Retroarch()
        emu.platform = "snes"

        assert emu.supports_states is True

    def test_a_platform_that_opts_out_reports_no_state_support(self) -> None:
        """A platform whose core stubs out serialization reports no state support."""
        emu = retroarch.Retroarch()
        emu.platform = "jaguar"

        assert emu.supports_states is False

    def test_an_unmapped_platform_still_claims_states(self) -> None:
        """With no platform mapped there is nothing to opt out, so states stay claimed."""
        assert retroarch.Retroarch().supports_states is True

    def test_a_core_that_cannot_save_is_not_asked_to_on_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A core that cannot save states is not asked to on the way out."""
        emu = retroarch.Retroarch()
        emu.platform = "jaguar"
        monkeypatch.setattr(emu, "alive", lambda: True)
        monkeypatch.setattr(emu, "_send", lambda *a, **kw: None)
        monkeypatch.setattr(emu, "_quit", lambda: None)

        def refuse(lock_wait: float) -> bool:
            raise AssertionError("the exit save should not run for a core without states")

        monkeypatch.setattr(emu, "_save_into_slot", refuse)

        assert emu.save_and_exit(0) == {
            "state_saved": False,
            "state_slot": 0,
            "state_file": None,
            "sram_flushed": False,
        }

    def test_a_core_that_cannot_save_logs_the_skipped_state(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Skipping the exit-time state save for a stateless core is logged, not silent."""
        emu = retroarch.Retroarch()
        emu.platform = "jaguar"
        monkeypatch.setattr(emu, "alive", lambda: True)
        monkeypatch.setattr(emu, "_send", lambda *a, **kw: None)
        monkeypatch.setattr(emu, "_quit", lambda: None)

        with caplog.at_level(logging.WARNING):
            emu.save_and_exit(0)

        assert "not saving state on exit" in caplog.text

    def test_a_confirmed_save_with_no_findable_state_file_is_not_reported_saved(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`state_path()` returning None after a confirmed save downgrades `saved`, logged.

        `_save_into_slot` returning True means RetroArch confirmed the write,
        but if the state can't be found afterward there is no file for RomM's
        exit dump to ship; reporting `state_saved: True` here would tell RomM
        a state exists when it has nothing to serve.
        """
        emu = retroarch.Retroarch()
        emu.platform = "snes"
        monkeypatch.setattr(emu, "alive", lambda: True)
        monkeypatch.setattr(emu, "_send", lambda *a, **kw: None)
        monkeypatch.setattr(emu, "_quit", lambda: None)
        monkeypatch.setattr(emu, "_save_into_slot", lambda lock_wait: True)
        monkeypatch.setattr(emu, "state_path", lambda: None)

        with caplog.at_level(logging.WARNING):
            result = emu.save_and_exit(0)

        assert result == {
            "state_saved": False,
            "state_slot": 0,
            "state_file": None,
            "sram_flushed": False,
        }
        assert "no state file could be found" in caplog.text

    def test_the_running_core_is_named_for_the_archive(self) -> None:
        """The archive manifest names the core actually running the game."""
        emu = retroarch.Retroarch()
        emu.platform = "psp"

        assert emu.archive_core() == "ppsspp"

    def test_an_unmapped_platform_names_no_core(self) -> None:
        """With no platform mapped there is no core to name."""
        assert retroarch.Retroarch().archive_core() is None


class TestClearWorkingSlot:
    """Emptying the save tree so one player's saves and states never reach the next."""

    @pytest.fixture
    def emulator(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> retroarch.Retroarch:
        """A Retroarch whose save root is a throwaway tree with both subtrees laid down.

        Args:
            tmp_path: Backs the save root.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A Retroarch on an unscoped platform, with `save_root` pointing at
            `tmp_path` and empty `states` and `saves` directories under it.
        """
        (tmp_path / "states").mkdir()
        (tmp_path / "saves").mkdir()
        monkeypatch.setattr(retroarch, "STATE_SLOT", 0)
        emulator = retroarch.Retroarch()
        emulator.platform = "snes"
        monkeypatch.setattr(emulator, "save_root", tmp_path)
        return emulator

    def test_the_previous_players_save_file_does_not_survive_the_activate(
        self, emulator: retroarch.Retroarch, tmp_path: Path
    ) -> None:
        """The leak this hook exists to close: a leftover .srm from the last session.

        Its fresh mtime is what makes the restore skip the arriving player's
        own save as older, and what puts it in that player's exit dump.
        """
        leftover = tmp_path / "saves" / "Game.srm"
        leftover.write_bytes(b"player-a save data")

        emulator.clear_working_slot()

        assert not leftover.exists()

    def test_a_save_a_core_keeps_in_its_own_subdir_goes_too(
        self, emulator: retroarch.Retroarch, tmp_path: Path
    ) -> None:
        """Cores that build a directory tree under the savefile dir lose all of it."""
        nested = tmp_path / "saves" / "dolphin-emu" / "User" / "GC"
        nested.mkdir(parents=True)
        (nested / "MemoryCardA.raw").write_bytes(b"card")

        emulator.clear_working_slot()

        assert not (tmp_path / "saves" / "dolphin-emu").exists()

    def test_a_state_in_the_slot_goes_with_its_thumbnail(
        self, emulator: retroarch.Retroarch, tmp_path: Path
    ) -> None:
        """A state in the broker's slot is dropped along with its thumbnail."""
        (tmp_path / "states" / "Game.state").write_bytes(b"s")
        (tmp_path / "states" / "Game.state.png").write_bytes(b"p")

        emulator.clear_working_slot()

        assert not (tmp_path / "states" / "Game.state").exists()
        assert not (tmp_path / "states" / "Game.state.png").exists()

    def test_a_state_a_core_redirected_into_its_own_dir_goes_too(
        self, emulator: retroarch.Retroarch, tmp_path: Path
    ) -> None:
        """A state a core redirected into its own subdir is cleared as well."""
        nested = tmp_path / "states" / "dolphin-emu"
        nested.mkdir()
        (nested / "Game.state").write_bytes(b"s")

        emulator.clear_working_slot()

        assert not nested.exists()

    def test_states_outside_the_brokers_slot_go_too(
        self, emulator: retroarch.Retroarch, tmp_path: Path
    ) -> None:
        """A state the player parked on another slot is still the last player's."""
        (tmp_path / "states" / "Game.state3").write_bytes(b"s")
        (tmp_path / "states" / "Game.state.auto").write_bytes(b"a")

        emulator.clear_working_slot()

        assert not (tmp_path / "states" / "Game.state3").exists()
        assert not (tmp_path / "states" / "Game.state.auto").exists()

    def test_the_save_directories_themselves_survive(
        self, emulator: retroarch.Retroarch, tmp_path: Path
    ) -> None:
        """The dirs stay: the broker config names them, and a launch missing one writes elsewhere."""
        (tmp_path / "saves" / "Game.srm").write_bytes(b"v")

        emulator.clear_working_slot()

        assert (tmp_path / "saves").is_dir()
        assert (tmp_path / "states").is_dir()

    def test_nothing_outside_the_save_subtrees_is_touched(
        self, emulator: retroarch.Retroarch, tmp_path: Path
    ) -> None:
        """The clear is scoped to the subtrees the restore writes into, and no wider."""
        cfg = tmp_path / "broker.cfg"
        cfg.write_text("stdin_cmd_enable = \"true\"\n")
        (tmp_path / "cores").mkdir()
        (tmp_path / "cores" / "snes9x_libretro.so").write_bytes(b"core")

        emulator.clear_working_slot()

        assert cfg.exists()
        assert (tmp_path / "cores" / "snes9x_libretro.so").exists()

    def test_a_scoped_platform_keeps_the_rest_of_the_core_data_dir(
        self, emulator: retroarch.Retroarch, tmp_path: Path
    ) -> None:
        """Platforms whose savefile dir is also their app-data dir clear only the save subtrees."""
        emulator.platform = "ngc"
        user = tmp_path / "saves" / "dolphin-emu" / "User"
        (user / "GC").mkdir(parents=True)
        (user / "Config").mkdir(parents=True)
        (user / "GC" / "MemoryCardA.raw").write_bytes(b"card")
        (user / "Config" / "Dolphin.ini").write_text("[General]\n")

        emulator.clear_working_slot()

        assert not (user / "GC" / "MemoryCardA.raw").exists()
        assert (user / "Config" / "Dolphin.ini").exists()

    def test_a_missing_save_dir_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing before RetroArch has ever run is a no-op, not a failure."""
        emulator = retroarch.Retroarch()
        monkeypatch.setattr(emulator, "save_root", tmp_path / "absent")

        emulator.clear_working_slot()

    def test_a_subtree_that_escapes_the_save_root_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A subtree pointing out of the save root deletes nothing, and says so."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.srm").write_bytes(b"v")
        emulator = retroarch.Retroarch()
        monkeypatch.setattr(emulator, "save_root", root)
        monkeypatch.setattr(
            retroarch.Retroarch, "save_subtrees", property(lambda self: ("../outside",))
        )

        with caplog.at_level(logging.ERROR):
            emulator.clear_working_slot()

        assert (outside / "keep.srm").exists()
        assert "escapes the save root" in caplog.text

    def test_a_file_that_cannot_be_removed_is_logged(
        self, emulator: retroarch.Retroarch, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A save that cannot be removed is logged rather than failing the activate."""
        (tmp_path / "saves" / "Game.srm").write_bytes(b"v")

        def refuse(self: Path, missing_ok: bool = False) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(Path, "unlink", refuse)

        with caplog.at_level(logging.WARNING):
            emulator.clear_working_slot()

        assert "could not clear stale save data" in caplog.text

    def test_the_clear_is_declared_to_the_registry(self) -> None:
        """The base class's flag has to say the save tree is cleared, or the gap goes unnoticed."""
        assert retroarch.Retroarch.clears_stale_saves is True


class TestWaitForStateFile:
    """Confirming a save-state write actually produced bytes, not just a file."""

    @pytest.fixture
    def state_dir(self, tmp_path: Path) -> Path:
        """A throwaway savestate directory."""
        states = tmp_path / "states"
        states.mkdir()
        return states

    def test_a_state_stuck_at_zero_bytes_is_never_confirmed(self, state_dir: Path) -> None:
        """A .state file that stays empty is a write that produced nothing, not a save."""
        before = retroarch._state_snapshot(state_dir, "Game")
        (state_dir / "Game.state").write_bytes(b"")

        settled = retroarch._wait_for_state_file(before, state_dir, "Game", 0, 0.9)

        assert settled is False

    def test_a_state_that_becomes_non_empty_and_holds_is_confirmed(self, state_dir: Path) -> None:
        """A .state file that lands with real bytes and stops changing is reported as saved."""
        before = retroarch._state_snapshot(state_dir, "Game")
        (state_dir / "Game.state").write_bytes(b"savedata")

        settled = retroarch._wait_for_state_file(before, state_dir, "Game", 0, 5.0)

        assert settled is True


def _write_after(path: Path, data: bytes, delay: float) -> None:
    """Write `data` to `path` after `delay` seconds, from a background thread.

    Args:
        path: The file to write.
        data: The bytes to write.
        delay: Seconds to sleep before writing, simulating an emulator that
            produces the file some time after the save was triggered.
    """
    time.sleep(delay)
    path.write_bytes(data)


class TestSaveStateThumbnail:
    """Waiting for the paired save thumbnail alongside the state file."""

    @pytest.fixture
    def emulator(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> retroarch.Retroarch:
        """A Retroarch that looks alive, skips slot homing, and drops commands silently.

        Args:
            tmp_path: Backs the savestate directory.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A Retroarch ready to have `save_state` called on it, with
            `STATE_DIR` pointed at a throwaway directory and enough time on
            `STATE_CONFIRM_WAIT` and `STATE_THUMBNAIL_WAIT` for both waits to
            settle.
        """
        states = tmp_path / "states"
        states.mkdir()
        monkeypatch.setattr(retroarch, "STATE_DIR", states)
        monkeypatch.setattr(retroarch, "STATE_CONFIRM_WAIT", 2.5)
        monkeypatch.setattr(retroarch, "STATE_THUMBNAIL_WAIT", 1.5)
        emulator = retroarch.Retroarch()
        emulator.platform = "gc"
        emulator._rom_base = "Game"
        emulator._slot_homed = True
        emulator._thumbnail_enabled = True
        monkeypatch.setattr(emulator, "alive", lambda: True)
        monkeypatch.setattr(emulator, "_write_cmd", lambda cmd: True)
        return emulator

    def test_a_thumbnail_written_after_the_state_is_still_waited_for(
        self, emulator: retroarch.Retroarch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A thumbnail that lands after the state file is still confirmed, not skipped."""
        threading.Thread(
            target=_write_after, args=(retroarch.STATE_DIR / "Game.state", b"savedata", 0.05), daemon=True
        ).start()
        threading.Thread(
            target=_write_after, args=(retroarch.STATE_DIR / "Game.state.png", b"thumb", 0.7), daemon=True
        ).start()

        with caplog.at_level(logging.WARNING):
            assert emulator.save_state(0) is True

        assert "save thumbnail" not in caplog.text
        assert (retroarch.STATE_DIR / "Game.state.png").exists()

    def test_a_slow_state_confirmation_does_not_starve_the_thumbnail_wait(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The thumbnail gets its own `STATE_THUMBNAIL_WAIT`, not whatever the state wait left behind.

        The state file lands late enough that only a sliver of
        `_state_confirm_wait` remains once it is confirmed — too little for
        the thumbnail's own 0.5s stability requirement. If the thumbnail
        wait were still carved out of that same, nearly-spent deadline (the
        pre-fix behaviour), a thumbnail landing shortly after would be
        reported missing even though it arrived in plenty of time.
        """
        emulator._state_confirm_wait = 1.0
        monkeypatch.setattr(retroarch, "STATE_THUMBNAIL_WAIT", 1.0)
        threading.Thread(
            target=_write_after, args=(retroarch.STATE_DIR / "Game.state", b"savedata", 0.2), daemon=True
        ).start()
        threading.Thread(
            target=_write_after, args=(retroarch.STATE_DIR / "Game.state.png", b"thumb", 0.9), daemon=True
        ).start()

        with caplog.at_level(logging.WARNING):
            assert emulator.save_state(0) is True

        assert "save thumbnail" not in caplog.text
        assert (retroarch.STATE_DIR / "Game.state.png").exists()

    def test_a_missing_thumbnail_does_not_fail_the_save(
        self, emulator: retroarch.Retroarch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No .png ever lands; the state save itself still reports success, with a warning logged."""
        emulator._state_confirm_wait = 1.0
        threading.Thread(
            target=_write_after, args=(retroarch.STATE_DIR / "Game.state", b"savedata", 0.05), daemon=True
        ).start()

        with caplog.at_level(logging.WARNING):
            assert emulator.save_state(0) is True

        assert "save thumbnail" in caplog.text
        assert "Game.state.png" in caplog.text


class TestLoadStateConfirmation:
    """Telling a load that restored the game from one RetroArch only echoed back."""

    @pytest.fixture
    def emulator(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> retroarch.Retroarch:
        """A live Retroarch with a state file sitting in the broker's slot.

        Args:
            tmp_path: Backs the savestate directory.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A Retroarch whose `state_path` resolves, with the confirmation
            wait short enough to fail a test quickly.
        """
        states = tmp_path / "states"
        states.mkdir()
        (states / "Game.state").write_bytes(b"savedata")
        monkeypatch.setattr(retroarch, "STATE_DIR", states)
        monkeypatch.setattr(retroarch, "STATE_SLOT", 0)
        monkeypatch.setattr(retroarch, "LOAD_CONFIRM_WAIT", 0.5)
        emulator = retroarch.Retroarch()
        emulator.platform = "snes"
        emulator._rom_base = "Game"
        monkeypatch.setattr(emulator, "alive", lambda: True)
        return emulator

    def test_a_load_that_reads_the_state_succeeds(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The state's access time moving past the marker is what confirms the restore."""
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)

        def echo_and_read(cmd: str, wait_prefix: Any, timeout: float) -> str:
            retroarch.STATE_DIR.joinpath("Game.state").read_bytes()
            return cmd

        monkeypatch.setattr(emulator, "_send", echo_and_read)

        assert emulator.load_state(0) is True

    def test_an_echo_with_no_read_behind_it_fails(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A refused load echoes exactly like one that worked; only the missing read separates them."""
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)
        monkeypatch.setattr(emulator, "_send", lambda cmd, wait_prefix, timeout: cmd)

        with caplog.at_level(logging.ERROR):
            assert emulator.load_state(0) is False

        assert "never read Game.state" in caplog.text

    def test_no_echo_at_all_fails(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A command that draws no reply is still a failed load."""
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)
        monkeypatch.setattr(emulator, "_send", lambda cmd, wait_prefix, timeout: None)

        assert emulator.load_state(0) is False

    def test_an_untracked_mount_falls_back_to_the_echo_with_a_warning(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """On noatime nothing records the read, so the echo is all there is and it says so."""
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: False)
        monkeypatch.setattr(emulator, "_send", lambda cmd, wait_prefix, timeout: cmd)

        with caplog.at_level(logging.WARNING):
            assert emulator.load_state(0) is True

        assert "access times are not tracked" in caplog.text

    def test_an_empty_slot_is_never_sent_at_all(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no state on disk there is nothing to load and no command goes out."""
        (retroarch.STATE_DIR / "Game.state").unlink()
        sent: list[str] = []
        monkeypatch.setattr(emulator, "_send", lambda cmd, wait_prefix, timeout: sent.append(cmd))

        assert emulator.load_state(0) is False
        assert sent == []

    def test_a_fingerprint_failure_right_after_backdating_aborts_rather_than_skips_the_guard(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Losing the stat right after backdating is itself the in-flight change the guard exists for.

        Treating a failed fingerprint as "no identity to check" would run the
        load with the one guard disarmed that catches a state replaced while
        the load is in flight.
        """
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)
        monkeypatch.setattr(retroarch, "_state_identity", lambda path: None)
        sent: list[str] = []
        monkeypatch.setattr(emulator, "_send", lambda cmd, wait_prefix, timeout: sent.append(cmd))

        with caplog.at_level(logging.ERROR):
            assert emulator.load_state(0) is False

        assert sent == []
        assert "cannot be confirmed safely" in caplog.text

    def test_the_state_is_backdated_before_the_command_goes_out(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A state already read this session must not answer the next load off its old atime."""
        state = retroarch.STATE_DIR / "Game.state"
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)
        seen: list[float] = []

        def record(cmd: str, wait_prefix: Any, timeout: float) -> str:
            seen.append(state.stat().st_atime)
            return cmd

        monkeypatch.setattr(emulator, "_send", record)
        emulator.load_state(0)

        assert seen and seen[0] < state.stat().st_mtime

    def test_a_probe_that_could_not_run_does_not_wave_the_load_through(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed probe is not a noatime mount, so the echo alone still confirms nothing."""
        def raise_probe(dir_path: Path) -> bool:
            raise retroarch.AtimeProbeError("probe blew up")

        monkeypatch.setattr(retroarch, "_atime_tracked", raise_probe)
        monkeypatch.setattr(emulator, "_send", lambda cmd, wait_prefix, timeout: cmd)

        with caplog.at_level(logging.WARNING):
            loaded = emulator.load_state(0)

        assert loaded is False
        assert "access-time probe for" in caplog.text
        assert "access times are not tracked" not in caplog.text

    def test_a_state_pushed_in_mid_load_is_not_taken_as_the_confirmation(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A state replaced under the load has a fresh atime that says nothing about that load."""
        state = retroarch.STATE_DIR / "Game.state"
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)

        def replace_and_read(cmd: str, wait_prefix: Any, timeout: float) -> str:
            state.unlink()
            state.write_bytes(b"pushed")
            now = time.time()
            os.utime(state, (now, now + 10))
            state.read_bytes()
            return cmd

        monkeypatch.setattr(emulator, "_send", replace_and_read)

        with caplog.at_level(logging.WARNING):
            loaded = emulator.load_state(0)

        assert loaded is False
        assert "was replaced while the load was in flight" in caplog.text

    def test_a_state_file_handout_during_the_wait_is_not_taken_as_the_confirmation(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """RomM's own GET of the state file must not be mistaken for RetroArch's load read."""
        state = retroarch.STATE_DIR / "Game.state"
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)

        def echo_then_handout_read(cmd: str, wait_prefix: Any, timeout: float) -> str:
            emulator.note_state_handout()
            state.read_bytes()
            return cmd

        monkeypatch.setattr(emulator, "_send", echo_then_handout_read)

        with caplog.at_level(logging.WARNING):
            loaded = emulator.load_state(0)

        assert loaded is False
        assert "state-file handout was in flight" in caplog.text

    def test_a_tainted_load_is_retried_with_a_fresh_marker_and_confirms(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A load whose only tainted read raced a handout is retried, not reported as failed."""
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)
        sent: list[str] = []

        def send(cmd: str, wait_prefix: Any, timeout: float) -> str:
            sent.append(cmd)
            return cmd

        monkeypatch.setattr(emulator, "_send", send)

        results = iter([None, True])
        monkeypatch.setattr(retroarch, "_wait_for_state_read", lambda *a, **k: next(results))

        with caplog.at_level(logging.WARNING):
            assert emulator.load_state(0) is True

        assert sent == ["LOAD_STATE_SLOT 0", "LOAD_STATE_SLOT 0"]
        assert "retrying with a fresh marker" in caplog.text

    def test_a_load_tainted_past_the_retry_limit_is_reported_failed(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A load that never clears the taint on any attempt still ends in a reported failure."""
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)
        monkeypatch.setattr(retroarch, "LOAD_TAINT_RETRY_LIMIT", 1)
        sent: list[str] = []

        def send(cmd: str, wait_prefix: Any, timeout: float) -> str:
            sent.append(cmd)
            return cmd

        monkeypatch.setattr(emulator, "_send", send)
        monkeypatch.setattr(retroarch, "_wait_for_state_read", lambda *a, **k: None)

        with caplog.at_level(logging.ERROR):
            assert emulator.load_state(0) is False

        assert sent == ["LOAD_STATE_SLOT 0", "LOAD_STATE_SLOT 0"]
        assert "could not be confirmed after" in caplog.text


class TestAtimeHelpers:
    """The access-time probe and wait the load confirmation is built on."""

    def test_a_read_is_detected_where_atimes_are_tracked(self, tmp_path: Path) -> None:
        """The probe reports what the mount this test runs on actually does."""
        probed = retroarch._atime_tracked(tmp_path)

        assert isinstance(probed, bool)

    def test_the_probe_leaves_nothing_behind(self, tmp_path: Path) -> None:
        """The scratch file the probe writes is always removed."""
        retroarch._atime_tracked(tmp_path)

        assert list(tmp_path.iterdir()) == []

    def test_an_unwritable_dir_raises_instead_of_answering_untracked(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A probe that could not run has proved nothing, and must not pass as noatime.

        "This mount keeps no access times" is the one verdict that lets a load
        through on its echo alone, so a probe that never ran cannot borrow it.
        """
        with caplog.at_level(logging.WARNING):
            with pytest.raises(retroarch.AtimeProbeError):
                retroarch._atime_tracked(tmp_path / "absent")

        assert "could not probe access-time tracking" in caplog.text

    def test_a_replaced_state_is_not_confirmed_by_its_own_fresh_atime(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A state pushed in under the load is a new file, and its read confirms nothing."""
        state = tmp_path / "Game.state"
        state.write_bytes(b"original")
        identity = retroarch._state_identity(state)
        state.unlink()
        state.write_bytes(b"pushed")
        # An inode the kernel handed straight back would leave the mtime as the
        # only thing separating the two files, so make it separate them.
        now = time.time()
        os.utime(state, (now, now + 10))

        with caplog.at_level(logging.WARNING):
            found = retroarch._wait_for_state_read(
                state, 0.0, time.monotonic() + 0.5, identity=identity
            )

        assert found is False
        assert "was replaced while the load was in flight" in caplog.text

    def test_a_tainted_read_is_not_accepted_while_the_taint_window_is_open(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An access-time move seen during a handout's grace window does not confirm the load."""
        state = tmp_path / "Game.state"
        state.write_bytes(b"data")
        now = time.time()
        os.utime(state, (now + 10, now))

        with caplog.at_level(logging.WARNING):
            found = retroarch._wait_for_state_read(
                state, 0.0, time.monotonic() + 0.3, tainted_until=lambda: float("inf")
            )

        assert found is None
        assert "state-file handout was in flight" in caplog.text

    def test_a_tainted_read_never_confirms_even_after_the_window_closes(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A read tainted during the handout window must not confirm once the window closes.

        Under `relatime` a genuine later read does not move the access time
        again once a handout's read already pushed it past the mtime, so
        there is no second move left to accept as evidence: the whole attempt
        has to fail and let the retry run with a fresh marker instead.
        """
        state = tmp_path / "Game.state"
        state.write_bytes(b"data")
        now = time.time()
        os.utime(state, (now + 10, now))
        taint_until = time.monotonic() + 0.15

        with caplog.at_level(logging.ERROR):
            found = retroarch._wait_for_state_read(
                state, 0.0, time.monotonic() + 0.5, tainted_until=lambda: taint_until
            )

        assert found is None
        assert "needs a fresh attempt to be confirmed" in caplog.text

    def test_backdating_a_missing_file_reports_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A state that is gone cannot be marked, and the caller has to hear about it."""
        with caplog.at_level(logging.WARNING):
            assert retroarch._backdate_atime(tmp_path / "gone.state") is None

        assert "could not backdate the access time" in caplog.text

    def test_a_state_that_disappears_mid_wait_stops_the_wait(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A state removed while the load is in flight ends the wait instead of timing it out."""
        with caplog.at_level(logging.WARNING):
            found = retroarch._wait_for_state_read(
                tmp_path / "gone.state", 0.0, time.monotonic() + 5.0
            )

        assert found is False
        assert "went away while waiting" in caplog.text


class TestCoreDownload:
    """Installing a libretro core without letting a bad binary reach a session."""

    @pytest.fixture
    def cores_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point CORES_DIR at a throwaway directory.

        Args:
            tmp_path: The per-test temporary directory.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            The directory CORES_DIR now names.
        """
        cores = tmp_path / "cores"
        cores.mkdir()
        monkeypatch.setattr(retroarch, "CORES_DIR", cores)
        return cores

    @staticmethod
    def _core_zip(payload: bytes = b"\x7fELF core") -> bytes:
        """A zip holding one `_libretro.so` member.

        Args:
            payload: The bytes to store as the core.

        Returns:
            The zip's bytes.
        """
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("snes9x_libretro.so", payload)
        return buf.getvalue()

    def _serve(
        self, monkeypatch: pytest.MonkeyPatch, body: bytes, headers: Optional[dict[str, str]] = None
    ) -> None:
        """Answer every core download with `body`.

        Args:
            monkeypatch: The pytest monkeypatch fixture.
            body: The response body to serve.
            headers: Response headers, defaulting to none.
        """
        class FakeResponse:
            def __init__(self) -> None:
                self.content = body
                self.headers = headers or {}

            def raise_for_status(self) -> None:
                return None

        monkeypatch.setattr(retroarch.httpx, "get", lambda *a, **kw: FakeResponse())

    def test_a_good_download_lands_executable(
        self, cores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core is installed under its real name with the exec bit set."""
        self._serve(monkeypatch, self._core_zip())

        so = retroarch._ensure_core("snes9x")

        assert so == cores_dir / "snes9x_libretro.so"
        assert so.read_bytes() == b"\x7fELF core"
        assert so.stat().st_mode & 0o111

    def test_a_truncated_download_never_becomes_a_core(
        self, cores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body short of its declared Content-Length is refused before anything is written."""
        body = self._core_zip()
        self._serve(monkeypatch, body[:-20], {"content-length": str(len(body))})

        with pytest.raises(RuntimeError, match="truncated"):
            retroarch._ensure_core("snes9x")

        assert not (cores_dir / "snes9x_libretro.so").exists()

    def test_a_corrupted_zip_never_becomes_a_core(
        self, cores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A member whose CRC does not match its bytes is refused by the zip read itself."""
        good = bytearray(self._core_zip(b"\x7fELF core payload here"))
        good[40:50] = b"\x00" * 10
        self._serve(monkeypatch, bytes(good))

        with pytest.raises(RuntimeError, match="failed to download"):
            retroarch._ensure_core("snes9x")

        assert not (cores_dir / "snes9x_libretro.so").exists()

    def test_an_empty_core_is_refused(
        self, cores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero-byte member would dlopen into an error nowhere near the download."""
        self._serve(monkeypatch, self._core_zip(b""))

        with pytest.raises(RuntimeError, match="empty"):
            retroarch._ensure_core("snes9x")

        assert not (cores_dir / "snes9x_libretro.so").exists()

    def test_a_zip_with_no_core_in_it_is_refused(
        self, cores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zip carrying something other than a core is an error, not an install."""
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.md", "not a core")
        self._serve(monkeypatch, buf.getvalue())

        with pytest.raises(RuntimeError):
            retroarch._ensure_core("snes9x")

    def test_the_temp_name_is_unique_per_download(
        self, cores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two downloads of one core must not share a temp file to half-write over each other."""
        self._serve(monkeypatch, self._core_zip())
        seen: list[str] = []
        real_write = Path.write_bytes

        def record(self: Path, data: bytes) -> int:
            if self.name.endswith(".tmp"):
                seen.append(self.name)
            return real_write(self, data)

        monkeypatch.setattr(Path, "write_bytes", record)

        retroarch._ensure_core("snes9x")
        (cores_dir / "snes9x_libretro.so").unlink()
        retroarch._ensure_core("snes9x")

        assert len(seen) == 2
        assert seen[0] != seen[1]

    def test_a_failed_install_leaves_no_temp_file_behind(
        self, cores_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rename that fails cleans up its temp file rather than leaving a stray core."""
        self._serve(monkeypatch, self._core_zip())

        def refuse(src: Any, dst: Any) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(retroarch.os, "replace", refuse)

        with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="failed to install"):
            retroarch._ensure_core("snes9x")

        assert list(cores_dir.iterdir()) == []

    def test_an_installed_core_is_not_downloaded_again(
        self, cores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A core already on disk short-circuits before any HTTP call."""
        so = cores_dir / "snes9x_libretro.so"
        so.write_bytes(b"already here")

        def explode(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("no download should be attempted")

        monkeypatch.setattr(retroarch.httpx, "get", explode)

        assert retroarch._ensure_core("snes9x") == so


class TestSaveStateAgainstResume:
    """A save landing while a deferred resume load is still in flight."""

    @pytest.fixture
    def emulator(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> retroarch.Retroarch:
        """A live Retroarch whose save and load are reduced to recorded events.

        Args:
            tmp_path: Backs a state file `state_path` resolves to, so the
                resume retry loop's between-attempt fingerprint check has
                something real to stat.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A Retroarch with its slot already homed, `_try_save` and
            `_load_state_locked` recording into its `events` list, and no
            settle to wait out.
        """
        emulator = retroarch.Retroarch()
        emulator.platform = "psp"
        emulator._rom_base = "Game"
        emulator._slot_homed = True
        emulator._resume_settle = 0
        emulator.events = []
        state = tmp_path / "Game.state"
        state.write_bytes(b"savedata")
        monkeypatch.setattr(emulator, "alive", lambda: True)
        monkeypatch.setattr(emulator, "state_path", lambda: state)
        monkeypatch.setattr(emulator, "_try_save", lambda: emulator.events.append("save") or True)
        monkeypatch.setattr(
            emulator, "_load_state_locked", lambda slot: emulator.events.append("load") or True
        )
        return emulator

    def test_a_save_cannot_land_inside_the_resume_load_window(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A save racing the deferred resume waits for the load instead of overwriting the slot.

        A SAVE_STATE sent inside the resume's settle window writes the
        not-yet-restored boot state into the slot, and the resume load that
        follows then reads that back over the player's own save.
        """
        entered_lock = threading.Event()
        release_resume = threading.Event()

        def fake_wait_for_state(deadline: float) -> bool:
            entered_lock.set()
            release_resume.wait(timeout=2)
            return True

        monkeypatch.setattr(emulator, "wait_for_state", fake_wait_for_state)
        monkeypatch.setattr(
            emulator, "_send", lambda cmd, wait_prefix, timeout: "GET_STATUS PLAYING psp,Game,0"
        )

        resume = threading.Thread(
            target=emulator._deferred_load_state, args=(0, emulator._launch_seq)
        )
        resume.start()
        assert entered_lock.wait(timeout=2)

        saved: list[bool] = []
        saver = threading.Thread(target=lambda: saved.append(emulator.save_state(0)))
        saver.start()
        time.sleep(0.2)

        assert emulator.events == []

        release_resume.set()
        resume.join(timeout=2)
        saver.join(timeout=2)

        assert emulator.events == ["load", "save"]
        assert saved == [True]

    def test_a_save_that_never_gets_the_tray_fails_loudly(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A save gives up rather than blocking forever, and says why."""
        monkeypatch.setattr(retroarch, "SAVE_LOCK_WAIT", 0.05)
        emulator._disc_lock.acquire()
        try:
            with caplog.at_level(logging.ERROR):
                assert emulator.save_state(0) is False
        finally:
            emulator._disc_lock.release()

        assert emulator.events == []
        assert "waiting on an in-flight resume load or disc swap" in caplog.text

    def test_the_exit_save_gives_up_on_the_tray_far_sooner_than_a_normal_save(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The exit runs under the API's session lock, so it waits its own short budget.

        Waiting out `SAVE_LOCK_WAIT` there would answer every other session
        route 409 for half a minute.
        """
        monkeypatch.setattr(retroarch, "SAVE_LOCK_WAIT", 30.0)
        monkeypatch.setattr(retroarch, "EXIT_SAVE_LOCK_WAIT", 0.1)
        monkeypatch.setattr(emulator, "_flush_sram", lambda: True)
        monkeypatch.setattr(emulator, "_quit", lambda: None)

        emulator._disc_lock.acquire()
        try:
            started = time.monotonic()
            with caplog.at_level(logging.ERROR):
                result = emulator.save_and_exit(0)
            waited = time.monotonic() - started
        finally:
            emulator._disc_lock.release()

        assert result["state_saved"] is False
        assert waited < 1.0
        assert emulator.events == []
        assert "gave up after 0.1s" in caplog.text

    def test_a_save_with_the_tray_free_still_goes_straight_through(
        self, emulator: retroarch.Retroarch
    ) -> None:
        """The tray lock is released again, so back-to-back saves both run."""
        assert emulator.save_state(0) is True
        assert emulator.save_state(0) is True
        assert emulator.events == ["save", "save"]


class TestResumeLoadRetry:
    """The deferred resume load's bounded retry and its failure escalation."""

    @pytest.fixture
    def emulator(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> retroarch.Retroarch:
        """A live Retroarch that retries without waiting between attempts.

        Args:
            tmp_path: Backs a state file `state_path` resolves to, so the
                retry loop's between-attempt fingerprint check has something
                real to stat.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A Retroarch with `RESUME_LOAD_RETRY_GAP` collapsed to nothing and
            the slot already holding a state file.
        """
        monkeypatch.setattr(retroarch, "RESUME_LOAD_RETRY_GAP", 0)
        emulator = retroarch.Retroarch()
        emulator.platform = "psp"
        emulator._rom_base = "Game"
        emulator._resume_settle = 0
        state = tmp_path / "Game.state"
        state.write_bytes(b"savedata")
        monkeypatch.setattr(emulator, "alive", lambda: True)
        monkeypatch.setattr(emulator, "wait_for_state", lambda deadline: True)
        monkeypatch.setattr(emulator, "state_path", lambda: state)
        return emulator

    def test_a_load_that_misses_once_is_tried_again(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A core that was not ready for the first load gets another attempt."""
        results = [False, True]
        monkeypatch.setattr(emulator, "_load_state_locked", lambda slot: results.pop(0))

        with caplog.at_level(logging.INFO):
            confirmed = emulator._load_until_confirmed(0, time.monotonic() + 5.0, emulator._launch_seq)

        assert confirmed is True
        assert results == []
        assert "delivered on attempt 2" in caplog.text

    def test_a_load_that_never_takes_is_escalated_when_the_budget_runs_out(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unrestored game is an error, not one info line saying "failed"."""
        attempts: list[int] = []
        monkeypatch.setattr(
            emulator, "_load_state_locked", lambda slot: attempts.append(slot) or False
        )

        with caplog.at_level(logging.WARNING):
            confirmed = emulator._load_until_confirmed(0, time.monotonic() + 0.3, emulator._launch_seq)

        assert confirmed is False
        assert len(attempts) > 1
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors and "running unrestored" in errors[0].getMessage()

    def test_a_relaunch_stops_the_retries(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Retries for a session that has been relaunched must not keep firing at the new one."""
        attempts: list[int] = []

        def fail_and_relaunch(slot: int) -> bool:
            attempts.append(slot)
            emulator._launch_seq += 1
            return False

        monkeypatch.setattr(emulator, "_load_state_locked", fail_and_relaunch)

        with caplog.at_level(logging.WARNING):
            confirmed = emulator._load_until_confirmed(0, time.monotonic() + 5.0, emulator._launch_seq)

        assert confirmed is False
        assert attempts == [0]
        assert "session ended before slot 0 could be loaded" in caplog.text

    def test_a_slot_still_empty_on_the_first_attempt_is_waited_for_again(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A state RomM pushes late still lands, without one hold spanning the whole budget.

        The wait for the state file is inside the tray hold, so it gets one
        attempt's slice and then drops the tray for the next attempt.
        """
        appears = [False, True]
        monkeypatch.setattr(emulator, "wait_for_state", lambda deadline: appears.pop(0))
        loads: list[int] = []
        monkeypatch.setattr(emulator, "_load_state_locked", lambda slot: loads.append(slot) or True)

        with caplog.at_level(logging.WARNING):
            confirmed = emulator._load_until_confirmed(0, time.monotonic() + 5.0, emulator._launch_seq)

        assert confirmed is True
        assert loads == [0]
        assert "still holds no state file on attempt 1" in caplog.text

    def test_the_state_file_wait_holds_the_tray_against_a_swap(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A save landing during the wait would create the very file the wait is looking for."""
        in_wait = threading.Event()
        release = threading.Event()

        def blocking_wait(deadline: float) -> bool:
            in_wait.set()
            release.wait(timeout=2)
            return True

        monkeypatch.setattr(emulator, "wait_for_state", blocking_wait)
        monkeypatch.setattr(emulator, "_load_state_locked", lambda slot: True)

        t = threading.Thread(
            target=emulator._load_until_confirmed,
            args=(0, time.monotonic() + 5.0, emulator._launch_seq),
        )
        t.start()
        try:
            assert in_wait.wait(timeout=2)
            assert emulator._disc_lock.acquire(blocking=False) is False
        finally:
            release.set()
            t.join(timeout=2)

    def test_the_deferred_load_retries_through_to_a_restore(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deferred resume path itself retries, not just the helper under it."""
        results = [False, False, True]
        monkeypatch.setattr(emulator, "_load_state_locked", lambda slot: results.pop(0))
        monkeypatch.setattr(
            emulator, "_send", lambda cmd, wait_prefix, timeout: "GET_STATUS PLAYING psp,Game,0"
        )

        emulator._deferred_load_state(0, emulator._launch_seq)

        assert results == []

    def test_a_save_between_attempts_aborts_the_resume_instead_of_loading_the_wrong_state(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A save landing in the retry gap must not be loaded back as the resume.

        The tray is free between attempts, so nothing stops a save from
        overwriting the slot's state file with the running game's own (still
        unrestored) state; loading that back on the next attempt would report
        a successful resume that never actually happened.
        """
        original = emulator.state_path()
        clobbered = tmp_path / "clobbered.state"
        clobbered.write_bytes(b"a save that landed in the retry gap")
        paths = [original, clobbered]
        monkeypatch.setattr(emulator, "state_path", lambda: paths.pop(0))
        attempts: list[int] = []
        monkeypatch.setattr(emulator, "_load_state_locked", lambda slot: attempts.append(slot) or False)

        with caplog.at_level(logging.ERROR):
            confirmed = emulator._load_until_confirmed(0, time.monotonic() + 5.0, emulator._launch_seq)

        assert confirmed is False
        assert attempts == [0]
        assert "changed since attempt" in caplog.text

    def test_a_state_file_that_cannot_be_fingerprinted_is_not_treated_as_ready(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A stat failure right after the wait must not silently skip the fingerprint guard."""
        monkeypatch.setattr(retroarch, "_state_identity", lambda path: None)
        loads: list[int] = []
        monkeypatch.setattr(emulator, "_load_state_locked", lambda slot: loads.append(slot) or True)

        with caplog.at_level(logging.WARNING):
            confirmed = emulator._load_until_confirmed(0, time.monotonic() + 0.3, emulator._launch_seq)

        assert confirmed is False
        assert loads == []
        assert "could not be fingerprinted" in caplog.text


class TestSaveAndExitSramFlush:
    """`save_and_exit` must report whether `_flush_sram` actually landed, not discard it."""

    @pytest.fixture
    def emulator(self, monkeypatch: pytest.MonkeyPatch) -> retroarch.Retroarch:
        """A live Retroarch with `_quit` reduced to a no-op and no state save requested.

        Args:
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A Retroarch ready for `save_and_exit(None)`, which skips the
            state-save path entirely and leaves only the SRAM flush to check.
        """
        emulator = retroarch.Retroarch()
        emulator.platform = "gc"
        emulator._rom_base = "Game"
        monkeypatch.setattr(emulator, "alive", lambda: True)
        monkeypatch.setattr(emulator, "_quit", lambda: None)
        return emulator

    def test_a_refused_sram_flush_is_reported_not_silently_dropped(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_flush_sram`'s False return must survive into the exit report."""
        monkeypatch.setattr(emulator, "_flush_sram", lambda: False)

        result = emulator.save_and_exit(None)

        assert result["sram_flushed"] is False

    def test_a_confirmed_sram_flush_is_reported_true(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful flush is threaded through the same way."""
        monkeypatch.setattr(emulator, "_flush_sram", lambda: True)

        result = emulator.save_and_exit(None)

        assert result["sram_flushed"] is True

    def test_sram_flush_is_not_attempted_when_the_process_is_already_gone(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No process, nothing to flush: report None rather than a stale True or False."""
        monkeypatch.setattr(emulator, "alive", lambda: False)
        flush_calls: list[int] = []
        monkeypatch.setattr(emulator, "_flush_sram", lambda: flush_calls.append(1) or True)

        result = emulator.save_and_exit(None)

        assert result["sram_flushed"] is None
        assert flush_calls == []


class TestLoadStateBackdateFailure:
    """A state that cannot be stamped, on a mount that does track access times."""

    @pytest.fixture
    def emulator(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> retroarch.Retroarch:
        """A live Retroarch with a state file in the broker's slot.

        Args:
            tmp_path: Backs the savestate directory.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A Retroarch whose `state_path` resolves and whose commands are all
            echoed back.
        """
        states = tmp_path / "states"
        states.mkdir()
        (states / "Game.state").write_bytes(b"savedata")
        monkeypatch.setattr(retroarch, "STATE_DIR", states)
        monkeypatch.setattr(retroarch, "STATE_SLOT", 0)
        emulator = retroarch.Retroarch()
        emulator.platform = "snes"
        emulator._rom_base = "Game"
        monkeypatch.setattr(emulator, "alive", lambda: True)
        monkeypatch.setattr(emulator, "_send", lambda cmd, wait_prefix, timeout: cmd)
        return emulator

    def test_a_failed_backdate_is_not_reported_as_an_untracked_mount(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The stamp failing on a tracking mount is a fault on the file, not the mount."""
        monkeypatch.setattr(retroarch, "_atime_tracked", lambda d: True)
        monkeypatch.setattr(retroarch, "_backdate_atime", lambda p: None)

        with caplog.at_level(logging.WARNING):
            loaded = emulator.load_state(0)

        assert loaded is False
        assert "could not backdate" in caplog.text
        assert "access times are not tracked" not in caplog.text
