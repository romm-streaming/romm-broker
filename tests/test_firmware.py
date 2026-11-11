"""Placing the firmware RomM holds for a platform where the launched emulator looks for it.

Covers the core-info name tables, each emulator's targets, the shared copy
with its overwrite rules, and the activate wiring.
"""

import hashlib
import io
import json
import logging
import os
import time
import zipfile
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from webstation_broker import firmware, settings
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
        self, dropper: _Dropper, library: Path, record: Path
    ) -> None:
        """The next copy of the same file clears what a killed broker left behind."""
        dropper.drop_dir.mkdir()
        stale = dropper.drop_dir / ".dc_boot.bin.abc123.tmp"
        stale.write_bytes(b"half a bios")

        firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert sorted(p.name for p in dropper.drop_dir.iterdir()) == ["dc_boot.bin"]

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

        with caplog.at_level(logging.WARNING, logger="webstation_broker.firmware"):
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

    def test_an_interrupted_copy_leaves_nothing_behind(
        self, dropper: _Dropper, library: Path, record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copy that dies midway leaves neither a torn target nor a temp file."""
        dropper.drop_dir.mkdir()

        def boom(*_args: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(firmware.os, "replace", boom)

        report = firmware.place(dropper, [_source(library, "dc_boot.bin", BOOT)])

        assert report["failed"] == ["dc_boot.bin"]
        assert list(dropper.drop_dir.iterdir()) == []

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

        assert report == {"placed": [], "unchanged": [], "kept": [], "failed": []}


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
        self, dropper: _Dropper, library: Path, record: Path, name: str
    ) -> None:
        """Spaces, brackets, glob characters and non-ASCII names copy like any other."""
        dropper.drop_dir.mkdir()
        (dropper.drop_dir / f".{name}.leftover.tmp").write_bytes(b"stale")

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
        response = _activate(client, broker_dirs, firmware=[self._entry(broker_dirs, "dc_boot.bin", BOOT)])

        assert response.status_code == 200
        assert (drop / "dc_boot.bin").read_bytes() == BOOT
        assert response.json()["firmware"]["placed"] == [str(drop / "dc_boot.bin")]
        assert fake_emulator[0].launched is not None

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

    def test_an_entry_without_a_path_is_a_422(
        self, client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], drop: Path
    ) -> None:
        """A malformed list is rejected by the schema before anything runs."""
        response = _activate(client, broker_dirs, firmware=[{"file_name": "dc_boot.bin"}])

        assert response.status_code == 422
        assert fake_emulator == []
