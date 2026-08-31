"""RetroArch's platform table: the map that decides which core a claim loads.

Also covers the core asset links, the per-launch config overlay, the resume
gate, and playlist-driven disc swapping.
"""

import json
import logging
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
        monkeypatch.setattr(emulator, "load_state", lambda slot: True)

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

        def refuse(slot: int) -> bool:
            raise AssertionError("save_state should not run for a core without states")

        monkeypatch.setattr(emu, "save_state", refuse)

        assert emu.save_and_exit(0) == {
            "state_saved": False,
            "state_slot": 0,
            "state_file": None,
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

    def test_the_running_core_is_named_for_the_archive(self) -> None:
        """The archive manifest names the core actually running the game."""
        emu = retroarch.Retroarch()
        emu.platform = "psp"

        assert emu.archive_core() == "ppsspp"

    def test_an_unmapped_platform_names_no_core(self) -> None:
        """With no platform mapped there is no core to name."""
        assert retroarch.Retroarch().archive_core() is None


class TestClearWorkingSlot:
    """Clearing the broker's slot so one player's state never resumes for the next."""

    @pytest.fixture
    def state_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point STATE_DIR at a throwaway directory in the broker's slot 0.

        Args:
            tmp_path: The per-test temporary directory.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            The directory STATE_DIR now names.
        """
        states = tmp_path / "states"
        states.mkdir()
        monkeypatch.setattr(retroarch, "STATE_DIR", states)
        monkeypatch.setattr(retroarch, "STATE_SLOT", 0)
        return states

    def test_a_state_in_the_slot_goes_with_its_thumbnail(self, state_dir: Path) -> None:
        """A state in the broker's slot is dropped along with its thumbnail."""
        (state_dir / "Game.state").write_bytes(b"s")
        (state_dir / "Game.state.png").write_bytes(b"p")

        retroarch.Retroarch().clear_working_slot()

        assert not (state_dir / "Game.state").exists()
        assert not (state_dir / "Game.state.png").exists()

    def test_a_state_a_core_redirected_into_its_own_dir_goes_too(self, state_dir: Path) -> None:
        """A state a core redirected into its own subdir is cleared as well."""
        nested = state_dir / "dolphin-emu"
        nested.mkdir()
        (nested / "Game.state").write_bytes(b"s")

        retroarch.Retroarch().clear_working_slot()

        assert not (nested / "Game.state").exists()

    def test_another_slot_is_left_alone(self, state_dir: Path) -> None:
        """States outside the broker's slot are not the broker's to drop."""
        (state_dir / "Game.state3").write_bytes(b"s")
        (state_dir / "Game.state.auto").write_bytes(b"a")

        retroarch.Retroarch().clear_working_slot()

        assert (state_dir / "Game.state3").exists()
        assert (state_dir / "Game.state.auto").exists()

    def test_save_data_is_left_alone(self, state_dir: Path) -> None:
        """Nothing but a state file is touched."""
        (state_dir / "Game.srm").write_bytes(b"v")

        retroarch.Retroarch().clear_working_slot()

        assert (state_dir / "Game.srm").exists()

    def test_a_missing_state_dir_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing before RetroArch has ever run is a no-op, not a failure."""
        monkeypatch.setattr(retroarch, "STATE_DIR", tmp_path / "absent")

        retroarch.Retroarch().clear_working_slot()

    def test_a_state_that_cannot_be_removed_is_logged(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A state that cannot be removed is logged rather than failing the activate."""
        (state_dir / "Game.state").write_bytes(b"s")

        def refuse(self: Path, missing_ok: bool = False) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(Path, "unlink", refuse)

        with caplog.at_level(logging.WARNING):
            retroarch.Retroarch().clear_working_slot()

        assert "could not clear stale state" in caplog.text

    def test_a_thumbnail_that_cannot_be_removed_is_logged_as_a_thumbnail(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A thumbnail left behind after the state itself was cleared gets its own message.

        A generic OSError on the thumbnail unlink (not FileNotFoundError, which
        missing_ok=True already swallows) must not read as a failure to clear
        the state, which by then already succeeded.
        """
        (state_dir / "Game.state").write_bytes(b"s")
        (state_dir / "Game.state.png").write_bytes(b"p")

        real_unlink = Path.unlink

        def flaky(self: Path, missing_ok: bool = False) -> None:
            if self.name.endswith(".png"):
                raise OSError("read-only")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", flaky)

        with caplog.at_level(logging.WARNING):
            retroarch.Retroarch().clear_working_slot()

        assert not (state_dir / "Game.state").exists()
        assert "could not clear stale state thumbnail" in caplog.text
        assert "could not clear stale state Game.state:" not in caplog.text


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
            `STATE_CONFIRM_WAIT` for both waits to settle.
        """
        states = tmp_path / "states"
        states.mkdir()
        monkeypatch.setattr(retroarch, "STATE_DIR", states)
        monkeypatch.setattr(retroarch, "STATE_CONFIRM_WAIT", 2.5)
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

    def test_a_missing_thumbnail_does_not_fail_the_save(
        self, emulator: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No .png ever lands; the state save itself still reports success, with a warning logged."""
        monkeypatch.setattr(retroarch, "STATE_CONFIRM_WAIT", 1.0)
        threading.Thread(
            target=_write_after, args=(retroarch.STATE_DIR / "Game.state", b"savedata", 0.05), daemon=True
        ).start()

        with caplog.at_level(logging.WARNING):
            assert emulator.save_state(0) is True

        assert "save thumbnail" in caplog.text
        assert "Game.state.png" in caplog.text
