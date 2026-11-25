"""Placing the firmware RomM holds for a platform where the launched emulator looks for it.

Covers the core-info name tables, each emulator's targets, the shared copy
with its overwrite rules, and the activate wiring.
"""

import hashlib
import io
import json
import logging
import os
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from webstation_broker import api, firmware, settings
from webstation_broker.emulators import duckstation, flycast, retroarch, retroarch_cores

from .conftest import PREFIX, FakeEmulator

API = f"{PREFIX}/api"

BOOT = b"dreamcast boot rom"
FLASH = b"dreamcast flash rom"
PSX = b"playstation bios"


def _md5(data: bytes) -> str:
    """The md5 hex digest of some bytes.

    Args:
        data: The bytes.

    Returns:
        Their md5, lower case.
    """
    return hashlib.md5(data).hexdigest()  # noqa: S324


def _sha1(data: bytes) -> str:
    """The sha1 hex digest of some bytes.

    Args:
        data: The bytes.

    Returns:
        Their sha1, lower case.
    """
    return hashlib.sha1(data).hexdigest()  # noqa: S324


def _info(paths: list[str], notes: str = "") -> str:
    """Build a `.info` file listing firmware paths and notes.

    Args:
        paths: The firmware paths, in order.
        notes: The notes line's value.

    Returns:
        The file's text.
    """
    lines = [f'firmware_count = "{len(paths)}"']
    for i, path in enumerate(paths):
        lines.append(f'firmware{i}_desc = "{path}"')
        lines.append(f'firmware{i}_path = "{path}"')
        lines.append(f'firmware{i}_opt = "true"')
    lines.append(f'notes = "{notes}"')
    return "\n".join(lines) + "\n"


def _use_core_info(infos: dict[str, str]) -> None:
    """Swap in a catalog built from the given `.info` files.

    The autouse `clean_catalog` fixture puts the real one back.

    Args:
        infos: Core name to its `.info` text.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for core, text in infos.items():
            zf.writestr(f"{core}_libretro.info", text)
    data = buf.getvalue()
    cores = retroarch_cores.parse_info_zip(data)
    retroarch_cores.set_catalog(retroarch_cores.Catalog(MappingProxyType(cores), data))


@pytest.fixture
def dreamcast_info() -> None:
    """A catalog where flycast keeps its BIOS under `dc/` and another core names the flash."""
    _use_core_info(
        {
            "flycast": _info(["dc/dc_boot.bin", "dc/naomi.zip"], f"(!) dc_boot.bin (md5): {_md5(BOOT)}"),
            "othercore": _info(["flash.bin"], f"(!) dc_flash.bin (md5): {_md5(FLASH)}"),
            "swanstation": _info(["scph5501.bin", "scph5502.bin"], f"(!) scph5501.bin (md5): {_md5(PSX)}"),
        }
    )


@pytest.fixture
def record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the ownership record at tmp_path.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
        tmp_path: The per-test temporary directory.

    Returns:
        The record's path.
    """
    path = tmp_path / "broker-firmware.json"
    monkeypatch.setattr(settings, "FIRMWARE_RECORD", path)
    return path


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A ROM root with a firmware folder in it.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
        tmp_path: The per-test temporary directory.

    Returns:
        The firmware folder under the ROM root.
    """
    roms = tmp_path / "romm"
    bios = roms / "library" / "bios" / "dc"
    bios.mkdir(parents=True)
    monkeypatch.setattr(settings, "ROM_ROOT", roms)
    return bios


def _source(folder: Path, name: str, data: bytes, md5: Optional[str] = "auto") -> firmware.Source:
    """Write a firmware file into the library and describe it as RomM would.

    Args:
        folder: The firmware folder under the ROM root.
        name: The uploaded file name.
        data: The file's bytes.
        md5: The md5 RomM reports; "auto" for the real one, None for none.

    Returns:
        The source.
    """
    path = folder / name
    path.write_bytes(data)
    return firmware.Source(
        name=name,
        path=path,
        md5=_md5(data) if md5 == "auto" else md5,
        sha1=None,
        size=len(data),
    )


def _sweep_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every temp file as stale, since a test cannot back-date a change time.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
    """
    monkeypatch.setattr(firmware, "_stale_after", lambda: -60.0)


class _Dropper(FakeEmulator):
    """An emulator that wants every firmware file under its own name in one directory."""

    name = "dropper"
    drop_dir: Path = Path("/nonexistent")

    def firmware_targets(self, source: firmware.Source) -> list[firmware.Target]:
        """Every file goes to `drop_dir` under its uploaded name.

        Args:
            source: The firmware file.

        Returns:
            One target.
        """
        return [firmware.Target(self.drop_dir, PurePosixPath(source.name))]


@pytest.fixture
def dropper(tmp_path: Path) -> _Dropper:
    """A `_Dropper` whose directory is in tmp_path.

    Args:
        tmp_path: The per-test temporary directory.

    Returns:
        The emulator.
    """
    emu = _Dropper()
    emu.drop_dir = tmp_path / "bios"
    return emu


class TestCoreInfoNames:
    """The names libretro's core info gives a firmware file."""

    def test_md5_notes_from_every_core_name_a_file(self, dreamcast_info: None) -> None:
        """A hash noted by any core maps to that core's name for it."""
        assert retroarch_cores.firmware_names(_md5(FLASH), None) == frozenset({"dc_flash.bin"})
        assert retroarch_cores.firmware_names(_md5(BOOT).upper(), None) == frozenset({"dc_boot.bin"})

    def test_sha1_notes_name_a_file(self) -> None:
        """A core that notes sha1 rather than md5 is read too."""
        _use_core_info(
            {"neocd": _info(["neocd/neocd_f.rom"], f"(!) neocd/neocd_f.rom (sha1): {_sha1(BOOT)}")}
        )

        assert retroarch_cores.firmware_names(None, _sha1(BOOT)) == frozenset({"neocd_f.rom"})

    def test_an_unknown_hash_has_no_names(self, dreamcast_info: None) -> None:
        """A hash no core notes maps to nothing."""
        assert retroarch_cores.firmware_names(_md5(b"nope"), None) == frozenset()
        assert retroarch_cores.firmware_names(None, None) == frozenset()

    def test_a_cores_firmware_paths_come_back_in_order(self, dreamcast_info: None) -> None:
        """A core's firmware paths are listed as its info file gives them."""
        assert retroarch_cores.firmware_paths("flycast") == (
            PurePosixPath("dc/dc_boot.bin"),
            PurePosixPath("dc/naomi.zip"),
        )
        assert retroarch_cores.firmware_paths("nosuchcore") == ()

    def test_unsafe_firmware_paths_are_dropped(self) -> None:
        """A path that is absolute or climbs out of the system dir is never offered."""
        _use_core_info(
            {
                "hostile": _info(
                    ["../escape.bin", "/etc/passwd", "a/../../b.bin", "", "ok/bios.bin", "c:\\\\x.bin"]
                )
            }
        )

        assert retroarch_cores.firmware_paths("hostile") == (PurePosixPath("ok/bios.bin"),)

    def test_the_bundled_core_info_knows_the_common_bios(self) -> None:
        """The shipped core info names the PS1 and Dreamcast BIOS by hash."""
        assert "scph5501.bin" in retroarch_cores.firmware_names("490f666e1afb15b7362b406ed1cea246", None)
        assert "dc_boot.bin" in retroarch_cores.firmware_names("e10c53c2f8b90bab96ead2d368858623", None)
        assert "dc_flash.bin" in retroarch_cores.firmware_names("0a93f7940c455905bea6e392dfde92a4", None)

    def test_known_names_keep_core_infos_spelling(self) -> None:
        """Names come back as core info spells them, mixed case included."""
        _use_core_info({"snes9x": _info(["BS-X.bin"], f"(!) BS-X.bin (md5): {_md5(BOOT)}")})

        assert retroarch_cores.firmware_names(_md5(BOOT), None) == frozenset({"BS-X.bin"})

    def test_the_supplement_covers_bios_core_info_leaves_out(self, tmp_path: Path) -> None:
        """Hashes core info lacks but RomM's known list agrees on are still named."""
        source = firmware.Source("x.bin", tmp_path / "x.bin", "924E392ED05558FFDB115408C263DCCF", None, None)

        assert "scph1001.bin" in firmware.known_names(source)


class TestRetroarchTargets:
    """Where RetroArch's system dir wants each file."""

    @pytest.fixture
    def emu(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dreamcast_info: None
    ) -> retroarch.Retroarch:
        """A RetroArch whose system dir is in tmp_path.

        Args:
            monkeypatch: Pytest's attribute patcher, undone when the test ends.
            tmp_path: The per-test temporary directory.
            dreamcast_info: The crafted catalog.

        Returns:
            The emulator.
        """
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", tmp_path / "system")
        return retroarch.Retroarch()

    def _with_core(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, core: Optional[str]
    ) -> None:
        """Pretend the emulator booted a core.

        Args:
            emu: The emulator.
            monkeypatch: Pytest's attribute patcher.
            core: The core, or None for none mapped.
        """
        monkeypatch.setattr(emu, "archive_core", lambda: core)

    def test_a_renamed_upload_lands_under_the_cores_name(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, library: Path
    ) -> None:
        """The md5 names the file, so an upload renamed by the user still lands where the core looks."""
        self._with_core(emu, monkeypatch, "flycast")
        source = _source(library, "Dreamcast BIOS (World).bin", BOOT)

        assert emu.firmware_targets(source) == [
            firmware.Target(retroarch.SYSTEM_DIR, PurePosixPath("dc/dc_boot.bin"))
        ]

    def test_an_unknown_hash_matches_by_file_name(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, library: Path
    ) -> None:
        """A file core info has no hash for lands at the core path with its name, ignoring case."""
        self._with_core(emu, monkeypatch, "flycast")
        source = _source(library, "NAOMI.zip", b"naomi set")

        assert emu.firmware_targets(source) == [
            firmware.Target(retroarch.SYSTEM_DIR, PurePosixPath("dc/naomi.zip"))
        ]

    def test_a_known_file_the_core_does_not_list_joins_its_folder(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, library: Path
    ) -> None:
        """A core keeping all its firmware in one folder gets the file there, under its known name."""
        self._with_core(emu, monkeypatch, "flycast")
        source = _source(library, "flash.rom", FLASH)

        assert emu.firmware_targets(source) == [
            firmware.Target(retroarch.SYSTEM_DIR, PurePosixPath("dc/dc_flash.bin"))
        ]

    def test_an_unknown_file_lands_at_the_root_under_its_own_name(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, library: Path
    ) -> None:
        """Nothing names the file, so it goes to the system dir as uploaded."""
        self._with_core(emu, monkeypatch, "swanstation")
        source = _source(library, "mystery.bin", b"???")

        assert emu.firmware_targets(source) == [
            firmware.Target(retroarch.SYSTEM_DIR, PurePosixPath("mystery.bin"))
        ]

    def test_an_upload_under_the_booted_cores_name_wins_over_another_cores_name(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, library: Path
    ) -> None:
        """A hash another core names differently still lands under the uploaded name the booted core reads.

        Core info knows the US Saturn BIOS only as mednafen_saturn's
        `mpr-17933.bin`, but yabasanshiro reads `saturn_bios.bin`.
        """
        _use_core_info(
            {
                "mednafen_saturn": _info(["mpr-17933.bin"], f"(!) mpr-17933.bin (md5): {_md5(BOOT)}"),
                "yabasanshiro": _info(["saturn_bios.bin"]),
            }
        )
        self._with_core(emu, monkeypatch, "yabasanshiro")
        source = _source(library, "saturn_bios.bin", BOOT)

        assert emu.firmware_targets(source) == [
            firmware.Target(retroarch.SYSTEM_DIR, PurePosixPath("saturn_bios.bin"))
        ]

    def test_the_known_name_still_wins_when_the_core_lists_it(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, library: Path
    ) -> None:
        """mednafen_saturn gets its own name for the file, whatever it was uploaded as."""
        _use_core_info(
            {
                "mednafen_saturn": _info(
                    ["mpr-17933.bin", "saturn_bios.bin"], f"(!) mpr-17933.bin (md5): {_md5(BOOT)}"
                ),
            }
        )
        self._with_core(emu, monkeypatch, "mednafen_saturn")
        source = _source(library, "saturn_bios.bin", BOOT)

        assert emu.firmware_targets(source) == [
            firmware.Target(retroarch.SYSTEM_DIR, PurePosixPath("mpr-17933.bin"))
        ]

    def test_the_bundled_core_info_puts_the_us_saturn_bios_where_yabasanshiro_reads_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, library: Path
    ) -> None:
        """Against the shipped core info, the review's case: `saturn_bios.bin` for yabasanshiro."""
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", tmp_path / "system")
        emu = retroarch.Retroarch()
        self._with_core(emu, monkeypatch, "yabasanshiro")
        source = firmware.Source(
            "saturn_bios.bin", library / "saturn_bios.bin", "3240872c70984b6cbfda1586cab68dbe", None, None
        )

        assert "mpr-17933.bin" in firmware.known_names(source)
        assert emu.firmware_targets(source) == [
            firmware.Target(retroarch.SYSTEM_DIR, PurePosixPath("saturn_bios.bin"))
        ]

    def test_no_core_falls_back_to_the_known_name_at_the_root(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, library: Path
    ) -> None:
        """Without a core the known name still applies."""
        self._with_core(emu, monkeypatch, None)
        source = _source(library, "bios.bin", PSX)

        assert emu.firmware_targets(source) == [
            firmware.Target(retroarch.SYSTEM_DIR, PurePosixPath("scph5501.bin"))
        ]

    def test_a_renamed_upload_lands_on_disk_under_the_cores_name(
        self, emu: retroarch.Retroarch, monkeypatch: pytest.MonkeyPatch, library: Path, record: Path
    ) -> None:
        """End to end through the real hook: the bytes are on disk at `system/dc/dc_boot.bin`."""
        self._with_core(emu, monkeypatch, "flycast")

        report = firmware.place(emu, [_source(library, "Dreamcast BIOS (World).bin", BOOT)])

        dest = retroarch.SYSTEM_DIR / "dc" / "dc_boot.bin"
        assert dest.read_bytes() == BOOT
        assert report["placed"] == [str(dest)]


