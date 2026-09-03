"""shadPS4 ROM resolution, binary version selection, launch, and IPC-driven stop."""

import json
import os
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, Optional

import pytest

from webstation_broker import settings
from webstation_broker.emulators import base, shadps4


@pytest.fixture(autouse=True)
def _isolated_gpu_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every test's gpu_id pin off the real filesystem and detection cache.

    Points SHADPS4_CONFIG_PATH at a file that does not exist, so a test that
    exercises launch() without caring about the pin never touches the real
    host's config.json or shells out to vulkaninfo. A successful detection
    is memoized process-wide and failures are counted toward a retry cap, so
    both are reset before every test too.
    """
    monkeypatch.setattr(shadps4, "SHADPS4_CONFIG_PATH", tmp_path / "unused-config.json")
    monkeypatch.setattr(shadps4, "_DETECTED_GPU_ID", None)
    monkeypatch.setattr(shadps4, "_GPU_DETECT_ATTEMPTS", 0)


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the ROM library root at a fresh temporary directory."""
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(settings, "ROM_ROOT", root)
    return root


def test_resolve_refuses_a_rom_file_that_symlinks_out_of_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """A ROM file that resolves outside the ROM root is refused.

    The file branch used to hand the path straight to shadPS4 and, for a
    .pkg, to pkg_extractor, on the word of whichever caller passed it in.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"not a game")
    linked = rom_root / "Game.bin"
    linked.symlink_to(secret)

    assert shadps4.Shadps4().resolve_rom_file(linked) is None


def test_resolve_refuses_a_pkg_that_symlinks_out_of_the_rom_root(
    rom_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .pkg pointing outside the ROM root is refused even with the cache on.

    With caching enabled the path goes on to pkg_extractor, so containment has
    to be decided before the format is.
    """
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.pkg"
    secret.write_bytes(b"not a game")
    linked = rom_root / "Game.pkg"
    linked.symlink_to(secret)

    assert shadps4.Shadps4().resolve_rom_file(linked) is None


def test_resolve_accepts_a_rom_file_that_symlinks_inside_the_rom_root(rom_root: Path) -> None:
    """A ROM file symlinked to another path inside the ROM root still boots."""
    shared = rom_root / "SharedAssets"
    shared.mkdir()
    real = shared / "eboot.bin"
    real.write_bytes(b"game")
    linked = rom_root / "Game.bin"
    linked.symlink_to(real)

    assert shadps4.Shadps4().resolve_rom_file(linked) == linked


def test_resolve_refuses_an_eboot_that_symlinks_out_of_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """Resolution rejects an eboot symlink pointing outside the ROM root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(secret)

    assert shadps4.Shadps4().resolve_rom_file(folder) is None


def test_resolve_accepts_an_eboot_that_symlinks_inside_the_rom_root(rom_root: Path) -> None:
    """Resolution accepts an eboot symlink that stays inside the ROM root."""
    shared = rom_root / "SharedAssets"
    shared.mkdir()
    real_eboot = shared / "actual_eboot.bin"
    real_eboot.write_bytes(b"game data")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(real_eboot)

    assert shadps4.Shadps4().resolve_rom_file(folder) == folder / "eboot.bin"


def test_resolve_refuses_a_dangling_eboot_symlink(rom_root: Path) -> None:
    """Resolution rejects an eboot symlink whose target does not exist."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(rom_root / "does-not-exist")

    assert shadps4.Shadps4().resolve_rom_file(folder) is None


def test_resolve_refuses_an_eboot_that_symlinks_to_a_non_regular_file_outside_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """Resolution rejects an eboot symlink to a non-regular file outside the ROM root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.mkfifo(outside / "pipe")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(outside / "pipe")

    assert shadps4.Shadps4().resolve_rom_file(folder) is None


def test_resolve_refuses_a_game_folder_that_symlinks_out_of_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """A folder symlinked out of the library is refused even when it holds no eboot.

    shadps4 appends eboot.bin to a directory path itself, so returning the bare folder would
    hand it a host path nothing validated.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    link = rom_root / "MyGame"
    link.symlink_to(outside, target_is_directory=True)

    assert shadps4.Shadps4().resolve_rom_file(link) is None


def test_resolve_accepts_a_game_folder_that_symlinks_inside_the_rom_root(rom_root: Path) -> None:
    """A folder symlink that stays inside the library still resolves to the bare folder."""
    real = rom_root / "Library" / "MyGame"
    real.mkdir(parents=True)
    link = rom_root / "MyGame"
    link.symlink_to(real, target_is_directory=True)

    assert shadps4.Shadps4().resolve_rom_file(link) == link


def test_resolve_takes_a_direct_file_as_given(rom_root: Path) -> None:
    """Resolution returns a direct ROM file path unchanged."""
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(rom) == rom


def test_resolve_finds_eboot_inside_a_game_folder(rom_root: Path) -> None:
    """Resolution finds the eboot file inside a game folder."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    eboot = folder / "eboot.bin"
    eboot.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(folder) == eboot


def test_resolve_falls_back_to_the_bare_folder_when_there_is_no_eboot(rom_root: Path) -> None:
    """Resolution falls back to the bare game folder when no eboot file exists."""
    folder = rom_root / "MyGame"
    folder.mkdir()

    assert shadps4.Shadps4().resolve_rom_file(folder) == folder


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(
    rom_root: Path,
) -> None:
    """Resolution returns nothing for a path that is neither a file nor a folder."""
    missing = rom_root / "nope"

    assert shadps4.Shadps4().resolve_rom_file(missing) is None


@pytest.mark.parametrize("ext", [".pkg", ".7z", ".zip", ".rar"])
def test_resolve_rejects_pkg_and_archives_when_the_cache_is_disabled(
    rom_root: Path, monkeypatch: pytest.MonkeyPatch, ext: str
) -> None:
    """A .pkg or archive ROM is refused when the extraction cache is disabled.

    Without the cache, an extraction would just be discarded on every launch,
    so only natively bootable formats should resolve at all.
    """
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", False)
    rom = rom_root / f"game{ext}"
    rom.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(rom) is None


@pytest.mark.parametrize("ext", [".pkg", ".7z", ".zip", ".rar"])
def test_resolve_accepts_pkg_and_archives_when_the_cache_is_enabled(
    rom_root: Path, monkeypatch: pytest.MonkeyPatch, ext: str
) -> None:
    """A .pkg or archive ROM resolves normally once the extraction cache is enabled."""
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    rom = rom_root / f"game{ext}"
    rom.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(rom) == rom


@pytest.fixture
def versions_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point shadps4's VERSIONS_DIR at a fresh temporary directory."""
    d = tmp_path / "versions"
    d.mkdir()
    monkeypatch.setattr(shadps4, "VERSIONS_DIR", d)
    monkeypatch.delenv("SHADPS4_BIN", raising=False)
    return d


def _make_release(root: Path, folder_name: str, bin_name: str = "Shadps4-sdl.AppImage") -> Path:
    folder = root / folder_name
    folder.mkdir(parents=True)
    binary = folder / bin_name
    binary.write_bytes(b"")
    return binary