class TestFlycastTargets:
    """Flycast takes only the Dreamcast BIOS pair, in its data dir."""

    def test_the_bios_pair_lands_by_hash_or_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, library: Path, dreamcast_info: None
    ) -> None:
        """The boot rom is found by hash, the flash by its name."""
        monkeypatch.setattr(flycast, "DATA_DIR", tmp_path / "flycast")
        emu = flycast.Flycast()

        boot = emu.firmware_targets(_source(library, "boot.rom", BOOT))
        flash = emu.firmware_targets(_source(library, "DC_FLASH.BIN", b"some flash", md5=None))

        assert boot == [firmware.Target(flycast.DATA_DIR, PurePosixPath("dc_boot.bin"))]
        assert flash == [firmware.Target(flycast.DATA_DIR, PurePosixPath("dc_flash.bin"))]

    def test_a_renamed_upload_without_hashes_is_recognised_by_its_contents(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        library: Path,
        dreamcast_info: None,
        record: Path,
    ) -> None:
        """RomM sent no hash, so the broker reads one and the boot rom still lands as `dc_boot.bin`."""
        monkeypatch.setattr(flycast, "DATA_DIR", tmp_path / "flycast")

        report = firmware.place(flycast.Flycast(), [_source(library, "boot.rom", BOOT, md5=None)])

        assert report["placed"] == [str(flycast.DATA_DIR / "dc_boot.bin")]

    def test_an_upload_named_for_the_pair_whose_hash_core_info_files_elsewhere_is_taken(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, library: Path
    ) -> None:
        """A `dc_boot.bin` core info knows only under another name still lands, by its name."""
        _use_core_info({"other": _info(["boot.rom"], f"(!) boot.rom (md5): {_md5(b'jp boot')}")})
        monkeypatch.setattr(flycast, "DATA_DIR", tmp_path / "flycast")

        targets = flycast.Flycast().firmware_targets(_source(library, "dc_boot.bin", b"jp boot"))

        assert targets == [firmware.Target(flycast.DATA_DIR, PurePosixPath("dc_boot.bin"))]

    def test_anything_else_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, library: Path, dreamcast_info: None
    ) -> None:
        """A NAOMI set or a PS1 BIOS is not Flycast standalone's business."""
        monkeypatch.setattr(flycast, "DATA_DIR", tmp_path / "flycast")

        assert flycast.Flycast().firmware_targets(_source(library, "naomi.zip", b"naomi")) == []