def test_an_explicit_override_wins_over_everything(
    versions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SHADPS4_BIN override wins over any discovered release folder."""
    _make_release(versions_dir, "v0.17.0 - Garbage Collector's Edition")
    monkeypatch.setenv("SHADPS4_BIN", "/opt/custom/shadps4")

    assert shadps4._resolve_binary() == Path("/opt/custom/shadps4")


def test_the_pre_release_folder_beats_every_numbered_release(versions_dir: Path) -> None:
    """The Pre-release folder is preferred over any numbered release."""
    _make_release(versions_dir, "v99.0.0 - Newest Looking Number")
    pre = _make_release(versions_dir, "Pre-release")

    assert shadps4._resolve_binary() == pre


def test_the_highest_semver_release_wins_by_number_not_by_string(versions_dir: Path) -> None:
    """Release selection compares versions numerically, not lexicographically."""
    _make_release(versions_dir, "v0.9.9 - A")
    newest = _make_release(versions_dir, "v0.9.10 - B")

    assert shadps4._resolve_binary() == newest


def test_release_folder_precedence_across_more_than_two_versions(versions_dir: Path) -> None:
    """Release selection picks the highest version among more than two candidates."""
    _make_release(versions_dir, "v0.9.9 - A")
    _make_release(versions_dir, "v0.9.10 - B")
    newest = _make_release(
        versions_dir, "v0.17.0 - Garbage Collector's Edition - 2026-07-30"
    )

    assert shadps4._resolve_binary() == newest


def test_a_folder_whose_name_does_not_parse_as_a_version_is_skipped_not_fatal(
    versions_dir: Path,
) -> None:
    """A folder name that fails to parse as a version is skipped, not fatal."""
    (versions_dir / "notes").mkdir()
    (versions_dir / "notes" / "Shadps4-sdl.AppImage").write_bytes(b"")
    newest = _make_release(versions_dir, "v0.5.0 - Only Real Release")

    assert shadps4._resolve_binary() == newest


def test_a_missing_versions_dir_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Binary resolution returns nothing when the versions directory is absent."""
    monkeypatch.setattr(shadps4, "VERSIONS_DIR", tmp_path / "does-not-exist")
    monkeypatch.delenv("SHADPS4_BIN", raising=False)

    assert shadps4._resolve_binary() is None


def test_a_versions_dir_with_no_usable_binary_resolves_to_nothing(versions_dir: Path) -> None:
    """Binary resolution returns nothing when no release folder has a binary."""
    (versions_dir / "v0.1.0 - Empty").mkdir()

    assert shadps4._resolve_binary() is None


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gpu_id pin at a throwaway config, defaulted to device 0.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The path of the config the pin will edit; the file does not exist yet.
    """
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(shadps4, "SHADPS4_CONFIG_PATH", cfg)
    monkeypatch.setattr(shadps4, "SHADPS4_GPU_ID", "0")
    return cfg


def test_gpu_id_is_pinned_into_an_existing_config(pinned: Path) -> None:
    """An auto-select gpu_id of -1 is pinned to the configured device index."""
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1, "vkvalidation_enabled": False}}))
    shadps4._pin_gpu_id()
    cfg = json.loads(pinned.read_text())
    assert cfg["Vulkan"]["gpu_id"] == 0


def test_pinning_leaves_the_rest_of_the_config_alone(pinned: Path) -> None:
    """Pinning rewrites only Vulkan.gpu_id; every other key is untouched."""
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}, "General": {"volume_slider": 100}}))
    shadps4._pin_gpu_id()
    cfg = json.loads(pinned.read_text())
    assert cfg["General"] == {"volume_slider": 100}


def test_a_config_with_no_vulkan_section_gains_one(pinned: Path) -> None:
    """A config with no Vulkan table gains one carrying just the pinned gpu_id."""
    pinned.write_text(json.dumps({"General": {"volume_slider": 100}}))
    shadps4._pin_gpu_id()
    cfg = json.loads(pinned.read_text())
    assert cfg["Vulkan"] == {"gpu_id": 0}


def test_the_gpu_id_is_configurable_for_hosts_where_auto_select_works(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned gpu_id follows SHADPS4_GPU_ID, so another device can be chosen."""
    monkeypatch.setattr(shadps4, "SHADPS4_GPU_ID", "1")
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}}))
    shadps4._pin_gpu_id()
    assert json.loads(pinned.read_text())["Vulkan"]["gpu_id"] == 1


@pytest.mark.parametrize("setting", ["KEEP", "", "-1"])
def test_the_gpu_id_pin_can_be_turned_off(
    pinned: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    """Turning the pin off leaves an existing gpu_id alone."""
    monkeypatch.setattr(shadps4, "SHADPS4_GPU_ID", setting)
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}}))
    shadps4._pin_gpu_id()
    assert json.loads(pinned.read_text())["Vulkan"]["gpu_id"] == -1


def test_a_missing_config_is_not_created(pinned: Path) -> None:
    """A missing config is left for shadPS4 to create on its own first launch."""
    shadps4._pin_gpu_id()
    assert not pinned.exists()


def test_an_unparseable_config_is_left_alone(pinned: Path) -> None:
    """Invalid JSON is not overwritten; the pin logs and backs off instead of guessing."""
    pinned.write_text("not json")
    shadps4._pin_gpu_id()
    assert pinned.read_text() == "not json"


def test_a_config_that_is_not_a_json_object_is_left_alone(pinned: Path) -> None:
    """A top-level JSON array (or any non-object) is not overwritten, just backed off."""
    pinned.write_text(json.dumps(["not", "an", "object"]))
    shadps4._pin_gpu_id()
    assert json.loads(pinned.read_text()) == ["not", "an", "object"]


def test_a_non_object_vulkan_section_is_left_alone(pinned: Path) -> None:
    """A Vulkan key that isn't itself an object is not overwritten, just backed off."""
    pinned.write_text(json.dumps({"Vulkan": None}))
    shadps4._pin_gpu_id()
    assert json.loads(pinned.read_text()) == {"Vulkan": None}


def test_an_unwritable_config_is_left_as_it_was(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config that cannot be written is left intact and the pin backs off without raising."""
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}}))

    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("read-only file system")

    monkeypatch.setattr(shadps4.tempfile, "mkstemp", fail)
    shadps4._pin_gpu_id()
    assert json.loads(pinned.read_text())["Vulkan"]["gpu_id"] == -1


def test_the_config_is_swapped_in_and_leaves_no_scratch_file(pinned: Path) -> None:
    """The pin renames its replacement into place and leaves nothing beside it.

    A truncating write that dies half way costs shadPS4 every setting in the
    file, and a fixed `.tmp` sibling lets two pins write the same scratch path
    and rename half of each other's config in.
    """
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}}))
    shadps4._pin_gpu_id()

    assert json.loads(pinned.read_text())["Vulkan"]["gpu_id"] == 0
    assert [p.name for p in pinned.parent.iterdir()] == [pinned.name]


def test_two_concurrent_pins_leave_a_readable_config(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins racing each other still leave one whole config behind."""
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}, "General": {"volume": 50}}))
    real_replace = os.replace

    def slow_replace(src: object, dst: object) -> None:
        time.sleep(0.05)
        real_replace(src, dst)

    monkeypatch.setattr(shadps4.os, "replace", slow_replace)
    threads = [threading.Thread(target=shadps4._pin_gpu_id) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    cfg = json.loads(pinned.read_text())
    assert cfg["Vulkan"]["gpu_id"] == 0
    assert cfg["General"]["volume"] == 50


def test_the_config_keeps_its_permissions_across_a_pin(pinned: Path) -> None:
    """The replacement config carries the mode the original had.

    mkstemp creates its file owner-only, which would lock shadPS4 out of a
    config it shares.
    """
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}}))
    os.chmod(pinned, 0o640)
    shadps4._pin_gpu_id()

    assert pinned.stat().st_mode & 0o777 == 0o640


def test_an_invalid_gpu_id_env_value_is_left_alone(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-integer SHADPS4_GPU_ID is ignored rather than crashing the launch."""
    monkeypatch.setattr(shadps4, "SHADPS4_GPU_ID", "not-a-number")
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}}))
    shadps4._pin_gpu_id()
    assert json.loads(pinned.read_text())["Vulkan"]["gpu_id"] == -1


def _fake_vulkaninfo(returncode: int = 0, stdout: str = "", stderr: str = "") -> object:
    """A stand-in for subprocess.run's result, shaped like _detect_gpu_id needs."""
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


_AMD_RENOIR_SUMMARY = """\
Devices:
========
GPU0:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
\tdeviceName         = AMD Radeon Graphics (RADV RENOIR)
\tdriverName         = radv
GPU1:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 21.1.8, 256 bits)
\tdriverName         = llvmpipe
"""
"""Trimmed real `vulkaninfo --summary` output captured on the AMD Renoir/RADV
hardware the black-screen bug was diagnosed on: index 0 is the real GPU."""

_MIXED_VENDOR_SUMMARY = """\
Devices:
========
GPU0:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
\tdeviceName         = Intel(R) UHD Graphics
\tdriverName         = intel
GPU1:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA GeForce RTX 4070
\tdriverName         = nvidia
GPU2:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 21.1.8, 256 bits)
\tdriverName         = llvmpipe
"""
"""A synthetic multi-vendor listing: proves the discrete NVIDIA card at
index 1 wins over the Intel iGPU at index 0 on deviceType alone, with no
vendor name anywhere in the selection logic."""

_CPU_ONLY_SUMMARY = """\
Devices:
========
GPU0:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 21.1.8, 256 bits)
\tdriverName         = llvmpipe
"""


def test_detect_gpu_id_matches_our_actual_amd_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection picks the real RADV device over llvmpipe on the hardware this was diagnosed on."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_vulkaninfo(stdout=_AMD_RENOIR_SUMMARY))
    assert shadps4._detect_gpu_id() == 0


def test_detect_gpu_id_prefers_a_discrete_gpu_over_an_earlier_integrated_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later discrete GPU outranks an earlier integrated one, purely by deviceType."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_vulkaninfo(stdout=_MIXED_VENDOR_SUMMARY))
    assert shadps4._detect_gpu_id() == 1


def test_detect_gpu_id_returns_none_when_only_cpu_devices_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with no real GPU (only llvmpipe) yields no pin, not a bad index."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_vulkaninfo(stdout=_CPU_ONLY_SUMMARY))
    assert shadps4._detect_gpu_id() is None


def test_detect_gpu_id_returns_none_when_vulkaninfo_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing vulkaninfo binary is handled, not raised."""
    def raise_oserror(*a: object, **k: object) -> NoReturn:
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert shadps4._detect_gpu_id() is None


def test_detect_gpu_id_returns_none_on_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vulkaninfo failure (bad ICD, broken driver, ...) is handled, not raised."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_vulkaninfo(returncode=1, stderr="boom"))
    assert shadps4._detect_gpu_id() is None


def test_detect_gpu_id_is_only_run_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection is cached: a second call does not shell out again."""
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (calls.append(1), _fake_vulkaninfo(stdout=_AMD_RENOIR_SUMMARY))[1],
    )
    assert shadps4._detect_gpu_id() == 0
    assert shadps4._detect_gpu_id() == 0
    assert len(calls) == 1