class TestDuckstationTargets:
    """DuckStation scans its BIOS folder by hash, so the file keeps its name."""

    @pytest.fixture
    def data(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """Point DuckStation's data root at tmp_path.

        Args:
            monkeypatch: Pytest's attribute patcher, undone when the test ends.
            tmp_path: The per-test temporary directory.

        Returns:
            The data root.
        """
        root = tmp_path / "duckstation"
        root.mkdir()
        monkeypatch.setattr(duckstation, "DATA_DIR", root)
        monkeypatch.setattr(duckstation, "INI_PATH", root / "settings.ini")
        return root

    def test_the_default_bios_folder(self, data: Path, library: Path) -> None:
        """With no settings.ini, the file goes to `bios/` under the data root."""
        targets = duckstation.Duckstation().firmware_targets(_source(library, "SCPH-1001.BIN", PSX))

        assert targets == [firmware.Target(data / "bios", PurePosixPath("SCPH-1001.BIN"))]

    def test_a_relative_search_directory_resolves_against_the_data_root(
        self, data: Path, library: Path
    ) -> None:
        """The setup wizard's relative choice is honoured."""
        (data / "settings.ini").write_text(
            "[Main]\nSetupWizardIncomplete = false\n\n[BIOS]\nSearchDirectory = roms/bios\n"
        )

        targets = duckstation.Duckstation().firmware_targets(_source(library, "bios.bin", PSX))

        assert targets == [firmware.Target(data / "roms" / "bios", PurePosixPath("bios.bin"))]

    def test_an_absolute_search_directory_is_used_as_is(
        self, data: Path, library: Path, tmp_path: Path
    ) -> None:
        """A wizard pointed at an absolute folder gets the file there."""
        (data / "settings.ini").write_text(f"[BIOS]\nSearchDirectory={tmp_path / 'mybios'}\n")

        targets = duckstation.Duckstation().firmware_targets(_source(library, "bios.bin", PSX))

        assert targets == [firmware.Target(tmp_path / "mybios", PurePosixPath("bios.bin"))]

    def test_an_unreadable_settings_file_is_logged(
        self, data: Path, library: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A settings.ini that cannot be decoded falls back to `bios/`, and says so."""
        (data / "settings.ini").write_bytes(b"[BIOS]\nSearchDirectory=\xff\xfe\n")

        with caplog.at_level(logging.WARNING, logger="webstation_broker.emulators.duckstation"):
            targets = duckstation.Duckstation().firmware_targets(_source(library, "bios.bin", PSX))

        assert targets == [firmware.Target(data / "bios", PurePosixPath("bios.bin"))]
        assert any("cannot read" in r.getMessage() for r in caplog.records)

    def test_a_psx_bios_lands_on_disk_where_duckstation_looks(
        self, data: Path, library: Path, record: Path
    ) -> None:
        """End to end through the real hook: the file is on disk in DuckStation's search folder."""
        (data / "settings.ini").write_text("[BIOS]\nSearchDirectory = roms/bios\n")

        report = firmware.place(duckstation.Duckstation(), [_source(library, "SCPH-1001.BIN", PSX)])

        dest = data / "roms" / "bios" / "SCPH-1001.BIN"
        assert dest.read_bytes() == PSX
        assert report["placed"] == [str(dest)]


class TestResolveSource:
    """Turning RomM's firmware entry into a file the broker may read."""

    def test_a_file_under_the_rom_root_resolves(self, library: Path) -> None:
        """A real path under ROM_ROOT comes back resolved, with the hash lowered."""
        (library / "dc_boot.bin").write_bytes(BOOT)

        source = firmware.resolve_source(
            "dc_boot.bin", str(library / "dc_boot.bin"), _md5(BOOT).upper(), None, 18
        )

        assert source.path == (library / "dc_boot.bin").resolve()
        assert source.md5 == _md5(BOOT)

    def test_a_path_outside_the_rom_root_is_refused(self, library: Path, tmp_path: Path) -> None:
        """A firmware path outside ROM_ROOT raises."""
        (tmp_path / "evil.bin").write_bytes(b"x")

        with pytest.raises(ValueError, match="outside"):
            firmware.resolve_source("evil.bin", str(tmp_path / "evil.bin"), None, None, None)

    def test_a_symlink_out_of_the_rom_root_is_refused(self, library: Path, tmp_path: Path) -> None:
        """A link inside ROM_ROOT pointing out of it is judged by where it points."""
        (tmp_path / "secret").write_bytes(b"x")
        (library / "link.bin").symlink_to(tmp_path / "secret")

        with pytest.raises(ValueError, match="outside"):
            firmware.resolve_source("link.bin", str(library / "link.bin"), None, None, None)

    @pytest.mark.parametrize("name", ["", ".", "..", "a/b.bin", "..\\b.bin", "x\x00.bin"])
    def test_a_file_name_that_is_not_one_name_is_refused(self, library: Path, name: str) -> None:
        """The file name becomes a path component, so it must be exactly one."""
        (library / "f.bin").write_bytes(b"x")

        with pytest.raises(ValueError):
            firmware.resolve_source(name, str(library / "f.bin"), None, None, None)

    def test_a_hash_that_is_not_hex_is_dropped(self, library: Path) -> None:
        """A malformed hash is treated as no hash at all."""
        (library / "f.bin").write_bytes(b"x")

        source = firmware.resolve_source("f.bin", str(library / "f.bin"), "not-a-hash", "zz", None)

        assert source.md5 is None
        assert source.sha1 is None


class TestPlace:
    """The shared copy: atomic, idempotent, and never over a file the broker did not put there."""

    def test_a_placed_file_keeps_the_sources_mtime(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A fresh mtime would land the BIOS in the exit dump's window, so the copy keeps the upload's."""
        source = _source(library, "dc_boot.bin", BOOT)
        os.utime(source.path, ns=(1_000_000_000, 1_000_000_000))

        firmware.place(dropper, [source])

        assert (dropper.drop_dir / "dc_boot.bin").stat().st_mtime_ns == 1_000_000_000

    def test_a_temp_file_left_by_a_killed_copy_is_swept(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The next copy of the same file clears what a killed broker left behind."""
        dropper.drop_dir.mkdir()
        stale = dropper.drop_dir / ".dc_boot.bin.broker-fw-abc123xy.tmp"
        stale.write_bytes(b"half a bios")
        _sweep_now(monkeypatch)

        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert sorted(p.name for p in dropper.drop_dir.iterdir()) == ["dc_boot.bin"]

    def test_a_fresh_temp_file_another_copy_is_writing_is_not_swept(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A copy an abandoned call is still writing is never pulled out from under it."""
        dropper.drop_dir.mkdir()
        live = dropper.drop_dir / ".dc_boot.bin.broker-fw-live1234.tmp"
        live.write_bytes(b"half a bios")
        # A finished copy carries its source's old mtime; only the change time says it is live.
        os.utime(live, (0, 0))

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert live.read_bytes() == b"half a bios"
        assert report["placed"] == [str(dropper.drop_dir / "dc_boot.bin")]

    def test_a_user_file_that_only_looks_like_a_temp_file_is_not_swept(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """Only the broker's own marked temp names are removed, and never a folder.

        `.dc_boot.bin.backup12.tmp` has the shape `tempfile` makes, so only
        the broker's marker tells it apart.
        """
        dropper.drop_dir.mkdir()
        mine = dropper.drop_dir / ".dc_boot.bin.backup12.tmp"
        mine.write_bytes(b"keep me")
        folder = dropper.drop_dir / ".dc_boot.bin.broker-fw-abcdefgh.tmp"
        folder.mkdir()

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert mine.read_bytes() == b"keep me"
        assert folder.is_dir()
        assert report["placed"] == [str(dropper.drop_dir / "dc_boot.bin")]

    def test_a_file_already_in_an_emulator_folder_inside_the_library_is_kept(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """An emulator folder inside the library is a no-copy setup, so a file there is not a failure."""
        dropper.drop_dir = library / "emu-bios"
        dropper.drop_dir.mkdir()
        (dropper.drop_dir / "dc_boot.bin").write_bytes(b"the player's own")

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["failed"] == []
        assert report["kept"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert (dropper.drop_dir / "dc_boot.bin").read_bytes() == b"the player's own"

    def test_a_subfolder_linked_into_the_library_counts_as_the_library(
        self, dropper: _Dropper, library: Path, record: Path, tmp_path: Path
    ) -> None:
        """A target subfolder that is a link into the library is the same no-copy setup as the root."""

        class _Nested(_Dropper):
            def firmware_targets(self, source: firmware.Source) -> list[firmware.Target]:
                return [firmware.Target(self.drop_dir, PurePosixPath("dc") / source.name)]

        nested = _Nested()
        nested.drop_dir = tmp_path / "system"
        nested.drop_dir.mkdir()
        (nested.drop_dir / "dc").symlink_to(library, target_is_directory=True)
        source = _source(library, "dc_boot.bin", BOOT)
        (library / "dc_flash.bin").write_bytes(b"the player's own")
        flash = firmware.Source("dc_flash.bin", library / "other.bin", None, None, 3)
        (library / "other.bin").write_bytes(b"new")

        report = firmware.place(nested, [source, flash])

        assert report["unchanged"] == [str(nested.drop_dir / "dc" / "dc_boot.bin")]
        assert report["kept"] == [str(nested.drop_dir / "dc" / "dc_flash.bin")]
        assert report["failed"] == []
        assert (library / "dc_flash.bin").read_bytes() == b"the player's own"
        assert not record.exists()

    def test_a_missing_file_in_an_emulator_folder_inside_the_library_is_failed(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """Nothing is written into the library, and a BIOS that is not there is not reported as kept."""
        dropper.drop_dir = library / "emu-bios"

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["failed"] == ["dc_boot.bin"]
        assert report["kept"] == []
        assert not dropper.drop_dir.exists()

    def test_a_source_swapped_for_a_symlink_after_the_check_is_not_read(
        self, dropper: _Dropper, library: Path, record: Path, tmp_path: Path
    ) -> None:
        """The path is re-checked when it is opened, so a swap after `resolve_source` reads nothing."""
        source = _source(library, "dc_boot.bin", BOOT, md5=None)
        secret = tmp_path / "secret.bin"
        secret.write_bytes(b"secret")
        source.path.unlink()
        source.path.symlink_to(secret)

        report = firmware.place(dropper, [source])

        assert report["failed"] == ["dc_boot.bin"]
        assert not (dropper.drop_dir / "dc_boot.bin").exists()

    def test_a_parent_folder_swapped_for_a_symlink_out_of_the_library_is_not_read(
        self, dropper: _Dropper, library: Path, record: Path, tmp_path: Path
    ) -> None:
        """The opened file is checked where the kernel says it is, so a folder swap is caught.

        The swapped-in folder's file has the same name, so only the check on
        the open descriptor can tell; a check on the path before the open
        would see the same resolved path.
        """
        folder = library / "swap"
        folder.mkdir()
        source = _source(folder, "dc_boot.bin", BOOT, md5=None)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "dc_boot.bin").write_bytes(b"secret")
        (folder / "dc_boot.bin").unlink()
        folder.rmdir()
        folder.symlink_to(outside)

        report = firmware.place(dropper, [source])

        assert report["failed"] == ["dc_boot.bin"]
        assert not (dropper.drop_dir / "dc_boot.bin").exists()

    def test_a_fifo_in_place_of_the_source_does_not_block(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A FIFO swapped in after the checks is refused at open instead of hanging the launch."""
        source = _source(library, "dc_boot.bin", BOOT)
        source.path.unlink()
        os.mkfifo(source.path)

        with pytest.raises(OSError, match="not a regular file"):
            firmware._open_source(source)

    def test_an_unwritable_target_folder_leaks_no_descriptor(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the temp file cannot be made, the source descriptor is closed."""
        source = _source(library, "dc_boot.bin", BOOT)
        dropper.drop_dir.mkdir()
        opened: list[int] = []
        real_open = firmware._open_source

        def track(src: firmware.Source) -> int:
            fd = real_open(src)
            opened.append(fd)
            return fd

        def no_space(*args: object, **kwargs: object) -> tuple[int, str]:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(firmware, "_open_source", track)
        monkeypatch.setattr(firmware.tempfile, "mkstemp", no_space)

        report = firmware.place(dropper, [source])

        assert report["failed"] == ["dc_boot.bin"]
        assert opened
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)

    def test_a_target_that_is_the_library_file_itself_counts_as_unchanged(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """An emulator already reading the library copy is not reported failed every launch."""
        source = _source(library, "dc_boot.bin", BOOT)
        dropper.drop_dir = library

        report = firmware.place(dropper, [source])

        assert report == {**firmware.empty_report(), "unchanged": [str(library / "dc_boot.bin")]}

    def test_a_recognised_file_wins_a_target_over_one_that_only_shares_its_name(
        self,
        library: Path,
        record: Path,
        tmp_path: Path,
        dreamcast_info: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The upload core info knows by hash takes the slot, whatever the names sort as."""
        monkeypatch.setattr(flycast, "DATA_DIR", tmp_path / "flycast")
        other = library / "other"
        other.mkdir()
        named_only = _source(library, "dc_boot.bin", b"not a bios")
        real = _source(other, "zzz.bin", BOOT)

        report = firmware.place(flycast.Flycast(), [named_only, real])

        assert (flycast.DATA_DIR / "dc_boot.bin").read_bytes() == BOOT
        assert report["placed"] == [str(flycast.DATA_DIR / "dc_boot.bin")]
        assert report["skipped"] == ["dc_boot.bin"]
        assert report["failed"] == []

    def test_a_hash_match_for_the_target_beats_a_known_file_matched_only_by_name(
        self, library: Path, record: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Being known to core info under some other name is not enough to win `dc_boot.bin`.

        The `dc_boot.bin` upload is a Japanese boot ROM core info knows only
        as `boot_jp.bin`, so it reaches the target by its uploaded name; the
        real boot ROM, uploaded as `zzz.bin`, is matched by hash and wins.
        """
        jp = b"jp boot rom"
        _use_core_info(
            {
                "flycast": _info(
                    ["dc/dc_boot.bin", "dc/boot_jp.bin"],
                    f"(!) dc_boot.bin (md5): {_md5(BOOT)}|(!) boot_jp.bin (md5): {_md5(jp)}",
                )
            }
        )
        monkeypatch.setattr(flycast, "DATA_DIR", tmp_path / "flycast")
        other = library / "other"
        other.mkdir()

        report = firmware.place(
            flycast.Flycast(), [_source(library, "dc_boot.bin", jp), _source(other, "zzz.bin", BOOT)]
        )

        assert (flycast.DATA_DIR / "dc_boot.bin").read_bytes() == BOOT
        assert report["skipped"] == ["dc_boot.bin"]

    def test_a_target_whose_first_claimant_failed_is_still_open_to_the_next(
        self,
        library: Path,
        record: Path,
        tmp_path: Path,
        dreamcast_info: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A copy that fails its md5 check does not block the next file wanting that target."""
        monkeypatch.setattr(flycast, "DATA_DIR", tmp_path / "flycast")
        other = library / "other"
        other.mkdir()
        stale = _source(library, "a.bin", BOOT)
        # Smaller than BOOT, so the size order still tries it first.
        stale.path.write_bytes(b"stale")
        good = _source(other, "b.bin", BOOT)

        report = firmware.place(flycast.Flycast(), [stale, good])

        assert (flycast.DATA_DIR / "dc_boot.bin").read_bytes() == BOOT
        assert report["failed"] == ["a.bin"]

    def test_an_emulator_that_places_nothing_reads_nothing(
        self, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a `firmware_targets` of its own, no source is opened, hashless ones included."""
        monkeypatch.setattr(firmware, "_source_md5", lambda s: pytest.fail("read a source"))

        report = firmware.place(FakeEmulator(), [_source(library, "dc_boot.bin", BOOT, md5=None)])

        assert report == firmware.empty_report()

    def test_a_copy_that_does_not_match_romms_sha1_is_not_placed(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """With only a sha1 to go on, it still checks the bytes before the core is handed them."""
        source = replace(_source(library, "dc_boot.bin", BOOT, md5=None), sha1=_sha1(b"other"))

        report = firmware.place(dropper, [source])

        assert report["failed"] == ["dc_boot.bin"]
        assert not (dropper.drop_dir / "dc_boot.bin").exists()

    def test_a_copy_that_matches_romms_sha1_is_placed(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """The sha1-only check passes the right bytes."""
        source = replace(_source(library, "dc_boot.bin", BOOT, md5=None), sha1=_sha1(BOOT))

        report = firmware.place(dropper, [source])

        assert report["placed"] == [str(dropper.drop_dir / "dc_boot.bin")]

    def test_a_source_that_grows_past_the_cap_while_copying_is_not_placed(
        self,
        dropper: _Dropper,
        library: Path,
        record: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The cap holds on the bytes read, not only on the size seen before the copy.

        The source carries a sha1 and no md5, so nothing reads it before
        `_copy_in`; the cap that stops it is the one inside the copy loop.
        """
        source = replace(_source(library, "dc_boot.bin", BOOT, md5=None), sha1=_sha1(BOOT))
        copied: list[str] = []
        real_copy = firmware._copy_in

        def spy(src: firmware.Source, dest: Path) -> Optional[str]:
            copied.append(src.name)
            return real_copy(src, dest)

        monkeypatch.setattr(firmware, "_copy_in", spy)
        real_stat = Path.stat

        def small_stat(self: Path, **kwargs: Any) -> os.stat_result:
            st = real_stat(self, **kwargs)
            if self == source.path:
                return os.stat_result((st.st_mode, *st[1:6], 1, *st[7:]))
            return st

        monkeypatch.setattr(firmware, "MAX_FILE_BYTES", 4)
        monkeypatch.setattr(Path, "stat", small_stat)

        with caplog.at_level(logging.ERROR, logger="webstation_broker.firmware"):
            report = firmware.place(dropper, [source])

        assert copied == ["dc_boot.bin"]
        assert "grew past the 4 byte cap while copying" in caplog.text
        assert report["failed"] == ["dc_boot.bin"]
        assert not (dropper.drop_dir / "dc_boot.bin").exists()

    def test_each_placement_is_recorded_as_it_lands(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broker killed partway through a list still owns what it already copied."""
        real = firmware._copy_in
        calls: list[str] = []

        def copy_then_die(source: firmware.Source, dest: Path) -> Optional[str]:
            calls.append(source.name)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(source, dest)

        monkeypatch.setattr(firmware, "_copy_in", copy_then_die)

        with pytest.raises(KeyboardInterrupt):
            firmware.place(
                dropper, [_source(library, "a.bin", b"aaa"), _source(library, "b.bin", b"bbb")]
            )

        assert list(json.loads(record.read_text())) == [str(dropper.drop_dir / "a.bin")]

    def test_two_files_wanting_one_target_warn_about_the_one_skipped(
        self, dropper: _Dropper, library: Path, record: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The loser is not silent: the log names it."""
        other = library / "other"
        other.mkdir()

        with caplog.at_level(logging.WARNING, logger="webstation_broker.firmware"):
            firmware.place(dropper, [_source(library, "x.bin", b"one"), _source(other, "x.bin", b"two")])

        assert "x.bin skipped, another file already took" in caplog.text

    def test_the_file_that_lost_a_target_is_reported_skipped(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """RomM hears about a file that was dropped for a collision, as skipped rather than failed."""
        other = library / "other"
        other.mkdir()

        report = firmware.place(dropper, [_source(library, "x.bin", b"one"), _source(other, "x.bin", b"two")])

        assert report["placed"] == [str(dropper.drop_dir / "x.bin")]
        assert report["skipped"] == ["x.bin"]
        assert report["failed"] == []

    def test_the_same_bytes_uploaded_twice_are_neither_skipped_nor_failed(
        self,
        library: Path,
        record: Path,
        tmp_path: Path,
        dreamcast_info: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two uploads of one BIOS under different names are one file, not a collision."""
        monkeypatch.setattr(flycast, "DATA_DIR", tmp_path / "flycast")
        other = library / "other"
        other.mkdir()

        report = firmware.place(
            flycast.Flycast(), [_source(library, "dc_boot.bin", BOOT), _source(other, "boot copy.bin", BOOT)]
        )

        assert report["placed"] == [str(flycast.DATA_DIR / "dc_boot.bin")]
        assert report["skipped"] == []
        assert report["failed"] == []

    def test_a_file_beaten_at_one_target_but_placed_at_another_is_not_skipped(
        self, library: Path, record: Path, tmp_path: Path
    ) -> None:
        """`skipped` means lost every target; one placed target keeps it out."""

        class _Split(FakeEmulator):
            def firmware_targets(self, source: firmware.Source) -> list[firmware.Target]:
                shared = firmware.Target(tmp_path / "shared", PurePosixPath("x.bin"))
                if source.path.parent == library:
                    # Sorts after `other/x.bin`, so it loses `shared` and still lands `own`.
                    return [shared, firmware.Target(tmp_path / "own", PurePosixPath("y.bin"))]
                return [shared]

        other = library / "other"
        other.mkdir()

        sources = [_source(library, "x.bin", b"one"), _source(other, "x.bin", b"two")]
        report = firmware.place(_Split(), sources)

        own, shared = tmp_path / "own" / "y.bin", tmp_path / "shared" / "x.bin"
        assert sorted(report["placed"]) == [str(own), str(shared)]
        assert report["skipped"] == []
        assert report["failed"] == []

    def test_a_target_the_user_owns_is_not_called_taken_by_another_file(
        self, dropper: _Dropper, library: Path, record: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A kept target was not taken this launch, so the next file for it is kept too, not skipped."""
        dropper.drop_dir.mkdir(parents=True, exist_ok=True)
        (dropper.drop_dir / "x.bin").write_bytes(b"mine")
        other = library / "other"
        other.mkdir()

        with caplog.at_level(logging.WARNING, logger="webstation_broker.firmware"):
            report = firmware.place(
                dropper, [_source(library, "x.bin", b"one"), _source(other, "x.bin", b"two")]
            )

        assert "another file already took" not in caplog.text
        assert report["kept"] == [str(dropper.drop_dir / "x.bin")]
        assert report["skipped"] == []
        assert (dropper.drop_dir / "x.bin").read_bytes() == b"mine"

    def test_a_sha1_only_source_is_read_once_when_placed(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The md5 written by the copy is reused; the source is not read again after it lands."""
        source = replace(_source(library, "dc_boot.bin", BOOT, md5=None), sha1=_sha1(BOOT))

        def no_read(src: firmware.Source) -> str:
            raise OSError(5, "the library went away after the copy")

        monkeypatch.setattr(firmware, "_source_md5", no_read)

        report = firmware.place(dropper, [source])

        dest = dropper.drop_dir / "dc_boot.bin"
        assert report["placed"] == [str(dest)]
        assert report["failed"] == []
        assert json.loads(record.read_text())[str(dest)]["md5"] == _md5(BOOT)

    def test_the_summary_log_counts_skipped(
        self, dropper: _Dropper, library: Path, record: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The one-line summary names every bucket the report has."""
        other = library / "other"
        other.mkdir()

        with caplog.at_level(logging.INFO, logger="webstation_broker.firmware"):
            firmware.place(dropper, [_source(library, "x.bin", b"one"), _source(other, "x.bin", b"two")])

        assert "1 placed, 0 unchanged, 0 kept, 1 skipped, 0 failed" in caplog.text

    def test_one_target_failing_does_not_skip_the_sources_other_targets(
        self, library: Path, record: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RetroArch can want a file in several places; one bad place costs only that one."""

        class _Two(FakeEmulator):
            def firmware_targets(self, source: firmware.Source) -> list[firmware.Target]:
                return [
                    firmware.Target(tmp_path / "a", PurePosixPath(source.name)),
                    firmware.Target(tmp_path / "b", PurePosixPath(source.name)),
                ]

        real = firmware._place_one

        def first_fails(source: firmware.Source, size: int, target: firmware.Target, *rest: Any) -> str:
            if target.root == tmp_path / "a":
                raise OSError(5, "I/O error")
            return real(source, size, target, *rest)

        monkeypatch.setattr(firmware, "_place_one", first_fails)

        report = firmware.place(_Two(), [_source(library, "dc_boot.bin", BOOT)])

        assert report["placed"] == [str(tmp_path / "b" / "dc_boot.bin")]
        assert report["failed"] == ["dc_boot.bin"]

    def test_a_hashless_source_over_the_cap_is_never_read_in_full(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Working out a missing md5 stops at the cap like the copy does."""
        source = _source(library, "dc_boot.bin", BOOT, md5=None)
        monkeypatch.setattr(firmware, "MAX_FILE_BYTES", 4)

        with pytest.raises(OSError, match="over the 4 byte cap"):
            firmware._source_md5(source)

    def test_a_missing_file_is_copied_and_owned(self, dropper: _Dropper, library: Path, record: Path) -> None:
        """A file the target lacks is copied in, reported, and recorded as the broker's."""
        source = _source(library, "dc_boot.bin", BOOT)

        report = firmware.place(dropper, [source])

        dest = dropper.drop_dir / "dc_boot.bin"
        assert dest.read_bytes() == BOOT
        assert report["placed"] == [str(dest)]
        assert json.loads(record.read_text())[str(dest)]["md5"] == _md5(BOOT)
        assert [p.name for p in dropper.drop_dir.iterdir()] == ["dc_boot.bin"]

    def test_a_second_place_writes_nothing(self, dropper: _Dropper, library: Path, record: Path) -> None:
        """Unchanged firmware leaves the placed file's mtime alone."""
        source = _source(library, "dc_boot.bin", BOOT)
        firmware.place(dropper, [source])
        dest = dropper.drop_dir / "dc_boot.bin"
        os.utime(dest, ns=(1_000_000_000, 1_000_000_000))
        firmware.place(dropper, [source])
        stamp = dest.stat().st_mtime_ns

        report = firmware.place(dropper, [source])

        assert dest.stat().st_mtime_ns == stamp
        assert report["placed"] == []
        assert report["unchanged"] == [str(dest)]

    def test_an_identical_file_the_user_placed_is_left_and_not_claimed(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """Same bytes already there: nothing is written and the file stays the user's."""
        dropper.drop_dir.mkdir()
        dest = dropper.drop_dir / "dc_boot.bin"
        dest.write_bytes(BOOT)
        os.utime(dest, ns=(1_000_000_000, 1_000_000_000))

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert dest.stat().st_mtime_ns == 1_000_000_000
        assert report["unchanged"] == [str(dest)]
        assert str(dest) not in (json.loads(record.read_text()) if record.exists() else {})

    def test_a_different_file_the_user_placed_is_kept(
        self, dropper: _Dropper, library: Path, record: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A user's own BIOS with other bytes is never overwritten, and the kept file is logged."""
        dropper.drop_dir.mkdir()
        dest = dropper.drop_dir / "dc_boot.bin"
        dest.write_bytes(b"the user's own dump")

        with caplog.at_level(logging.INFO, logger="webstation_broker.firmware"):
            report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert dest.read_bytes() == b"the user's own dump"
        assert report["kept"] == [str(dest)]
        assert str(dest) in caplog.text

    def test_a_file_the_broker_placed_is_replaced_when_romm_changes(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """New firmware in RomM replaces the copy the broker put there itself."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", b"a better dump")])

        dest = dropper.drop_dir / "dc_boot.bin"
        assert dest.read_bytes() == b"a better dump"
        assert report["placed"] == [str(dest)]
        assert json.loads(record.read_text())[str(dest)]["md5"] == _md5(b"a better dump")

    def test_a_placed_file_changed_since_is_kept(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """Once something else rewrites the broker's copy, it stops being the broker's to replace."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        dest = dropper.drop_dir / "dc_boot.bin"
        dest.write_bytes(b"written by the emulator")

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", b"a better dump")])

        assert dest.read_bytes() == b"written by the emulator"
        assert report["kept"] == [str(dest)]

    def test_a_copy_that_does_not_match_romms_hash_is_discarded(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A source whose bytes disagree with RomM's md5 never lands."""
        source = _source(library, "dc_boot.bin", BOOT, md5=_md5(b"something else"))

        report = firmware.place(dropper, [source])

        assert report["failed"] == ["dc_boot.bin"]
        assert not dropper.drop_dir.exists() or list(dropper.drop_dir.iterdir()) == []

    def test_a_missing_source_fails_alone(self, dropper: _Dropper, library: Path, record: Path) -> None:
        """One file gone from the library does not stop the rest."""
        gone = _source(library, "gone.bin", b"x")
        gone.path.unlink()
        here = _source(library, "here.bin", b"y")

        report = firmware.place(dropper, [gone, here])

        assert report["failed"] == ["gone.bin"]
        assert report["placed"] == [str(dropper.drop_dir / "here.bin")]

    def test_an_oversized_source_is_skipped(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file over the size cap is never copied."""
        monkeypatch.setattr(firmware, "MAX_FILE_BYTES", 4)

        report = firmware.place(dropper, [_source(library, "big.bin", b"too large")])

        assert report["failed"] == ["big.bin"]
        assert not (dropper.drop_dir / "big.bin").exists()

    def test_a_rename_that_fails_leaves_nothing_behind(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copy whose final rename fails leaves neither a torn target nor a temp file."""
        dropper.drop_dir.mkdir()

        def boom(*_args: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(firmware.os, "replace", boom)

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["failed"] == ["dc_boot.bin"]
        assert list(dropper.drop_dir.iterdir()) == []

    def test_a_write_that_fails_midway_leaves_nothing_behind(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copy that dies part-way through writing leaves no target and no temp file."""
        dropper.drop_dir.mkdir()
        monkeypatch.setattr(firmware, "_CHUNK", 4)
        real_open = os.fdopen
        writes: list[int] = []

        class _Breaking:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            def __enter__(self) -> "_Breaking":
                self.inner.__enter__()
                return self

            def __exit__(self, *exc: Any) -> None:
                self.inner.__exit__(*exc)

            def write(self, chunk: bytes) -> int:
                writes.append(len(chunk))
                if len(writes) == 2:
                    raise OSError(28, "No space left on device")
                return int(self.inner.write(chunk))

            def __getattr__(self, name: str) -> Any:
                return getattr(self.inner, name)

        def fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            fh = real_open(fd, mode, *args, **kwargs)
            return _Breaking(fh) if "w" in mode else fh

        monkeypatch.setattr(firmware.os, "fdopen", fdopen)

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert len(writes) == 2
        assert report["failed"] == ["dc_boot.bin"]
        assert list(dropper.drop_dir.iterdir()) == []

    def _clock(self, monkeypatch: pytest.MonkeyPatch, budget: float) -> list[float]:
        """A fake monotonic clock, advanced by hand, and a placement budget.

        Args:
            monkeypatch: Pytest's attribute patcher.
            budget: The budget in fake seconds.

        Returns:
            The clock's one-item list; set `[0]` to move time.
        """
        now = [0.0]
        monkeypatch.setattr(firmware.time, "monotonic", lambda: now[0])
        monkeypatch.setattr(settings, "FIRMWARE_PLACE_BUDGET", budget)
        return now

    def test_a_copy_that_runs_out_of_time_keeps_the_old_file_and_its_record(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A replacement cut short by the budget leaves the broker's old copy and its record entry."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        dest = dropper.drop_dir / "dc_boot.bin"
        entry = json.loads(record.read_text())[str(dest)]
        monkeypatch.setattr(firmware, "_CHUNK", 4)
        now = self._clock(monkeypatch, 10.0)
        real_tick = firmware._tick
        ticks: list[int] = []

        def tick() -> None:
            ticks.append(1)
            if len(ticks) == 3:
                now[0] = 11.0
            real_tick()

        monkeypatch.setattr(firmware, "_tick", tick)

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", FLASH)])

        assert report["failed"] == ["dc_boot.bin"]
        assert dest.read_bytes() == BOOT
        assert json.loads(record.read_text())[str(dest)] == entry
        assert sorted(p.name for p in dropper.drop_dir.iterdir()) == ["dc_boot.bin"]

    def test_hashing_a_hashless_source_counts_against_the_budget(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The content hash `_plan` reads for a hashless source stops once the budget is spent."""
        now = self._clock(monkeypatch, 10.0)
        real_read = firmware._source_md5

        def slow_read(src: firmware.Source) -> str:
            now[0] = 11.0
            return real_read(src)

        monkeypatch.setattr(firmware, "_source_md5", slow_read)

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT, md5=None)])

        assert report["failed"] == ["dc_boot.bin"]
        assert not (dropper.drop_dir / "dc_boot.bin").exists()

    def test_a_file_already_in_place_is_unchanged_after_the_budget_is_spent(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A target settled from the record without a read is not reported failed for lack of time."""
        source = _source(library, "dc_boot.bin", BOOT)
        firmware.place(dropper, [source])
        now = self._clock(monkeypatch, 10.0)
        now[0] = 0.0
        real_load = firmware._load_record

        def load_late() -> dict[str, dict[str, Any]]:
            now[0] = 11.0
            return real_load()

        monkeypatch.setattr(firmware, "_load_record", load_late)

        report = firmware.place(dropper, [source])

        assert report["unchanged"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert report["failed"] == []

    def test_small_files_go_before_a_large_one(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A large file that cannot finish in time is tried last, so it never starves the small ones."""
        other = library / "other"
        other.mkdir()
        big = _source(library, "a-big.bin", b"x" * 64)
        small = _source(other, "z-small.bin", BOOT)
        monkeypatch.setattr(firmware, "_CHUNK", 4)
        now = self._clock(monkeypatch, 10.0)
        real_tick = firmware._tick
        started: list[str] = []
        real_copy = firmware._copy_in

        def copy(src: firmware.Source, dest: Path) -> Optional[str]:
            started.append(src.name)
            return real_copy(src, dest)

        def tick() -> None:
            if started and started[-1] == "a-big.bin":
                now[0] = 11.0
            real_tick()

        monkeypatch.setattr(firmware, "_copy_in", copy)
        monkeypatch.setattr(firmware, "_tick", tick)

        report = firmware.place(dropper, [big, small])

        assert started == ["z-small.bin", "a-big.bin"]
        assert report["placed"] == [str(dropper.drop_dir / "z-small.bin")]
        assert report["failed"] == ["a-big.bin"]

    def test_once_activate_stops_waiting_a_copy_in_flight_is_not_renamed(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop set while the bytes are being written keeps the file from landing.

        The flag is set after the last read, so only the check before the
        rename can stop it.
        """
        stop = threading.Event()
        real_utime = os.utime

        def utime(path: Any, *args: Any, **kwargs: Any) -> None:
            stop.set()
            real_utime(path, *args, **kwargs)

        monkeypatch.setattr(firmware.os, "utime", utime)

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)], stop)

        assert report["failed"] == ["dc_boot.bin"]
        assert list(dropper.drop_dir.iterdir()) == []
        assert not record.exists()

    def test_once_activate_stops_waiting_no_further_target_is_started(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the stop, the next source is not even opened, and nothing is swept for it."""
        stop = threading.Event()
        real_copy = firmware._copy_in
        started: list[str] = []

        def copy(src: firmware.Source, dest: Path) -> Any:
            started.append(src.name)
            result = real_copy(src, dest)
            stop.set()
            return result

        monkeypatch.setattr(firmware, "_copy_in", copy)
        _sweep_now(monkeypatch)
        other = library / "other"
        other.mkdir()
        dropper.drop_dir.mkdir()
        names = ("dc_boot.bin", "dc_flash.bin")
        leftovers = [dropper.drop_dir / f".{n}.broker-fw-leftover.tmp" for n in names]
        for leftover in leftovers:
            leftover.write_bytes(b"stale")

        report = firmware.place(
            dropper, [_source(library, "dc_boot.bin", BOOT), _source(other, "dc_flash.bin", FLASH)], stop
        )

        assert len(started) == 1
        # The target that was never started keeps its leftover: nothing was swept for it.
        assert [p.exists() for p in leftovers] == [n != started[0] for n in names]
        landed = str(dropper.drop_dir / started[0])
        assert report["placed"] == [landed]
        assert report["failed"] == [n for n in ("dc_boot.bin", "dc_flash.bin") if n != started[0]]
        # The file that landed before the stop is still recorded, or it would read as the user's.
        assert landed in json.loads(record.read_text())

    def test_a_late_record_write_keeps_what_a_newer_call_recorded(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """Each write merges into the record on disk, so two calls never drop each other's entries."""
        other = library / "other"
        other.mkdir()
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        boot = str(dropper.drop_dir / "dc_boot.bin")
        # A call that loaded the record before dc_boot.bin was recorded lands dc_flash.bin late.
        stale_view: dict[str, dict[str, Any]] = {}
        firmware.place(dropper, [_source(other, "dc_flash.bin", FLASH)])
        flash = str(dropper.drop_dir / "dc_flash.bin")
        stale_view[flash] = json.loads(record.read_text())[flash]
        record.write_text(json.dumps({boot: json.loads(record.read_text())[boot]}))

        firmware._save_record(stale_view)

        assert set(json.loads(record.read_text())) == {boot, flash}

    def test_a_stamp_for_a_file_replaced_since_is_not_recorded(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """An entry whose size and time no longer match the file there is dropped, not written."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        dest = dropper.drop_dir / "dc_boot.bin"
        entry = json.loads(record.read_text())[str(dest)]
        dest.write_bytes(b"the user's own file, longer than the bios")

        firmware._save_record({str(dest): dict(entry, md5="0" * 32)})

        assert json.loads(record.read_text())[str(dest)]["md5"] == entry["md5"]

    def test_the_budget_is_cleared_after_place(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outside `place` a read is never stopped, whatever the clock says."""
        now = self._clock(monkeypatch, 10.0)
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        now[0] = 1e9

        firmware._tick()

    def test_a_record_that_cannot_be_read_is_not_overwritten(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A passing read error never replaces the record with only this launch's entries."""
        other = library / "other"
        other.mkdir()
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        before = record.read_text()
        real_read = Path.read_text

        def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == record:
                raise PermissionError("transient")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)

        report = firmware.place(dropper, [_source(other, "dc_flash.bin", FLASH)])

        assert report["placed"] == [str(dropper.drop_dir / "dc_flash.bin")]
        monkeypatch.setattr(Path, "read_text", real_read)
        assert record.read_text() == before

    def test_a_record_write_that_failed_is_tried_again_at_the_end(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A placed file whose first record write failed is still recorded by the final write."""
        calls: list[int] = []
        real_write = firmware._write_record

        def write(path: Path, data: dict[str, dict[str, Any]]) -> bool:
            calls.append(len(data))
            if len(calls) == 1:
                return False
            return real_write(path, data)

        monkeypatch.setattr(firmware, "_write_record", write)

        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert len(calls) == 2
        assert str(dropper.drop_dir / "dc_boot.bin") in json.loads(record.read_text())

    def test_place_writes_only_its_own_changes_over_a_newer_record(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Entries another call recorded after this one loaded the record survive its writes."""
        newer = {"md5": "0" * 32, "size": 1, "mtime_ns": 1}
        real_copy = firmware._copy_in

        def copy(src: firmware.Source, dest: Path) -> Any:
            # Another call records a file while this one is copying.
            record.write_text(json.dumps({"/elsewhere/other.bin": newer}))
            return real_copy(src, dest)

        monkeypatch.setattr(firmware, "_copy_in", copy)

        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        data = json.loads(record.read_text())
        assert data["/elsewhere/other.bin"] == newer
        assert str(dropper.drop_dir / "dc_boot.bin") in data

    def test_a_stamp_whose_time_no_longer_matches_is_not_recorded(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """Same size, different mtime: the file was replaced since, so the entry is dropped."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        dest = dropper.drop_dir / "dc_boot.bin"
        entry = json.loads(record.read_text())[str(dest)]
        os.utime(dest, ns=(entry["mtime_ns"] + 10**9, entry["mtime_ns"] + 10**9))

        firmware._save_record({str(dest): dict(entry, md5="0" * 32)})

        assert json.loads(record.read_text())[str(dest)]["md5"] == entry["md5"]

    def test_a_target_hook_that_raises_on_dest_fails_only_that_source(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A malformed target from a hook is isolated like any other per-target error."""

        class _Bad(firmware.Target):
            @property
            def dest(self) -> Path:
                raise RuntimeError("bad target")

        class _Mixed(_Dropper):
            def firmware_targets(self, source: firmware.Source) -> list[firmware.Target]:
                if source.name == "dc_flash.bin":
                    return [_Bad(self.drop_dir, PurePosixPath(source.name))]
                return super().firmware_targets(source)

        mixed = _Mixed()
        mixed.drop_dir = dropper.drop_dir
        other = library / "other"
        other.mkdir()

        report = firmware.place(
            mixed, [_source(library, "dc_boot.bin", BOOT), _source(other, "dc_flash.bin", FLASH)]
        )

        assert report["placed"] == [str(mixed.drop_dir / "dc_boot.bin")]
        assert report["failed"] == ["dc_flash.bin"]

    def test_a_leftover_beside_a_file_already_in_place_is_swept(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relaunch that finds the target unchanged still clears what a killed copy left."""
        source = _source(library, "dc_boot.bin", BOOT)
        firmware.place(dropper, [source])
        leftover = dropper.drop_dir / ".dc_boot.bin.broker-fw-leftover.tmp"
        leftover.write_bytes(b"stale")
        _sweep_now(monkeypatch)

        report = firmware.place(dropper, [source])

        assert report["unchanged"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert not leftover.exists()

    def test_the_sweep_age_grows_with_a_long_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A budget near a day still leaves a live copy well clear of the sweep."""
        monkeypatch.setattr(settings, "FIRMWARE_PLACE_BUDGET", 7200.0)

        assert firmware._stale_after() > 2 * 7200.0

    def test_place_honours_the_budget_it_is_given(
        self,
        dropper: _Dropper,
        library: Path,
        record: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The budget activate passes down, not the setting, is the limit, and the log says so."""
        monkeypatch.setattr(settings, "FIRMWARE_PLACE_BUDGET", 1000.0)
        now = [0.0]
        monkeypatch.setattr(firmware.time, "monotonic", lambda: now[0])
        real_tick = firmware._tick

        def tick() -> None:
            now[0] = 3.0
            real_tick()

        monkeypatch.setattr(firmware, "_tick", tick)

        with caplog.at_level(logging.ERROR, logger="webstation_broker.firmware"):
            report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)], budget=2.0)

        assert report["failed"] == ["dc_boot.bin"]
        assert "out of time after 2 s" in caplog.text

    def test_a_negative_budget_fails_every_copy_but_still_settles_what_is_in_place(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A list check that used up the whole budget leaves nothing to copy, but nothing breaks."""
        other = library / "other"
        other.mkdir()
        done = _source(library, "dc_boot.bin", BOOT)
        firmware.place(dropper, [done])

        report = firmware.place(dropper, [done, _source(other, "dc_flash.bin", FLASH)], budget=-5.0)

        assert report["unchanged"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert report["failed"] == ["dc_flash.bin"]

    def test_a_stamp_for_a_file_that_is_gone_is_not_recorded(
        self, dropper: _Dropper, library: Path, record: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An entry for a file removed since it was stamped is dropped, and the log says why."""
        gone = str(dropper.drop_dir / "gone.bin")

        with caplog.at_level(logging.WARNING, logger="webstation_broker.firmware"):
            firmware._save_record({gone: {"md5": "0" * 32, "size": 1, "mtime_ns": 1}})

        assert gone not in json.loads(record.read_text())
        assert "which is gone" in caplog.text

    def test_two_sources_sharing_a_name_fail_once(
        self, dropper: _Dropper, library: Path, record: Path, tmp_path: Path
    ) -> None:
        """A file name is listed in `failed` once, however many sources carry it."""
        other = library / "other"
        other.mkdir()
        first = _source(library, "dc_boot.bin", BOOT, md5="0" * 32)
        second = _source(other, "dc_boot.bin", BOOT, md5="1" * 32)

        report = firmware.place(dropper, [first, second])

        assert report["failed"] == ["dc_boot.bin"]

    def test_the_stop_flag_is_cleared_after_place(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A worker thread reused after an abandoned call does not inherit its stop flag."""
        stop = threading.Event()
        stop.set()
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)], stop)

        assert not firmware._stopped()
        firmware._tick()

    def test_a_record_temp_left_by_a_killed_broker_is_swept(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record's own temp files carry the marker and are cleaned on the next write."""
        record.parent.mkdir(parents=True, exist_ok=True)
        stale = record.parent / f".{record.name}.broker-fw-abcd1234.tmp"
        stale.write_text("{")
        _sweep_now(monkeypatch)
        unrelated = record.parent / f".{record.name}.notours.tmp"
        unrelated.write_text("{")

        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert not stale.exists()
        assert unrelated.exists()
        assert [p.name for p in record.parent.glob(f".{record.name}.*broker-fw-*")] == []

    def test_a_sha1_only_source_is_not_reread_once_placed(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record keeps RomM's sha1, so the next launch settles "unchanged" without a read."""
        source = replace(_source(library, "dc_boot.bin", BOOT, md5=None), sha1=_sha1(BOOT))
        firmware.place(dropper, [source])
        assert json.loads(record.read_text())[str(dropper.drop_dir / "dc_boot.bin")]["sha1"] == _sha1(BOOT)
        opened: list[str] = []
        real_open = firmware._open_source

        def spy(src: firmware.Source) -> int:
            opened.append(src.name)
            return real_open(src)

        monkeypatch.setattr(firmware, "_open_source", spy)

        report = firmware.place(dropper, [source])

        assert report["unchanged"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert opened == []

    def test_a_sha1_only_source_whose_sha1_changed_is_replaced(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A new upload under the same name, with a new sha1, replaces the broker's old copy."""
        firmware.place(dropper, [replace(_source(library, "dc_boot.bin", BOOT, md5=None), sha1=_sha1(BOOT))])

        newer = replace(_source(library, "dc_boot.bin", FLASH, md5=None), sha1=_sha1(FLASH))
        report = firmware.place(dropper, [newer])

        assert report["placed"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert (dropper.drop_dir / "dc_boot.bin").read_bytes() == FLASH

    def test_a_failed_hash_after_an_in_place_file_does_not_fail_it(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the emulator already reads the library copy, a read error after that is not a failure."""
        source = replace(_source(library, "dc_boot.bin", BOOT, md5=None), sha1=_sha1(BOOT))
        dropper.drop_dir.mkdir()
        (dropper.drop_dir / "dc_boot.bin").symlink_to(source.path)

        def broken(_src: firmware.Source) -> str:
            raise OSError(5, "I/O error")

        monkeypatch.setattr(firmware, "_source_md5", broken)

        report = firmware.place(dropper, [source])

        assert report["unchanged"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert report["failed"] == []

    def test_a_target_one_source_keeps_and_another_matches_is_only_unchanged(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A target lands in one bucket: the file there is right, so it is not also "kept"."""
        dropper.drop_dir.mkdir()
        (dropper.drop_dir / "dc_boot.bin").write_bytes(BOOT)
        monkeypatch.setattr(
            _Dropper,
            "firmware_targets",
            lambda self, s: [firmware.Target(self.drop_dir, PurePosixPath("dc_boot.bin"))],
        )
        other = library / "other"
        other.mkdir()
        wrong = _source(library, "a.bin", b"something else")
        right = _source(other, "b.bin", BOOT)

        report = firmware.place(dropper, [wrong, right])

        key = str(dropper.drop_dir / "dc_boot.bin")
        assert report["unchanged"] == [key]
        assert report["kept"] == []

    def test_without_proc_a_swapped_file_is_caught_by_its_identity(
        self,
        dropper: _Dropper,
        library: Path,
        record: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The /proc-less fallback checks the resolved path still holds the open file."""
        source = _source(library, "dc_boot.bin", BOOT)
        real_readlink = os.readlink
        real_open = os.open

        def no_proc(path: Any, *args: Any, **kwargs: Any) -> str:
            if str(path).startswith("/proc/self/fd/"):
                raise OSError(2, "No such file or directory")
            return real_readlink(path, *args, **kwargs)

        def open_then_swap(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            if Path(path) == source.path:
                source.path.unlink()
                source.path.write_bytes(BOOT)
            return fd

        monkeypatch.setattr(firmware.os, "readlink", no_proc)
        monkeypatch.setattr(firmware.os, "open", open_then_swap)

        with pytest.raises(OSError, match="changed while it was opened"):
            firmware._open_source(source)

    def test_without_proc_an_untouched_file_still_opens(
        self, library: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback lets an ordinary source through."""
        source = _source(library, "dc_boot.bin", BOOT)
        real_readlink = os.readlink

        def no_proc(path: Any, *args: Any, **kwargs: Any) -> str:
            if str(path).startswith("/proc/self/fd/"):
                raise OSError(2, "No such file or directory")
            return real_readlink(path, *args, **kwargs)

        monkeypatch.setattr(firmware.os, "readlink", no_proc)

        fd = firmware._open_source(source)
        os.close(fd)

    def test_a_target_that_climbs_out_of_its_root_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, library: Path, record: Path
    ) -> None:
        """A hook handing back `..` never writes outside its root."""
        emu = _Dropper()
        emu.drop_dir = tmp_path / "root"
        monkeypatch.setattr(
            _Dropper,
            "firmware_targets",
            lambda self, s: [firmware.Target(self.drop_dir, PurePosixPath("../escaped.bin"))],
        )

        report = firmware.place(emu, [_source(library, "x.bin", b"x")])

        assert report["failed"] == ["x.bin"]
        assert not (tmp_path / "escaped.bin").exists()

    def test_a_symlinked_folder_out_of_the_root_is_refused(
        self, dropper: _Dropper, tmp_path: Path, library: Path, record: Path
    ) -> None:
        """A folder under the root that links elsewhere is not followed."""
        outside = tmp_path / "outside"
        outside.mkdir()
        dropper.drop_dir.mkdir()
        (dropper.drop_dir / "sub").symlink_to(outside)
        dropper.firmware_targets = lambda s: [firmware.Target(dropper.drop_dir, PurePosixPath("sub/x.bin"))]  # type: ignore[method-assign]

        report = firmware.place(dropper, [_source(library, "x.bin", b"x")])

        assert report["failed"] == ["x.bin"]
        assert list(outside.iterdir()) == []

    def test_a_symlink_at_the_target_is_the_users(
        self, dropper: _Dropper, tmp_path: Path, library: Path, record: Path
    ) -> None:
        """A link the user put at the target is kept, and what it points at is untouched."""
        elsewhere = tmp_path / "elsewhere.bin"
        elsewhere.write_bytes(b"the user's")
        dropper.drop_dir.mkdir()
        (dropper.drop_dir / "dc_boot.bin").symlink_to(elsewhere)

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["kept"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert elsewhere.read_bytes() == b"the user's"

    def test_a_corrupt_record_counts_as_empty(self, dropper: _Dropper, library: Path, record: Path) -> None:
        """An unreadable record never stops placement; nothing is assumed owned."""
        record.write_text("{not json")
        dropper.drop_dir.mkdir()
        (dropper.drop_dir / "dc_boot.bin").write_bytes(b"older")

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["kept"] == [str(dropper.drop_dir / "dc_boot.bin")]

    def test_a_hook_that_raises_fails_its_file_only(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashing hook is logged as a failure, never raised into the launch."""

        def broken(self: _Dropper, source: firmware.Source) -> list[firmware.Target]:
            raise RuntimeError("bad ini")

        monkeypatch.setattr(_Dropper, "firmware_targets", broken)

        report = firmware.place(dropper, [_source(library, "x.bin", b"x")])

        assert report["failed"] == ["x.bin"]

    def test_the_base_emulator_places_nothing(self, library: Path, record: Path) -> None:
        """An emulator without the hook gets no files."""
        report = firmware.place(FakeEmulator(), [_source(library, "x.bin", b"x")])

        assert report == firmware.empty_report()


class TestPlaceEdgeCases:
    """Odd inputs and half-finished state: placement degrades to keeping files, never to losing them."""

    @pytest.mark.parametrize("ahead_s", [0, 3600], ids=["just-uploaded", "clock-ahead"])
    def test_a_source_newer_than_the_dump_window_is_backdated(
        self, dropper: _Dropper, library: Path, record: Path, ahead_s: int
    ) -> None:
        """A BIOS stamped at or after launch would ship as a save; its copy is stamped before the window."""
        source = _source(library, "dc_boot.bin", BOOT)
        stamp = time.time_ns() + ahead_s * 1_000_000_000
        os.utime(source.path, ns=(stamp, stamp))

        firmware.place(dropper, [source])

        placed = (dropper.drop_dir / "dc_boot.bin").stat().st_mtime_ns
        assert placed <= time.time_ns() - firmware._MTIME_BACKDATE_NS

    @pytest.mark.parametrize(
        "content",
        [
            "[]",
            '"a string"',
            "null",
            '{"%(dest)s": "not an entry"}',
            '{"%(dest)s": {}}',
            '{"%(dest)s": {"md5": 5, "size": "big", "mtime_ns": null}}',
            "",
        ],
        ids=["list", "string", "null", "entry-not-object", "entry-empty", "entry-wrong-types", "empty-file"],
    )
    def test_a_malformed_record_owns_nothing(
        self, dropper: _Dropper, library: Path, record: Path, content: str
    ) -> None:
        """Whatever shape the record is in, a file it cannot vouch for is kept."""
        dropper.drop_dir.mkdir()
        dest = dropper.drop_dir / "dc_boot.bin"
        dest.write_bytes(b"older")
        record.write_text(content % {"dest": dest} if "%(dest)s" in content else content)

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["kept"] == [str(dest)]
        assert dest.read_bytes() == b"older"

    def test_a_record_that_cannot_be_written_still_places(
        self,
        dropper: _Dropper,
        library: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A read-only /config costs ownership, logged, but the launch gets its BIOS."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setattr(settings, "FIRMWARE_RECORD", blocker / "broker-firmware.json")

        with caplog.at_level(logging.ERROR, logger="webstation_broker.firmware"):
            report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["placed"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert "could not write the record" in caplog.text

    def test_without_its_record_a_placed_file_is_no_longer_replaced(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """Losing the record (a wiped /config) makes the broker careful, not destructive."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        record.unlink()
        dest = dropper.drop_dir / "dc_boot.bin"

        same = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        newer = firmware.place(dropper, [_source(library, "dc_boot.bin", b"a better dump")])

        assert same["unchanged"] == [str(dest)]
        assert newer["kept"] == [str(dest)]
        assert dest.read_bytes() == BOOT

    def test_a_record_write_that_dies_midway_leaves_the_old_record(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record is swapped in whole or not at all, and no temp file is left beside it."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        before = record.read_text()

        def torn(obj: Any, fh: Any, **_kwargs: Any) -> None:
            fh.write('{"half')
            raise OSError("disk full")

        monkeypatch.setattr(firmware.json, "dump", torn)

        firmware.place(dropper, [_source(library, "dc_boot.bin", b"a better dump")])

        assert record.read_text() == before
        assert sorted(p.name for p in record.parent.iterdir() if p.name.startswith(".broker-firmware")) == []

    def test_a_placed_file_deleted_since_is_placed_again(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A BIOS removed while an older broker ran (or by hand) comes back on the next launch."""
        source = _source(library, "dc_boot.bin", BOOT)
        firmware.place(dropper, [source])
        dest = dropper.drop_dir / "dc_boot.bin"
        dest.unlink()

        report = firmware.place(dropper, [source])

        assert report["placed"] == [str(dest)]
        assert dest.read_bytes() == BOOT

    def test_a_placed_file_only_touched_since_stays_the_brokers(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A new mtime on the same bytes is not an edit; a later RomM change still replaces the file."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        dest = dropper.drop_dir / "dc_boot.bin"
        os.utime(dest, ns=(2_000_000_000, 2_000_000_000))

        touched = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])
        changed = firmware.place(dropper, [_source(library, "dc_boot.bin", b"a better dump")])

        assert touched["unchanged"] == [str(dest)]
        assert changed["placed"] == [str(dest)]

    def test_a_source_that_disagrees_with_romm_leaves_the_placed_copy(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A library file swapped under RomM's feet fails, and the good copy already placed stays."""
        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        report = firmware.place(
            dropper, [_source(library, "dc_boot.bin", b"swapped", md5=_md5(b"rescanned"))]
        )

        assert report["failed"] == ["dc_boot.bin"]
        assert (dropper.drop_dir / "dc_boot.bin").read_bytes() == BOOT

    @pytest.mark.parametrize("kind", ["directory", "dangling-link"])
    def test_a_target_that_is_not_a_file_is_kept(
        self, dropper: _Dropper, library: Path, record: Path, tmp_path: Path, kind: str
    ) -> None:
        """A folder or a broken link where the BIOS goes is left as found."""
        dropper.drop_dir.mkdir()
        dest = dropper.drop_dir / "dc_boot.bin"
        if kind == "directory":
            dest.mkdir()
        else:
            dest.symlink_to(tmp_path / "nowhere.bin")

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["kept"] == [str(dest)]
        assert os.path.lexists(dest) and not dest.is_file()

    @pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
    def test_two_files_wanting_one_target_settle_the_same_way(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
    ) -> None:
        """Whatever order RomM lists them in, one wins and keeps winning; nothing flips each launch."""
        monkeypatch.setattr(
            _Dropper,
            "firmware_targets",
            lambda self, s: [firmware.Target(self.drop_dir, PurePosixPath("bios.bin"))],
        )
        sources = [_source(library, "a.bin", b"first"), _source(library, "b.bin", b"second")]
        if reverse:
            sources.reverse()

        first = firmware.place(dropper, sources)
        second = firmware.place(dropper, sources)

        dest = dropper.drop_dir / "bios.bin"
        assert dest.read_bytes() == b"first"
        assert first["placed"] == [str(dest)]
        assert second["unchanged"] == [str(dest)]
        assert second["placed"] == []

    def test_the_same_file_listed_twice_is_placed_once(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """A duplicate entry in RomM's list is not copied twice or reported twice."""
        source = _source(library, "dc_boot.bin", BOOT)

        report = firmware.place(dropper, [source, source])

        assert report["placed"] == [str(dropper.drop_dir / "dc_boot.bin")]
        assert report["failed"] == []

    def test_a_source_without_an_md5_is_hashed_and_placed(
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """RomM not having hashed a file yet costs a read, not the placement."""
        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT, md5=None)])

        dest = dropper.drop_dir / "dc_boot.bin"
        assert report["placed"] == [str(dest)]
        assert json.loads(record.read_text())[str(dest)]["md5"] == _md5(BOOT)

    def test_an_empty_source_is_placed(self, dropper: _Dropper, library: Path, record: Path) -> None:
        """A zero-byte upload is copied like any other; the emulator decides what it is worth."""
        report = firmware.place(dropper, [_source(library, "empty.bin", b"")])

        assert report["placed"] == [str(dropper.drop_dir / "empty.bin")]
        assert (dropper.drop_dir / "empty.bin").read_bytes() == b""

    def test_a_source_that_is_a_folder_fails(self, dropper: _Dropper, library: Path, record: Path) -> None:
        """A folder where RomM says a file is fails that entry alone."""
        (library / "dc_boot.bin").mkdir()
        source = firmware.Source(
            name="dc_boot.bin", path=library / "dc_boot.bin", md5=None, sha1=None, size=None
        )

        report = firmware.place(dropper, [source, _source(library, "dc_flash.bin", FLASH)])

        assert report["failed"] == ["dc_boot.bin"]
        assert report["placed"] == [str(dropper.drop_dir / "dc_flash.bin")]

    @pytest.mark.parametrize("name", ["scph 1001 (USA).bin", "bios[1].bin", "bïos*.bin", ".hidden.bin"])
    def test_awkward_but_legal_names_are_placed(
        self,
        dropper: _Dropper,
        library: Path,
        record: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
    ) -> None:
        """Spaces, brackets, glob characters and non-ASCII names copy like any other."""
        dropper.drop_dir.mkdir()
        leftover = dropper.drop_dir / f".{name}.broker-fw-leftover.tmp"
        leftover.write_bytes(b"stale")
        _sweep_now(monkeypatch)

        report = firmware.place(dropper, [_source(library, name, BOOT)])

        assert report["placed"] == [str(dropper.drop_dir / name)]
        assert sorted(p.name for p in dropper.drop_dir.iterdir()) == [name]

    def test_a_record_folder_that_does_not_exist_yet_is_made(
        self, dropper: _Dropper, library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh /config without the record's folder gets one on first placement."""
        path = tmp_path / "fresh" / "config" / "broker-firmware.json"
        monkeypatch.setattr(settings, "FIRMWARE_RECORD", path)

        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert str(dropper.drop_dir / "dc_boot.bin") in json.loads(path.read_text())


def _activate(client: TestClient, broker_dirs: dict[str, Path], **overrides: object) -> Any:
    """Post an activate for the fake emulator with a ROM in the library.

    Args:
        client: The test client.
        broker_dirs: The redirected ROM root and archive directories.
        **overrides: Body fields that replace the defaults.

    Returns:
        The response.
    """
    rom = broker_dirs["roms"] / "Game.iso"
    rom.write_bytes(b"iso")
    body: dict[str, Any] = {
        "session_id": "sess-1",
        "emulator": "fake",
        "rom": {"id": 5, "name": "Game", "platform": "dc", "path": str(rom)},
    }
    body.update(overrides)
    return client.post(f"{API}/session/activate", json=body)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30", 30.0),
        ("inf", 120.0),
        ("1e400", 120.0),
        ("86400", 120.0),
        ("86399", 86399.0),
        ("nan", 120.0),
        ("0", 120.0),
        ("-5", 120.0),
        ("x", 120.0),
    ],
)
def test_the_budget_ignores_values_that_would_turn_it_off(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    """`inf` or a huge number would disable the deadline and activate's wait; both fall back."""
    monkeypatch.setenv("FIRMWARE_PLACE_BUDGET", raw)

    assert settings._positive_seconds("FIRMWARE_PLACE_BUDGET", 120.0) == expected


class TestActivate:
    """RomM's firmware list on the activate request."""

    @pytest.fixture
    def drop(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record: Path) -> Path:
        """Make the fake emulator drop every file into one folder.

        Args:
            monkeypatch: Pytest's attribute patcher, undone when the test ends.
            tmp_path: The per-test temporary directory.
            record: The redirected ownership record.

        Returns:
            The folder.
        """
        folder = tmp_path / "fake-bios"
        monkeypatch.setattr(
            FakeEmulator,
            "firmware_targets",
            lambda self, s: [firmware.Target(folder, PurePosixPath(s.name))],
            raising=False,
        )
        return folder

    def _entry(self, broker_dirs: dict[str, Path], name: str, data: bytes) -> dict[str, Any]:
        """Write a firmware file into the library and describe it as RomM sends it.

        Args:
            broker_dirs: The redirected ROM root and archive directories.
            name: The file name.
            data: Its bytes.

        Returns:
            The payload entry.
        """
        folder = broker_dirs["roms"] / "library" / "bios" / "dc"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        return {"file_name": name, "path": str(folder / name), "size": len(data), "md5": _md5(data)}

    def test_firmware_is_placed_before_launch_and_reported(
        self, client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], drop: Path
    ) -> None:
        """Each file is in place by launch, and the response says where it went."""
        seen_at_launch: list[bool] = []
        real_launch = FakeEmulator.launch

        def launch(self: FakeEmulator, rom_path: Optional[Path], resume_slot: Optional[int]) -> None:
            seen_at_launch.append((drop / "dc_boot.bin").is_file())
            real_launch(self, rom_path, resume_slot)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FakeEmulator, "launch", launch)
            response = _activate(
                client, broker_dirs, firmware=[self._entry(broker_dirs, "dc_boot.bin", BOOT)]
            )

        assert response.status_code == 200
        assert seen_at_launch == [True]
        assert (drop / "dc_boot.bin").read_bytes() == BOOT
        assert response.json()["firmware"]["placed"] == [str(drop / "dc_boot.bin")]

    def test_a_placement_that_hangs_does_not_hold_up_the_launch(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A read stuck on the library mount costs the firmware, not the launch, and is told to stop."""
        released = threading.Event()
        stops: list[Optional[threading.Event]] = []

        set_while_blocked = [False]

        def hang(
            emulator: Any,
            sources: Any,
            stop: Optional[threading.Event] = None,
            budget: Optional[float] = None,
        ) -> dict[str, list[str]]:
            stops.append(stop)
            set_while_blocked[0] = stop is not None and stop.wait(5)
            released.wait(5)
            return firmware.empty_report()

        monkeypatch.setattr(firmware, "place", hang)
        monkeypatch.setattr(settings, "FIRMWARE_PLACE_BUDGET", 0.05)
        monkeypatch.setattr(api, "_FIRMWARE_GRACE_S", 0.05)
        try:
            response = _activate(
                client, broker_dirs, firmware=[self._entry(broker_dirs, "dc_boot.bin", BOOT)]
            )
        finally:
            released.set()

        assert response.status_code == 200
        assert response.json()["firmware"]["failed"] == ["dc_boot.bin"]
        assert len(fake_emulator) == 1
        # Set by the timeout, while the call was still blocked, not only by the cleanup after it.
        assert stops and stops[0] is not None and stops[0].is_set()
        assert set_while_blocked[0]

    def test_checking_the_list_is_charged_to_the_placement_budget(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Checking and placing share one budget, so a slow check leaves less time to copy."""
        budgets: list[Optional[float]] = []
        real_sources = api._firmware_sources

        def slow_sources(entries: Any) -> tuple[list[firmware.Source], list[str]]:
            time.sleep(0.3)
            return real_sources(entries)

        def spy(
            emulator: Any,
            sources: Any,
            stop: Optional[threading.Event] = None,
            budget: Optional[float] = None,
        ) -> dict[str, list[str]]:
            budgets.append(budget)
            return firmware.empty_report()

        monkeypatch.setattr(api, "_firmware_sources", slow_sources)
        monkeypatch.setattr(firmware, "place", spy)
        monkeypatch.setattr(settings, "FIRMWARE_PLACE_BUDGET", 5.0)

        response = _activate(client, broker_dirs, firmware=[self._entry(broker_dirs, "dc_boot.bin", BOOT)])

        assert response.status_code == 200
        assert budgets and budgets[0] is not None and 3.0 < budgets[0] <= 4.75

    def test_a_refused_name_is_listed_as_failed_once(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
    ) -> None:
        """Two bad entries with one file name count as one failed file, as `place` counts them."""
        bad = {"file_name": "dc_boot.bin", "path": "../outside/dc_boot.bin"}

        response = _activate(client, broker_dirs, firmware=[bad, dict(bad)])

        assert response.status_code == 200
        assert response.json()["firmware"]["failed"] == ["dc_boot.bin"]

    def test_a_firmware_list_check_that_hangs_does_not_hold_up_the_launch(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Resolving the paths touches the library mount too, so it is bounded like the copies."""
        released = threading.Event()

        def hang(entries: Any) -> tuple[list[firmware.Source], list[str]]:
            released.wait(5)
            return [], []

        monkeypatch.setattr(api, "_firmware_sources", hang)
        monkeypatch.setattr(settings, "FIRMWARE_PLACE_BUDGET", 0.05)
        monkeypatch.setattr(api, "_FIRMWARE_GRACE_S", 0.05)
        try:
            response = _activate(
                client, broker_dirs, firmware=[self._entry(broker_dirs, "dc_boot.bin", BOOT), {"x": 1}]
            )
        finally:
            released.set()

        assert response.status_code == 200
        assert response.json()["firmware"]["failed"] == ["dc_boot.bin", "entry 1"]
        assert len(fake_emulator) == 1

    def test_a_firmware_path_outside_the_rom_root_is_skipped_and_the_rest_still_placed(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        tmp_path: Path,
    ) -> None:
        """One path outside ROM_ROOT is never read and fails alone; the launch goes ahead."""
        good = self._entry(broker_dirs, "dc_boot.bin", BOOT)
        (tmp_path / "evil.bin").write_bytes(b"x")
        evil = {"file_name": "evil.bin", "path": str(tmp_path / "evil.bin")}

        response = _activate(client, broker_dirs, firmware=[good, evil])

        assert response.status_code == 200
        assert fake_emulator[0].launched is not None
        assert sorted(p.name for p in drop.iterdir()) == ["dc_boot.bin"]
        report = response.json()["firmware"]
        assert report["placed"] == [str(drop / "dc_boot.bin")]
        assert report["failed"] == ["evil.bin"]

    def test_a_crash_report_still_lists_the_skipped_entries(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A placement crash and an out-of-root entry both end up under `failed`."""

        def boom(*_args: Any) -> dict[str, list[str]]:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(firmware, "place", boom)
        (tmp_path / "evil.bin").write_bytes(b"x")
        evil = {"file_name": "evil.bin", "path": str(tmp_path / "evil.bin")}

        response = _activate(
            client, broker_dirs, firmware=[self._entry(broker_dirs, "dc_boot.bin", BOOT), evil]
        )

        assert response.status_code == 200
        assert response.json()["firmware"]["failed"] == ["dc_boot.bin", "evil.bin"]

    def test_an_activate_without_firmware_is_unchanged(
        self, client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], drop: Path
    ) -> None:
        """An older RomM sends no list; nothing is placed and the launch goes ahead."""
        response = _activate(client, broker_dirs)

        assert response.status_code == 200
        assert response.json()["firmware"] is None
        assert not drop.exists()

    def test_sync_turned_off_places_nothing(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`FIRMWARE_SYNC_ENABLED` off ignores the list."""
        monkeypatch.setattr(settings, "FIRMWARE_SYNC_ENABLED", False)

        response = _activate(client, broker_dirs, firmware=[self._entry(broker_dirs, "dc_boot.bin", BOOT)])

        assert response.status_code == 200
        assert response.json()["firmware"] is None
        assert not drop.exists()

    def test_a_placement_that_crashes_still_launches(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The emulator's own missing-BIOS error stays the signal, not a refused launch."""

        def boom(*_args: Any) -> dict[str, list[str]]:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(firmware, "place", boom)

        response = _activate(client, broker_dirs, firmware=[self._entry(broker_dirs, "dc_boot.bin", BOOT)])

        assert response.status_code == 200
        assert fake_emulator[0].launched is not None
        assert response.json()["firmware"] == {
            "placed": [],
            "unchanged": [],
            "kept": [],
            "skipped": [],
            "failed": ["dc_boot.bin"],
        }

    def test_an_empty_list_places_nothing_and_says_so(
        self, client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], drop: Path
    ) -> None:
        """A platform with no firmware in RomM gets an empty report, not None."""
        response = _activate(client, broker_dirs, firmware=[])

        assert response.status_code == 200
        assert response.json()["firmware"] == firmware.empty_report()

    @pytest.mark.parametrize("name", ["../dc_boot.bin", "", "sub/dc_boot.bin", ".."])
    def test_a_file_name_that_is_not_one_name_is_skipped(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        name: str,
    ) -> None:
        """A name the hooks would join into a path never gets that far, and the game still launches."""
        entry = self._entry(broker_dirs, "dc_boot.bin", BOOT)
        entry["file_name"] = name

        response = _activate(client, broker_dirs, firmware=[entry])

        assert response.status_code == 200
        assert fake_emulator[0].launched is not None
        assert not drop.exists()
        assert response.json()["firmware"]["failed"] == [name]

    def test_a_path_that_links_out_of_the_rom_root_is_skipped(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        tmp_path: Path,
    ) -> None:
        """A symlink in the library pointing elsewhere is judged by where it leads."""
        (tmp_path / "secret.bin").write_bytes(b"x")
        entry = self._entry(broker_dirs, "dc_boot.bin", BOOT)
        link = Path(entry["path"]).with_name("link.bin")
        link.symlink_to(tmp_path / "secret.bin")
        entry.update(file_name="link.bin", path=str(link))

        response = _activate(client, broker_dirs, firmware=[entry])

        assert response.status_code == 200
        assert not drop.exists()
        assert response.json()["firmware"]["failed"] == ["link.bin"]

    def test_a_garbled_hash_is_ignored_not_refused(
        self, client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], drop: Path
    ) -> None:
        """A hash RomM got wrong in shape falls back to reading the file."""
        entry = self._entry(broker_dirs, "dc_boot.bin", BOOT)
        entry.update(md5="not-a-hash", sha1="ZZ")

        response = _activate(client, broker_dirs, firmware=[entry])

        assert response.status_code == 200
        assert (drop / "dc_boot.bin").read_bytes() == BOOT

    @pytest.mark.parametrize(
        ("bad", "label"),
        [
            ({"file_name": "dc_flash.bin"}, "dc_flash.bin"),
            ({"file_name": "dc_flash.bin", "path": None}, "dc_flash.bin"),
            ({"file_name": "dc_flash.bin", "path": "/x", "size": "big"}, "dc_flash.bin"),
            ({"path": "/x"}, "entry 1"),
            ({"file_name": 7, "path": "/x"}, "entry 1"),
            ("dc_flash.bin", "entry 1"),
            (None, "entry 1"),
        ],
    )
    def test_a_malformed_entry_is_skipped_and_the_rest_still_placed(
        self,
        client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        drop: Path,
        bad: object,
        label: str,
    ) -> None:
        """One entry RomM got wrong in shape never refuses the launch or the other files."""
        good = self._entry(broker_dirs, "dc_boot.bin", BOOT)

        response = _activate(client, broker_dirs, firmware=[good, bad])

        assert response.status_code == 200
        assert fake_emulator[0].launched is not None
        assert (drop / "dc_boot.bin").read_bytes() == BOOT
        assert response.json()["firmware"]["failed"] == [label]

    def test_a_firmware_field_that_is_not_a_list_still_launches(
        self, client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], drop: Path
    ) -> None:
        """Even a value that is wrong at the top level only costs the firmware."""
        response = _activate(client, broker_dirs, firmware={"file_name": "dc_boot.bin"})

        assert response.status_code == 200
        assert fake_emulator[0].launched is not None
        assert response.json()["firmware"]["failed"] == ["firmware"]

    def test_an_emulator_that_places_nothing_reports_no_failures(
        self, client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
    ) -> None:
        """An emulator without firmware sync never reads RomM's list or calls a bad entry failed."""
        response = _activate(client, broker_dirs, firmware=[{"file_name": "x.bin", "path": "/outside/x.bin"}])

        assert response.status_code == 200
        assert response.json()["firmware"] == firmware.empty_report()

    def test_a_firmware_path_that_loops_is_skipped(
        self, client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], drop: Path
    ) -> None:
        """A symlink loop under the library is one failed entry, not a 500."""
        loop = broker_dirs["roms"] / "loop.bin"
        loop.symlink_to(loop)

        response = _activate(client, broker_dirs, firmware=[{"file_name": "loop.bin", "path": str(loop)}])

        assert response.status_code == 200
        assert fake_emulator[0].launched is not None
        assert response.json()["firmware"]["failed"] == ["loop.bin"]