def test_detect_gpu_id_retries_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed probe is not memoized, so a later launch tries again.

    vulkaninfo can fail while the container is still coming up; caching that
    would leave shadPS4 on its black-screen auto-select for the broker's
    whole lifetime.
    """
    results = [_fake_vulkaninfo(returncode=1, stderr="boom"),
               _fake_vulkaninfo(stdout=_AMD_RENOIR_SUMMARY)]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: results.pop(0))

    assert shadps4._detect_gpu_id() is None
    assert shadps4._detect_gpu_id() == 0


def test_detect_gpu_id_stops_retrying_after_the_attempt_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection gives up once the cap is reached, so a hung vulkaninfo is not run forever.

    A probe that times out costs a full `_VULKANINFO_TIMEOUT`, and paying
    that on every launch for the process lifetime is worse than losing the
    pin.
    """
    probes = 0

    def fail() -> Optional[int]:
        nonlocal probes
        probes += 1
        return None

    monkeypatch.setattr(shadps4, "_probe_gpu_id", fail)

    for _ in range(shadps4._MAX_GPU_DETECT_ATTEMPTS + 2):
        assert shadps4._detect_gpu_id() is None

    assert probes == shadps4._MAX_GPU_DETECT_ATTEMPTS


def test_pin_gpu_id_auto_uses_the_detected_index(pinned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SHADPS4_GPU_ID=auto pins whatever _detect_gpu_id finds."""
    monkeypatch.setattr(shadps4, "SHADPS4_GPU_ID", "auto")
    monkeypatch.setattr(shadps4, "_detect_gpu_id", lambda: 1)
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}}))
    shadps4._pin_gpu_id()
    assert json.loads(pinned.read_text())["Vulkan"]["gpu_id"] == 1


def test_pin_gpu_id_auto_leaves_config_alone_when_detection_fails(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SHADPS4_GPU_ID=auto with no detectable GPU leaves the existing gpu_id as-is."""
    monkeypatch.setattr(shadps4, "SHADPS4_GPU_ID", "auto")
    monkeypatch.setattr(shadps4, "_detect_gpu_id", lambda: None)
    pinned.write_text(json.dumps({"Vulkan": {"gpu_id": -1}}))
    shadps4._pin_gpu_id()
    assert json.loads(pinned.read_text())["Vulkan"]["gpu_id"] == -1


def test_pin_gpu_id_auto_does_not_detect_without_a_config_to_write(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config file means nothing to pin, so detection (the vulkaninfo subprocess) never runs."""
    monkeypatch.setattr(shadps4, "SHADPS4_GPU_ID", "auto")

    def fail() -> NoReturn:
        raise AssertionError("_detect_gpu_id should not run when there is no config to pin")

    monkeypatch.setattr(shadps4, "_detect_gpu_id", fail)
    shadps4._pin_gpu_id()  # pinned does not exist; must not raise


class _FakeStdin:
    def __init__(self, fail: bool = False) -> None:
        self.written: list[bytes] = []
        self.flush_count = 0
        self.fail = fail

    def write(self, data: bytes) -> None:
        if self.fail:
            raise BrokenPipeError()
        self.written.append(data)

    def flush(self) -> None:
        self.flush_count += 1


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.stdin = _FakeStdin()
        self.wait_calls: list[Optional[float]] = []
        self.wait_exc: Optional[Exception] = None
        self.exit_code: Optional[int] = None

    def poll(self) -> Optional[int]:
        return self.exit_code

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        self.wait_calls.append(timeout)
        if self.wait_exc is not None:
            raise self.wait_exc
        self.exit_code = 0
        return self.exit_code


def test_launch_stops_first_then_spawns_with_ipc_enabled(
    monkeypatch: pytest.MonkeyPatch, versions_dir: Path, rom_root: Path
) -> None:
    """Launch stops any running instance before spawning a new one with IPC enabled."""
    binary = _make_release(versions_dir, "v0.17.0 - Only Release")
    order = []
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: order.append("stop"))
    spawned = {}

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        order.append("spawn")
        spawned["cmd"] = cmd
        spawned["env"] = env
        spawned["stdin_pipe"] = stdin_pipe
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    emu.launch(rom, resume_slot=None)

    assert order == ["stop", "spawn"]
    assert spawned["cmd"] == [str(binary), "-f", "true", "-g", str(rom)]
    assert spawned["env"]["SHADPS4_ENABLE_IPC"] == "true"
    assert spawned["stdin_pipe"] is True
    assert emu._proc.stdin.written == [b"RUN\n", b"START\n"]


def test_launch_pins_gpu_id_before_spawning(
    monkeypatch: pytest.MonkeyPatch, versions_dir: Path, rom_root: Path
) -> None:
    """Launch pins gpu_id before it spawns the emulator, not after."""
    _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    order = []
    monkeypatch.setattr(shadps4, "_pin_gpu_id", lambda: order.append("pin"))

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        order.append("spawn")
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")

    shadps4.Shadps4().launch(rom, resume_slot=None)

    assert order == ["pin", "spawn"]


def test_launch_logs_and_ignores_a_resume_slot(
    monkeypatch: pytest.MonkeyPatch,
    versions_dir: Path,
    rom_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Launch ignores an unsupported resume_slot and logs that it did so."""
    _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    with caplog.at_level("INFO"):
        emu.launch(rom, resume_slot=3)

    assert "resume_slot 3 ignored" in caplog.text
    assert emu._proc.stdin.written == [b"RUN\n", b"START\n"]


def test_launch_raises_when_no_binary_is_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rom_root: Path
) -> None:
    """Launch raises when no shadPS4 binary can be resolved."""
    monkeypatch.setattr(shadps4, "VERSIONS_DIR", tmp_path / "does-not-exist")
    monkeypatch.delenv("SHADPS4_BIN", raising=False)
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    spawned = []
    monkeypatch.setattr(
        shadps4.Shadps4,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    with pytest.raises(RuntimeError):
        emu.launch(rom, resume_slot=None)

    assert spawned == []


def test_stop_sends_ipc_stop_and_waits_for_a_graceful_exit(
    monkeypatch: pytest.MonkeyPatch, pid_record: Path
) -> None:
    """Stop sends the IPC STOP command and waits for a graceful exit."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    emu._proc = proc

    emu.stop()

    assert proc.stdin.written == [b"STOP\n"]
    assert proc.stdin.flush_count == 1
    assert proc.wait_calls == [emu.term_timeout]
    assert escalated == []
    assert emu._proc is None
    assert not pid_record.exists()


def test_stop_falls_back_to_sigterm_escalation_when_the_stdin_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop escalates to SIGTERM when the IPC stdin write fails."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.stdin = _FakeStdin(fail=True)
    emu._proc = proc

    emu.stop()

    assert escalated == [True]


def test_stop_falls_back_to_sigterm_escalation_when_the_process_never_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop escalates to SIGTERM when the process never exits after IPC STOP."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.wait_exc = subprocess.TimeoutExpired(cmd="shadps4", timeout=emu.term_timeout)
    emu._proc = proc

    emu.stop()

    assert proc.stdin.written == [b"STOP\n"]
    assert escalated == [True]


def test_stop_is_a_no_op_when_nothing_is_running() -> None:
    """Stop does nothing when no process is running."""
    emu = shadps4.Shadps4()
    emu._proc = None

    emu.stop()

    assert emu._proc is None


def test_stop_skips_ipc_and_escalates_when_the_process_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop skips the IPC command and escalates when the process already exited."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.exit_code = 0
    emu._proc = proc

    emu.stop()

    assert proc.stdin.written == []
    assert escalated == [True]


def test_ipc_send_returns_false_when_the_process_already_exited() -> None:
    """IPC send returns False when the process has already exited."""
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.exit_code = 0
    emu._proc = proc

    assert emu._ipc_send("RUN") is False
    assert proc.stdin.written == []


def test_ipc_send_returns_false_when_the_write_fails() -> None:
    """IPC send returns False when writing to stdin raises."""
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.stdin = _FakeStdin(fail=True)
    emu._proc = proc

    assert emu._ipc_send("RUN") is False


def test_ipc_send_returns_false_when_there_is_no_stdin() -> None:
    """IPC send returns False when the process has no stdin pipe."""
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.stdin = None
    emu._proc = proc

    assert emu._ipc_send("RUN") is False


# ── shutdown verdict and unmounted saves ───────────────────────────────


@pytest.fixture
def savedata_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the emulator's save root at a temp dir and hand back its savedata subtree."""
    root = tmp_path / "shadPS4"
    savedata = root / shadps4.SAVEDATA_SUBTREE
    savedata.mkdir(parents=True)
    monkeypatch.setattr(shadps4.Shadps4, "save_root", root)
    return savedata


def _mounted_save(savedata: Path, serial: str, slot: str = "SAVE00") -> Path:
    """Build a save directory still carrying shadPS4's read-write mount marker."""
    save = savedata / serial / slot
    (save / "sce_sys").mkdir(parents=True)
    (save / "sce_sys" / "corrupted").write_bytes(b"")
    (save / "data.bin").write_bytes(b"progress")
    return save


def test_stop_records_a_graceful_exit_when_ipc_stop_is_answered(
    monkeypatch: pytest.MonkeyPatch, pid_record: Path
) -> None:
    """A quit shadPS4 performed itself is the only one whose save flush can be trusted."""
    monkeypatch.setattr(base.Emulator, "stop", lambda self: None)
    emu = shadps4.Shadps4()
    emu._proc = _FakeProc()

    emu.stop()

    assert emu._graceful_exit is True


def test_stop_records_a_forced_exit_when_the_stdin_write_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A STOP that never reached shadPS4 means SIGTERM killed it wherever it was."""
    monkeypatch.setattr(base.Emulator, "stop", lambda self: None)
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.stdin = _FakeStdin(fail=True)
    emu._proc = proc

    with caplog.at_level("WARNING"):
        emu.stop()

    assert emu._graceful_exit is False
    assert "IPC STOP could not be delivered" in caplog.text


def test_stop_records_a_forced_exit_when_the_process_never_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A STOP that timed out escalates to SIGTERM, which shadPS4 has no handler for."""
    monkeypatch.setattr(base.Emulator, "stop", lambda self: None)
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.wait_exc = subprocess.TimeoutExpired(cmd="shadps4", timeout=emu.term_timeout)
    emu._proc = proc

    with caplog.at_level("WARNING"):
        emu.stop()

    assert emu._graceful_exit is False
    assert "did not exit within" in caplog.text


def test_launch_clears_the_previous_sessions_shutdown_verdict(
    monkeypatch: pytest.MonkeyPatch, versions_dir: Path, rom_root: Path
) -> None:
    """A verdict left over from the last session must not be reported for this one."""
    _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()
    emu._graceful_exit = False

    emu.launch(rom, resume_slot=None)

    assert emu._graceful_exit is None


def test_save_and_exit_reports_a_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch, savedata_root: Path
) -> None:
    """A graceful stop that left no save mounted reports exactly that, with no states."""
    (savedata_root / "CUSA00001" / "SAVE00" / "sce_sys").mkdir(parents=True)
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    emu = shadps4.Shadps4()
    emu._graceful_exit = True

    report = emu.save_and_exit(None)

    assert report == {
        "state_saved": None,
        "state_slot": None,
        "state_file": None,
        "graceful_exit": True,
        "unmounted_saves": [],
    }


def test_save_and_exit_names_the_saves_shadps4_never_unmounted(
    monkeypatch: pytest.MonkeyPatch, savedata_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A save still marked mounted was cut off mid-write, and the dump ships it regardless.

    saves.py strips the marker itself out of the archive, so this log line is the only
    trace that the uploaded save may be torn.
    """
    stranded = _mounted_save(savedata_root, "CUSA00001")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    emu = shadps4.Shadps4()
    emu._graceful_exit = False

    with caplog.at_level("ERROR"):
        report = emu.save_and_exit(None)

    assert report["graceful_exit"] is False
    assert report["unmounted_saves"] == [str(stranded)]
    assert "never unmounted" in caplog.text


def test_save_and_exit_warns_when_a_forced_stop_left_no_save_mounted(
    monkeypatch: pytest.MonkeyPatch, savedata_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A forced stop with nothing left mounted is still worth saying, but is not an error."""
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    emu = shadps4.Shadps4()
    emu._graceful_exit = False

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(None)

    assert report["unmounted_saves"] == []
    assert "force-stopped" in caplog.text


def test_save_and_exit_logs_and_ignores_a_slot(
    monkeypatch: pytest.MonkeyPatch, savedata_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A state slot is meaningless here and must not be echoed back as if one was written."""
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    emu = shadps4.Shadps4()

    with caplog.at_level("INFO"):
        report = emu.save_and_exit(2)

    assert report["state_slot"] is None
    assert "exit slot 2 ignored" in caplog.text


def test_save_and_exit_stops_the_emulator_before_reading_the_save_tree(
    monkeypatch: pytest.MonkeyPatch, savedata_root: Path
) -> None:
    """The scan has to follow the stop: a running emulator's markers say nothing yet."""
    order = []
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: order.append("stop"))
    monkeypatch.setattr(
        shadps4, "_unmounted_saves", lambda root: order.append("scan") or []
    )

    shadps4.Shadps4().save_and_exit(None)

    assert order == ["stop", "scan"]


def test_unmounted_saves_ignores_a_corrupted_file_outside_sce_sys(savedata_root: Path) -> None:
    """Only shadPS4's own sce_sys marker counts; a game's own file of that name does not."""
    save = savedata_root / "CUSA00001" / "SAVE00"
    save.mkdir(parents=True)
    (save / "corrupted").write_bytes(b"a save file the game happened to name that")

    assert shadps4._unmounted_saves(savedata_root) == []


def test_unmounted_saves_finds_every_marked_save(savedata_root: Path) -> None:
    """Each mounted save is reported once, at the slot directory holding the marker."""
    first = _mounted_save(savedata_root, "CUSA00001")
    second = _mounted_save(savedata_root, "CUSA00002", "SAVE01")

    assert shadps4._unmounted_saves(savedata_root) == sorted([first, second])


def test_unmounted_saves_is_empty_without_a_savedata_tree(tmp_path: Path) -> None:
    """A container that has never run a game has no savedata tree to scan."""
    assert shadps4._unmounted_saves(tmp_path / "nope") == []


# ── pkg extraction / extraction cache ──────────────────────────────────


def _touch(path: Path, mtime: Optional[float] = None) -> Path:
    """Write a placeholder file, creating parents, optionally with a fixed mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the pkg extraction cache at an isolated temp directory with caching disabled."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(shadps4, "CACHE_DIR", cache)
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", False)
    return cache


@pytest.mark.parametrize("setting,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("", False),
])
def test_the_cache_enabled_switch_reads_the_usual_spellings(setting: str, expected: bool) -> None:
    """_truthy recognizes the usual truthy and falsy string spellings."""
    assert shadps4._truthy(setting) is expected


def test_extracted_dir_size_sums_files_and_skips_the_marker(cache_dir: Path) -> None:
    """Extracted dir size sums files and skips the last-accessed marker."""
    game_dir = cache_dir / "Game"
    _touch(game_dir / "eboot.bin")
    _touch(game_dir / "sce_sys" / "param.sfo")
    _touch(game_dir / shadps4._LAST_ACCESSED_MARKER)

    assert shadps4._extracted_dir_size(game_dir) == 10


def test_cache_size_bytes_sums_across_every_game_dir(cache_dir: Path) -> None:
    """Cache size bytes sums across every game dir."""
    _touch(cache_dir / "GameA" / "eboot.bin")
    _touch(cache_dir / "GameB" / "eboot.bin")

    assert shadps4._cache_size_bytes() == 10


def test_cache_size_bytes_is_zero_without_a_cache_dir(cache_dir: Path) -> None:
    """Cache size bytes is zero without a cache dir."""
    assert shadps4._cache_size_bytes() == 0


def test_touch_last_accessed_writes_a_marker_file(cache_dir: Path) -> None:
    """Touch last accessed writes a marker file."""
    game_dir = cache_dir / "Game"
    _touch(game_dir / "eboot.bin")

    shadps4._touch_last_accessed(game_dir)

    assert (game_dir / shadps4._LAST_ACCESSED_MARKER).exists()


def test_evict_lru_is_a_noop_when_disabled(cache_dir: Path) -> None:
    """Evict LRU is a no-op when disabled."""
    game_dir = cache_dir / "GameA"
    _touch(game_dir / "eboot.bin")

    shadps4._evict_lru(10**9, "SomethingElse")

    assert game_dir.exists()


def test_evict_lru_removes_the_least_recently_used_entry_first(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU removes the least recently used entry first."""
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 8 / 1024**3)
    old = cache_dir / "Old"
    new = cache_dir / "New"
    _touch(old / "eboot.bin")
    _touch(new / "eboot.bin")
    _touch(old / shadps4._LAST_ACCESSED_MARKER, mtime=1000)
    _touch(new / shadps4._LAST_ACCESSED_MARKER, mtime=2000)

    shadps4._evict_lru(2, "Incoming")

    assert not old.exists()
    assert new.exists()


def test_evict_lru_never_removes_the_entry_being_extracted(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU never removes the entry being extracted."""
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 1 / 1024**3)
    keep = cache_dir / "Incoming"
    _touch(keep / "eboot.bin")
    _touch(keep / shadps4._LAST_ACCESSED_MARKER, mtime=1)

    shadps4._evict_lru(50, "Incoming")

    assert keep.exists()


def test_evict_lru_gives_up_and_proceeds_when_nothing_is_left_to_evict(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU gives up and proceeds when nothing is left to evict."""
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 1 / 1024**3)
    cache_dir.mkdir(parents=True, exist_ok=True)

    shadps4._evict_lru(10**9, "Incoming")  # must not raise


def test_extracted_boot_target_finds_the_eboot_pkg_extractor_produced(cache_dir: Path) -> None:
    """Extracted boot target finds the eboot.bin under a title-id output subfolder."""
    root = cache_dir / "Game"
    eboot = _touch(root / "CUSA23079" / "eboot.bin")

    assert shadps4._extracted_boot_target(root) == eboot


def test_extracted_boot_target_is_none_when_nothing_bootable_is_present(cache_dir: Path) -> None:
    """Extracted boot target is none when nothing bootable is present."""
    root = cache_dir / "Game"
    _touch(root / "CUSA23079" / "sce_sys" / "param.sfo")

    assert shadps4._extracted_boot_target(root) is None


def test_extracted_boot_target_rejects_an_eboot_that_symlinks_outside_root(
    cache_dir: Path, tmp_path: Path
) -> None:
    """Extracted boot target rejects an eboot symlinked outside the extraction root."""
    outside = _touch(tmp_path / "outside" / "eboot.bin")
    root = cache_dir / "Game"
    root.mkdir(parents=True)
    (root / "eboot.bin").symlink_to(outside)

    assert shadps4._extracted_boot_target(root) is None


def test_cache_key_changes_when_a_same_named_pkg_is_replaced(tmp_path: Path) -> None:
    """Cache key changes when a same-named pkg is replaced with different content."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"original")
    original_key = shadps4._cache_key(pkg)

    pkg.write_bytes(b"a completely different replacement dump")
    os.utime(pkg, (pkg.stat().st_mtime + 5, pkg.stat().st_mtime + 5))

    assert shadps4._cache_key(pkg) != original_key


def test_run_pkg_extractor_raises_on_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run pkg extractor raises on a nonzero exit."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 2, "stderr": "boom"})(),
    )

    with pytest.raises(RuntimeError, match="exited 2"):
        shadps4._run_pkg_extractor(tmp_path / "Game.pkg", tmp_path / "dest")


def test_run_pkg_extractor_raises_when_the_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run pkg extractor raises when the binary is missing."""
    def raise_oserror(*a: object, **k: object) -> NoReturn:
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(RuntimeError, match="failed to run"):
        shadps4._run_pkg_extractor(tmp_path / "Game.pkg", tmp_path / "dest")


def test_extract_and_cache_pkg_reuses_an_existing_bootable_extraction(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg reuses an existing bootable extraction."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    key = shadps4._cache_key(pkg)
    eboot = _touch(cache_dir / key / "CUSA23079" / "eboot.bin")
    called = []
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", lambda *a: called.append(a))

    boot = shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert boot == eboot
    assert called == []
    assert (cache_dir / key / shadps4._LAST_ACCESSED_MARKER).exists()


def test_extract_and_cache_pkg_re_extracts_a_stale_cache_dir_with_no_boot_target(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg re-extracts a stale cache dir with no boot target."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    stale = cache_dir / shadps4._cache_key(pkg)
    _touch(stale / "readme.txt")

    def fake_extract(pkg_: Path, dest: Path) -> None:
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    boot = shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert boot.name == "eboot.bin"
    assert not (stale / "readme.txt").exists()


def test_extract_and_cache_pkg_extracts_and_returns_the_boot_target_on_a_miss(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg extracts and returns the boot target on a miss."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")

    def fake_extract(pkg_: Path, dest: Path) -> None:
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    boot = shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert boot.name == "eboot.bin"
    assert (cache_dir / shadps4._cache_key(pkg) / shadps4._LAST_ACCESSED_MARKER).exists()


def test_extract_and_cache_pkg_sets_and_clears_extracting_pkg_phase(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extracting a bare .pkg reports the extracting_pkg phase, then clears it."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    emulator = shadps4.Shadps4()
    seen_phase = []

    def fake_extract(pkg_: Path, dest: Path) -> None:
        seen_phase.append(emulator.extraction_phase)
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    shadps4._extract_and_cache_pkg(pkg, emulator)

    assert seen_phase == ["extracting_pkg"]
    assert emulator.extraction_phase is None


def test_extract_and_cache_pkg_clears_the_phase_when_extraction_fails(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extraction phase resets to None even when pkg_extractor raises."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    emulator = shadps4.Shadps4()

    def fake_extract(pkg_: Path, dest: Path) -> NoReturn:
        raise RuntimeError("boom")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    with pytest.raises(RuntimeError, match="boom"):
        shadps4._extract_and_cache_pkg(pkg, emulator)

    assert emulator.extraction_phase is None


def test_extract_and_cache_pkg_cleans_up_and_raises_when_extraction_fails(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg cleans up and raises when extraction fails."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")

    def fake_extract(pkg_: Path, dest: Path) -> NoReturn:
        raise RuntimeError("boom")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    with pytest.raises(RuntimeError, match="boom"):
        shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert not (cache_dir / shadps4._cache_key(pkg)).exists()


def test_extract_and_cache_pkg_cleans_up_and_raises_when_nothing_bootable_was_extracted(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg cleans up and raises when nothing bootable was extracted."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", lambda p, d: None)

    with pytest.raises(RuntimeError, match="no eboot.bin"):
        shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert not (cache_dir / shadps4._cache_key(pkg)).exists()


def test_a_partial_extraction_never_appears_at_the_cache_key(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is written under the cache key until a boot target is confirmed.

    A process killed mid-extraction must not leave a truncated eboot.bin
    where the next launch would take it for a finished cache entry, so the
    game dir may only appear once, complete, by rename.
    """
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    game_dir = cache_dir / shadps4._cache_key(pkg)
    seen = []

    def fake_extract(pkg_: Path, dest: Path) -> None:
        # Mid-extraction: whatever pkg_extractor has written so far is not
        # yet reachable under the cache key.
        seen.append(game_dir.exists())
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert seen == [False]
    assert (game_dir / "CUSA23079" / "eboot.bin").is_file()


def test_an_extraction_too_big_for_the_cap_is_refused_before_it_starts(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ROM whose extraction cannot fit under CACHE_MAX_GB is refused, not attempted anyway.

    Eviction cannot help when there is nothing left to evict, and starting
    regardless means filling the disk over the minutes the unpack takes
    before failing on a write error.
    """
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 0.000001)
    extracted = []
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", lambda p, d: extracted.append(p))
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"x" * 4096)

    with pytest.raises(RuntimeError, match="SHADPS4_CACHE_MAX_GB"):
        shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert extracted == []
    assert not (cache_dir / shadps4._cache_key(pkg)).exists()


def test_the_cap_is_charged_for_what_an_archive_leaves_behind_not_its_peak(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive is judged against CACHE_MAX_GB by what it caches, not its transient peak.

    Unpacking an archive holds the .pkg and pkg_extractor's output at once,
    but only the output is kept. Charging the cap for the peak would refuse
    titles that sit well under it once extracted.
    """
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"x" * 4096)
    # Between what the extraction keeps (1.1x) and what it peaks at (2.2x).
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 6000 / shadps4._GB)
    monkeypatch.setattr(shadps4, "_extract_archive", lambda a, d: _touch(d / "Game.pkg"))
    monkeypatch.setattr(
        shadps4, "_run_pkg_extractor", lambda p, d: _touch(d / "CUSA23079" / "eboot.bin")
    )

    boot = shadps4._extract_and_cache_pkg(archive, shadps4.Shadps4())

    assert boot == cache_dir / shadps4._cache_key(archive) / "CUSA23079" / "eboot.bin"


def test_an_extraction_larger_than_the_free_disk_is_refused_before_it_starts(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ROM that would not fit on the filesystem is refused even when the cap allows it.

    CACHE_MAX_GB bounds the cache's own contents, not the disk it shares
    with the rest of /config, so the cap passing proves nothing about there
    being room to write.
    """
    monkeypatch.setattr(
        shadps4.shutil, "disk_usage", lambda p: SimpleNamespace(total=1, used=1, free=1)
    )
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"x" * 4096)

    with pytest.raises(RuntimeError, match="is free on"):
        shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())


def test_an_uncleared_cache_dir_fails_the_extraction_instead_of_silently_escaping(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A game dir that would not delete raises RuntimeError, not a bare OSError.

    The stale-entry cleanup ignores errors, so the rename into place can
    still find the old directory there; launch only translates RuntimeError
    into a useful message.
    """
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    game_dir = _touch(cache_dir / shadps4._cache_key(pkg) / "junk.txt").parent
    monkeypatch.setattr(shadps4.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(
        shadps4, "_run_pkg_extractor", lambda p, d: _touch(d / "CUSA23079" / "eboot.bin")
    )

    with pytest.raises(RuntimeError, match="could not cache the extraction"):
        shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert (game_dir / "junk.txt").is_file()


def test_extraction_reclaims_orphaned_scratch_before_sizing_the_cache(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scratch left by a dead process is removed before eviction, not evicted around.

    Scratch is exempt from eviction but still counts toward CACHE_MAX_GB, so
    leaving an orphan in place would make eviction delete real cache entries
    to make room for space that is already garbage.
    """
    orphan = _touch(cache_dir / shadps4._SCRATCH_DIR_NAME / "dead-run" / "huge.pkg").parent
    scratch_at_eviction = []
    monkeypatch.setattr(
        shadps4,
        "_evict_lru",
        lambda needed, keep: scratch_at_eviction.append(orphan.exists()),
    )
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", lambda p, d: _touch(d / "T" / "eboot.bin"))
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")

    shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    assert scratch_at_eviction == [False]


def test_extract_and_cache_pkg_reserves_more_headroom_for_an_archive_than_a_pkg(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive reserves disk for its scratch extraction and pkg_extractor's output at once.

    Only the output is kept either way, so the cache cap is charged the same
    1.1x for both while the disk check carries the archive's higher peak.
    """
    seen = []
    monkeypatch.setattr(shadps4, "_evict_lru", lambda needed, keep: None)
    monkeypatch.setattr(
        shadps4, "_require_room", lambda peak, kept, name: seen.append((peak, kept))
    )
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", lambda p, d: _touch(d / "T" / "eboot.bin"))

    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"x" * 1000)
    shadps4._extract_and_cache_pkg(pkg, shadps4.Shadps4())

    archive = _make_zip(tmp_path / "Game.zip", {"Game.pkg": b"x" * 1000})
    shadps4._extract_and_cache_pkg(archive, shadps4.Shadps4())

    pkg_size, archive_size = pkg.stat().st_size, archive.stat().st_size
    assert seen == [
        (int(pkg_size * 1.1), int(pkg_size * 1.1)),
        (int(archive_size * 2.2), int(archive_size * 1.1)),
    ]


def test_sweep_stale_extractions_removes_orphaned_scratch_dirs_but_keeps_real_ones(
    cache_dir: Path,
) -> None:
    """Sweep stale extractions empties the scratch subtree but keeps real cache entries."""
    scratch = _touch(
        cache_dir / shadps4._SCRATCH_DIR_NAME / "Game-abc123-xyz987" / "leftover.pkg"
    ).parent
    game_dir = _touch(cache_dir / "Game-abc123" / "eboot.bin").parent

    shadps4.sweep_stale_extractions()

    assert not scratch.exists()
    assert game_dir.exists()


def test_sweep_stale_extractions_spares_a_cache_dir_whose_own_name_looks_like_scratch(
    cache_dir: Path,
) -> None:
    """A real cache dir survives the sweep no matter what its ROM's filename contained.

    `_cache_key` names a cache dir `<rom stem>-<digest>`, so a ROM like
    "Uncharted-archive-Edition.pkg" once produced a dir the name-matching
    sweep mistook for scratch. Scratch dirs now live in their own subtree,
    so the name carries no weight either way.
    """
    collider = cache_dir / "Uncharted-archive-Edition-a1b2c3d4e5f6"
    _touch(collider / "CUSA23079" / "eboot.bin")

    shadps4.sweep_stale_extractions()

    assert collider.exists()


def test_sweep_stale_extractions_keeps_a_real_cache_dir_with_no_marker_yet(
    cache_dir: Path,
) -> None:
    """A fully extracted cache dir survives even before its last-accessed marker exists.

    Covers the process being killed between pkg_extractor finishing and
    `_touch_last_accessed` running, and the marker write simply failing:
    re-extracting a multi-GB title because a zero-byte marker is missing
    would be a very expensive false positive.
    """
    game_dir = _touch(cache_dir / "Game-abc123" / "CUSA23079" / "eboot.bin").parent.parent
    assert not (game_dir / shadps4._LAST_ACCESSED_MARKER).exists()

    shadps4.sweep_stale_extractions()

    assert game_dir.exists()


def test_sweep_stale_extractions_removes_a_scratch_dir_whose_archive_held_an_eboot_decoy(
    cache_dir: Path,
) -> None:
    """An orphaned scratch dir is swept even when its archive held a file named eboot.bin.

    A scratch dir holds an archive's raw, unpacked contents, so a
    bootable-looking file under it proves nothing; living under the scratch
    subtree is what makes it scratch.
    """
    scratch = _touch(
        cache_dir / shadps4._SCRATCH_DIR_NAME / "Game-abc123" / "bonus" / "eboot.bin"
    ).parent.parent

    shadps4.sweep_stale_extractions()

    assert not scratch.exists()


def test_sweep_stale_extractions_is_a_noop_without_a_cache_dir(cache_dir: Path) -> None:
    """Sweep stale extractions is a no-op when the cache dir does not exist yet."""
    shadps4.sweep_stale_extractions()  # must not raise


def test_extract_and_cache_pkg_serializes_a_second_call_racing_the_same_pkg(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A racing second call must wait for _CACHE_LOCK, not extract concurrently.

    A second launch racing in while the first is mid-extraction must wait
    for _CACHE_LOCK rather than run its own extraction concurrently, which
    could interleave writes into the same not-yet-populated game_dir or
    have one call evict the directory the other is about to boot from.
    """
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")

    entered = threading.Event()
    release = threading.Event()
    entry_count: list[int] = []

    def fake_run_pkg_extractor(pkg_: Path, dest: Path) -> None:
        entry_count.append(1)
        entered.set()
        release.wait(timeout=5)
        (dest / "CUSA23079").mkdir(parents=True, exist_ok=True)
        (dest / "CUSA23079" / "eboot.bin").write_bytes(b"x")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_run_pkg_extractor)

    first = threading.Thread(
        target=shadps4._extract_and_cache_pkg, args=(pkg, shadps4.Shadps4())
    )
    first.start()
    assert entered.wait(timeout=5)

    second = threading.Thread(
        target=shadps4._extract_and_cache_pkg, args=(pkg, shadps4.Shadps4())
    )
    second.start()
    time.sleep(0.2)
    # Still 1: the second call is blocked on _CACHE_LOCK, not free to run its
    # own extraction while the first is still inside the critical section.
    assert entry_count == [1]

    release.set()
    first.join(timeout=5)
    second.join(timeout=5)


# ── archive extraction (zip/7z/rar) feeding pkg_extractor ──────────────


def _make_zip(path: Path, members: dict) -> Path:
    """Write a real zip file at path with the given {member_name: bytes} contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_archive_pkg_member_finds_a_pkg_inside_an_extracted_tree(tmp_path: Path) -> None:
    """Archive pkg member finds a .pkg file inside the extracted tree."""
    root = tmp_path / "extracted"
    pkg = _touch(root / "Game" / "CUSA23079.pkg")

    assert shadps4._archive_pkg_member(root) == pkg


def test_archive_pkg_member_is_none_without_a_pkg(tmp_path: Path) -> None:
    """Archive pkg member is none when the extracted tree holds no .pkg."""
    root = tmp_path / "extracted"
    _touch(root / "readme.txt")

    assert shadps4._archive_pkg_member(root) is None


def test_archive_pkg_member_rejects_a_pkg_that_symlinks_outside_root(tmp_path: Path) -> None:
    """Archive pkg member rejects a .pkg symlinked outside the extraction root."""
    outside = _touch(tmp_path / "outside" / "Game.pkg")
    root = tmp_path / "extracted"
    root.mkdir(parents=True)
    (root / "Game.pkg").symlink_to(outside)

    assert shadps4._archive_pkg_member(root) is None


def test_reject_unsafe_members_raises_on_a_zip_slip_path(tmp_path: Path) -> None:
    """Reject unsafe members raises on a member path that escapes dest."""
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(RuntimeError, match="escapes extraction dir"):
        shadps4._reject_unsafe_members(dest, ["../outside.txt"])


def test_reject_unsafe_members_allows_a_normal_relative_path(tmp_path: Path) -> None:
    """Reject unsafe members allows an ordinary relative member path."""
    dest = tmp_path / "dest"
    dest.mkdir()

    shadps4._reject_unsafe_members(dest, ["Game/CUSA23079.pkg"])  # must not raise


def test_extract_archive_unpacks_a_zip_via_the_stdlib(tmp_path: Path) -> None:
    """Extract archive unpacks a real .zip using the stdlib zipfile module."""
    archive = _make_zip(tmp_path / "Game.zip", {"CUSA23079.pkg": b"pkg data"})
    dest = tmp_path / "dest"
    dest.mkdir()

    shadps4._extract_archive(archive, dest)

    assert (dest / "CUSA23079.pkg").read_bytes() == b"pkg data"


def test_extract_archive_rejects_a_zip_slip_member(tmp_path: Path) -> None:
    """Extract archive raises rather than write a zip member outside dest."""
    archive = tmp_path / "Game.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.pkg", b"pkg data")
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(RuntimeError, match="escapes extraction dir"):
        shadps4._extract_archive(archive, dest)


def test_extract_archive_raises_on_a_corrupt_zip(tmp_path: Path) -> None:
    """Extract archive raises when the .zip is not a valid archive."""
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"not a zip")
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(RuntimeError, match="zip extraction"):
        shadps4._extract_archive(archive, dest)


def test_extract_archive_dispatches_7z_through_the_external_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract archive dispatches a .7z through the 7z listing and extraction tools."""
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    calls = []
    monkeypatch.setattr(shadps4, "_7z_member_paths", lambda a: ["CUSA23079.pkg"])

    def fake_run_extractor(cmd: list, what: str) -> str:
        calls.append(cmd)
        (dest / "CUSA23079.pkg").write_bytes(b"pkg data")
        return ""

    monkeypatch.setattr(shadps4, "_run_extractor", fake_run_extractor)

    shadps4._extract_archive(archive, dest)

    assert calls and calls[0][0] == "7z"
    assert (dest / "CUSA23079.pkg").exists()


def test_extract_archive_dispatches_rar_through_the_external_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract archive dispatches a .rar through the unrar listing and extraction tools."""
    archive = tmp_path / "Game.rar"
    archive.write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    calls = []
    monkeypatch.setattr(shadps4, "_rar_member_paths", lambda a: ["CUSA23079.pkg"])

    def fake_run_extractor(cmd: list, what: str) -> str:
        calls.append(cmd)
        (dest / "CUSA23079.pkg").write_bytes(b"pkg data")
        return ""

    monkeypatch.setattr(shadps4, "_run_extractor", fake_run_extractor)

    shadps4._extract_archive(archive, dest)

    assert calls and calls[0][0] == "unrar"
    assert (dest / "CUSA23079.pkg").exists()


def test_reject_escaped_tree_raises_when_a_symlink_escapes_dest(tmp_path: Path) -> None:
    """Reject escaped tree raises when a real symlink resolves outside dest."""
    outside = _touch(tmp_path / "outside.pkg")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "escaped.pkg").symlink_to(outside)

    with pytest.raises(RuntimeError, match="escapes cache dir"):
        shadps4._reject_escaped_tree(dest)


def test_reject_escaped_tree_allows_a_normal_extraction(tmp_path: Path) -> None:
    """Reject escaped tree allows a normal, fully-contained extraction."""
    dest = tmp_path / "dest"
    _touch(dest / "Game" / "CUSA23079.pkg")

    shadps4._reject_escaped_tree(dest)  # must not raise


def test_run_extractor_raises_on_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run extractor raises on a nonzero exit."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 2, "stderr": "boom", "stdout": ""})(),
    )

    with pytest.raises(RuntimeError, match="exited 2"):
        shadps4._run_extractor(["7z", "l"], "7z list (Game.7z)")


def test_run_extractor_raises_when_the_binary_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run extractor raises when the underlying binary cannot be run."""
    def raise_oserror(*a: object, **k: object) -> NoReturn:
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(RuntimeError, match="failed to run"):
        shadps4._run_extractor(["unrar", "lb"], "unrar list (Game.rar)")


def test_extract_and_cache_pkg_extracts_an_archive_and_returns_the_boot_target(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg unpacks an archive, extracts the .pkg it holds, and boots the result."""
    archive = _make_zip(tmp_path / "Game.zip", {"CUSA23079.pkg": b"pkg data"})
    seen_pkgs = []

    def fake_run_pkg_extractor(pkg: Path, dest: Path) -> None:
        seen_pkgs.append(pkg.name)
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_run_pkg_extractor)

    boot = shadps4._extract_and_cache_pkg(archive, shadps4.Shadps4())

    assert boot.name == "eboot.bin"
    assert seen_pkgs == ["CUSA23079.pkg"]
    # Only pkg_extractor's own output survives; the scratch archive
    # extraction under CACHE_DIR/.scratch is discarded.
    remaining_top_level = {p.name for p in cache_dir.iterdir()}
    assert remaining_top_level == {shadps4._cache_key(archive), shadps4._SCRATCH_DIR_NAME}
    assert not list((cache_dir / shadps4._SCRATCH_DIR_NAME).iterdir())


def test_extract_and_cache_pkg_moves_through_archive_then_pkg_phases(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive reports extracting_archive, then extracting_pkg, then clears the phase."""
    archive = _make_zip(tmp_path / "Game.zip", {"CUSA23079.pkg": b"pkg data"})
    emulator = shadps4.Shadps4()
    seen_phase = []

    real_extract_archive = shadps4._extract_archive

    def spying_extract_archive(archive_: Path, dest: Path) -> None:
        seen_phase.append(emulator.extraction_phase)
        real_extract_archive(archive_, dest)

    def fake_run_pkg_extractor(pkg: Path, dest: Path) -> None:
        seen_phase.append(emulator.extraction_phase)
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_extract_archive", spying_extract_archive)
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_run_pkg_extractor)

    shadps4._extract_and_cache_pkg(archive, emulator)

    assert seen_phase == ["extracting_archive", "extracting_pkg"]
    assert emulator.extraction_phase is None


def test_extract_and_cache_pkg_reuses_a_cached_archive_extraction(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg reuses a cached extraction on a relaunch of the same archive."""
    archive = _make_zip(tmp_path / "Game.zip", {"CUSA23079.pkg": b"pkg data"})
    key = shadps4._cache_key(archive)
    eboot = _touch(cache_dir / key / "CUSA23079" / "eboot.bin")
    called = []
    monkeypatch.setattr(shadps4, "_extract_archive", lambda *a: called.append(a))

    boot = shadps4._extract_and_cache_pkg(archive, shadps4.Shadps4())

    assert boot == eboot
    assert called == []


def test_extract_and_cache_pkg_raises_when_the_archive_holds_no_pkg(
    cache_dir: Path, tmp_path: Path
) -> None:
    """Extract and cache pkg cleans up and raises when the archive holds no .pkg."""
    archive = _make_zip(tmp_path / "Game.zip", {"readme.txt": b"nothing bootable"})

    with pytest.raises(RuntimeError, match=r"held no \.pkg"):
        shadps4._extract_and_cache_pkg(archive, shadps4.Shadps4())

    assert not (cache_dir / shadps4._cache_key(archive)).exists()


def test_launch_extracts_and_boots_from_a_zip_archive(
    monkeypatch: pytest.MonkeyPatch, versions_dir: Path, rom_root: Path, tmp_path: Path
) -> None:
    """Launch dispatches a .zip ROM through the same extraction cache as a .pkg."""
    binary = _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    extracted_boot = _touch(tmp_path / "extracted" / "CUSA23079" / "eboot.bin")
    monkeypatch.setattr(
        shadps4, "_extract_and_cache_pkg", lambda rom, emulator: extracted_boot
    )
    rom = rom_root / "game.zip"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    emu.launch(rom, resume_slot=None)

    assert spawned["cmd"] == [str(binary), "-f", "true", "-g", str(extracted_boot)]


def test_launch_extracts_and_boots_from_a_pkg_rom(
    monkeypatch: pytest.MonkeyPatch, versions_dir: Path, rom_root: Path, tmp_path: Path
) -> None:
    """Launch extracts a .pkg ROM through the cache and boots the extracted eboot.bin."""
    binary = _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    extracted_boot = _touch(tmp_path / "extracted" / "CUSA23079" / "eboot.bin")
    monkeypatch.setattr(
        shadps4, "_extract_and_cache_pkg", lambda pkg, emulator: extracted_boot
    )
    rom = rom_root / "game.pkg"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    emu.launch(rom, resume_slot=None)

    assert spawned["cmd"] == [str(binary), "-f", "true", "-g", str(extracted_boot)]


def test_stop_keeps_the_pkg_extraction_for_the_next_launch(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path, versions_dir: Path, rom_root: Path
) -> None:
    """Stop leaves the cached extraction alone so the next launch reuses it."""
    _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "_spawn", lambda self, cmd, env, stdin_pipe=False: None)
    rom = rom_root / "game.pkg"
    rom.write_bytes(b"")
    game_dir = cache_dir / shadps4._cache_key(rom)
    monkeypatch.setattr(
        shadps4,
        "_extract_and_cache_pkg",
        lambda pkg, emulator: _touch(game_dir / "eboot.bin"),
    )

    emu = shadps4.Shadps4()
    emu.launch(rom, resume_slot=None)

    emu.stop()

    assert game_dir.exists()


def test_the_cache_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache is disabled by default when SHADPS4_CACHE_ENABLED is unset."""
    monkeypatch.delenv("SHADPS4_CACHE_ENABLED", raising=False)
    assert shadps4._truthy(os.environ.get("SHADPS4_CACHE_ENABLED", "false")) is False


# ── archive listings must fail closed ──────────────────────────────────


def test_rar_member_paths_raises_when_the_listing_names_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrar listing with no members stops the extraction.

    An empty parse used to reach `_reject_unsafe_members` as an empty list,
    which approves every member in the archive without checking one.
    """
    monkeypatch.setattr(shadps4, "_run_extractor", lambda cmd, what: "\n  \n\n")

    with pytest.raises(RuntimeError, match="listed no members"):
        shadps4._rar_member_paths(tmp_path / "Game.rar")


def test_rar_member_paths_returns_the_listed_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal unrar listing yields one path per member."""
    monkeypatch.setattr(
        shadps4, "_run_extractor", lambda cmd, what: "Game/CUSA23079.pkg\nGame/readme.txt\n"
    )

    assert shadps4._rar_member_paths(tmp_path / "Game.rar") == [
        "Game/CUSA23079.pkg",
        "Game/readme.txt",
    ]


def test_7z_member_paths_raises_when_the_listing_has_no_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 7z listing shaped differently than expected stops the extraction.

    Without the dashed separator there is no member section to parse, so
    every path in the archive would go unchecked.
    """
    monkeypatch.setattr(
        shadps4, "_run_extractor", lambda cmd, what: "7-Zip 24.09\n\nListing archive: Game.7z\n"
    )

    with pytest.raises(RuntimeError, match="no member section"):
        shadps4._7z_member_paths(tmp_path / "Game.7z")


def test_7z_member_paths_raises_when_the_member_section_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 7z member section holding no Path line stops the extraction."""
    listing = "7-Zip 24.09\n----------\nSize = 10\nAttributes = A\n"
    monkeypatch.setattr(shadps4, "_run_extractor", lambda cmd, what: listing)

    with pytest.raises(RuntimeError, match="listed no members"):
        shadps4._7z_member_paths(tmp_path / "Game.7z")


def test_7z_member_paths_returns_the_listed_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal 7z -slt listing yields one path per member, header skipped."""
    listing = (
        "7-Zip 24.09\n"
        "Path = Game.7z\n"
        "----------\n"
        "Path = Game/CUSA23079.pkg\n"
        "Size = 4096\n"
        "\n"
        "Path = Game/readme.txt\n"
        "Size = 12\n"
    )
    monkeypatch.setattr(shadps4, "_run_extractor", lambda cmd, what: listing)

    assert shadps4._7z_member_paths(tmp_path / "Game.7z") == [
        "Game/CUSA23079.pkg",
        "Game/readme.txt",
    ]


def test_reject_unsafe_members_raises_on_a_control_character_name(tmp_path: Path) -> None:
    """A member name carrying a newline is refused.

    The .rar/.7z member lists are read back out of a line-based listing, so a
    name holding a newline cannot be checked as the path the archive holds.
    """
    with pytest.raises(RuntimeError, match="control character"):
        shadps4._reject_unsafe_members(tmp_path, ["ok.pkg\n../../etc/evil"])


def test_extract_archive_discards_a_tree_that_escaped_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escaping extraction is emptied instead of being left for a later boot."""
    outside = _touch(tmp_path / "outside.pkg")
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(shadps4, "_7z_member_paths", lambda a: ["CUSA23079.pkg"])

    def fake_run_extractor(cmd: list, what: str) -> str:
        (dest / "CUSA23079.pkg").write_bytes(b"pkg data")
        (dest / "escaped.pkg").symlink_to(outside)
        return ""

    monkeypatch.setattr(shadps4, "_run_extractor", fake_run_extractor)

    with pytest.raises(RuntimeError, match="escapes cache dir"):
        shadps4._extract_archive(archive, dest)

    assert list(dest.iterdir()) == []
    assert outside.exists()


# ── cache lock and expansion accounting ────────────────────────────────


def test_the_cache_lock_gives_up_rather_than_parking_a_request(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that cannot take the cache lock is told so instead of blocking.

    The lock is held for a whole extraction, bounded only by the 1800 s
    extraction timeout, so an untimed acquire would park a second launch's
    request thread for that long.
    """
    monkeypatch.setattr(shadps4, "_CACHE_LOCK_WAIT", 0.05)
    with shadps4._cache_lock("first"):
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="still running"):
            with shadps4._cache_lock("second"):
                pass
        waited = time.monotonic() - started

    assert waited < 5


def test_the_cache_lock_is_released_when_the_block_raises() -> None:
    """A failed extraction does not leave the cache lock held forever."""
    with pytest.raises(ValueError):
        with shadps4._cache_lock("boom"):
            raise ValueError("extraction failed")

    assert shadps4._CACHE_LOCK.acquire(timeout=0.1)
    shadps4._CACHE_LOCK.release()


def test_an_extraction_inside_its_reservation_is_not_reported(
    caplog: pytest.LogCaptureFixture
) -> None:
    """An extraction that fit the room reserved for it says nothing."""
    with caplog.at_level("ERROR"):
        shadps4._check_expansion(actual_bytes=100, reserved_bytes=200, rom_name="Game.pkg")

    assert caplog.records == []


def test_an_extraction_past_its_reservation_is_reported(
    caplog: pytest.LogCaptureFixture
) -> None:
    """An extraction that outgrew its reservation names the factor that sized it.

    The free-space guard was sized from that factor, so the run already
    slipped past it; nothing else would report that.
    """
    with caplog.at_level("ERROR"):
        shadps4._check_expansion(actual_bytes=300, reserved_bytes=200, rom_name="Game.pkg")

    assert "Game.pkg" in caplog.text
    assert "SHADPS4_PKG_EXPANSION_FACTOR" in caplog.text


def test_an_extraction_past_the_whole_cache_cap_is_refused(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An extraction bigger than the whole cache cap is refused, not cached."""
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 1.0)

    with pytest.raises(RuntimeError, match="SHADPS4_CACHE_MAX_GB"):
        shadps4._check_expansion(
            actual_bytes=2 * shadps4._GB, reserved_bytes=shadps4._GB // 2, rom_name="Game.pkg"
        )
