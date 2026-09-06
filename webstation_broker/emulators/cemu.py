"""Cemu (Wii U) launcher: settings.xml patching, gamepad profile seeding, and SIGTERM shutdown.

Cemu has no control API and no save states. Persistence is the game's own
save data, written to host files under the virtual MLC
(`mlc01/usr/save/<titleHigh>/<titleLow>/user/<persistentId>`). Cemu installs
no signal handler, so SIGTERM is a hard kill; saves are already on disk.

A first launch with no settings.xml opens a modal Getting Started dialog, so
a minimal config is seeded before every launch. Cemu creates the default
account (0x80000001) itself on first boot, so save paths line up across
containers without the account store traveling.

Cemu applies its built-in controller mapping only through the GUI, so the
broker seeds a Wii U GamePad profile for player 0 bound to the selkies
virtual pad. Cemu addresses SDL controllers by joystick GUID, and SDL >= 2.24
folds a CRC16 of the device name into that GUID, so the profile carries one
controller node per GUID variant; the one that matches binds.
"""

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Library root a resolved ROM must live under (env `ROM_ROOT`, default `/romm`)."""


def _xdg_dir(var: str, fallback: str) -> str:
    """One of Cemu's XDG directories.

    Cemu's Linux layout: config under `$XDG_CONFIG_HOME/Cemu`, user data
    (the mlc) under `$XDG_DATA_HOME/Cemu`.

    Args:
        var: The XDG environment variable to honour when set to an absolute path.
        fallback: The path under `$HOME` used otherwise, such as `.config`.

    Returns:
        The `Cemu` directory under the chosen root.
    """
    xdg = os.environ.get(var)
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "Cemu")
    return os.path.join(os.environ.get("HOME", "/config"), fallback, "Cemu")


CONFIG_DIR = Path(os.environ.get("CEMU_CONFIG_DIR", _xdg_dir("XDG_CONFIG_HOME", ".config")))
"""Cemu's config directory (env `CEMU_CONFIG_DIR`, default `$XDG_CONFIG_HOME/Cemu`)."""
DATA_DIR = Path(os.environ.get("CEMU_DATA_DIR", _xdg_dir("XDG_DATA_HOME", ".local/share")))
"""Cemu's user data directory (env `CEMU_DATA_DIR`, default `$XDG_DATA_HOME/Cemu`)."""
SETTINGS_PATH = CONFIG_DIR / "settings.xml"
"""The settings.xml patched before every launch."""
PROFILE_PATH = CONFIG_DIR / "controllerProfiles" / "controller0.xml"
"""The player-0 controller profile the broker seeds."""
MLC_DIR = Path(os.environ.get("CEMU_MLC_DIR", str(DATA_DIR / "mlc01")))
"""The virtual MLC holding save data (env `CEMU_MLC_DIR`, default `DATA_DIR/mlc01`)."""
SAVE_DIR = MLC_DIR / "usr" / "save"
"""Where title save directories live inside the MLC."""
CEMU_LOG_PATH = Path(os.environ.get("CEMU_LOG_PATH", "/config/cemu.log"))
"""The emulator log file (env `CEMU_LOG_PATH`, default `/config/cemu.log`)."""

ROM_EXTENSIONS = (".wua", ".wux", ".wud", ".wuhb", ".iso", ".rpx", ".elf")
"""Formats Cemu boots directly, best first; a folder holding several candidates picks by this order."""
_ROM_SEARCH_GLOBS = ("*", "*/*", "*/*/*")
"""Glob patterns a ROM folder is searched with.

An extracted dump boots from `<game>/code/<title>.rpx`, two levels down; a
library folder wrapping one adds a third.
"""
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)
"""Matches update and DLC names: they sit beside base games in library folders, and the base game boots."""

_HEX8_RE = re.compile(r"^[0-9a-fA-F]{8}$")
"""Matches a title id half.

Title save dirs are `usr/save/<titleHigh>/<titleLow>`; `system` holds the
account store and play stats.
"""

_SETTINGS_PATCHES: dict[str, str] = {
    "check_update": "false",
    "receive_untested_updates": "false",
    "use_discord_presence": "false",
    "play_boot_sound": "false",
    "fullscreen_menubar": "false",
}
"""settings.xml keys forced before every launch, all children of `<content>`.

check_update phones home at startup; the rest keep the session free of
dialogs and chrome.
"""

_AUDIO_DEVICE_PATCHES: dict[str, str] = {
    "TVDevice": "default",
    "PadDevice": "default",
}
"""`Audio/<key>` values forced before every launch, all children of `<content>/Audio`.

Cemu ships these blank. `IAudioAPI::CreateDeviceFromConfig` treats an empty
device string as "no device" and silently skips opening any audio stream at
all, rather than falling back to the system default. `default` is the
sentinel `CubebAPI::GetDevices()` reserves for a null device id, the only
string that path resolves to "let cubeb pick the system default".
"""

_PAD_NAME = os.environ.get("CEMU_PAD_NAME", "Microsoft X-Box 360 pad")
"""The selkies virtual pad's name as the interposer presents it (env `CEMU_PAD_NAME`)."""

_VPAD_SDL_MAPPINGS: tuple[tuple[int, int], ...] = (
    (1, 1),    # A -> east
    (2, 0),    # B -> south
    (3, 3),    # X -> north
    (4, 2),    # Y -> west
    (5, 9),    # L -> left shoulder
    (6, 10),   # R -> right shoulder
    (7, 42),   # ZL -> left trigger axis
    (8, 43),   # ZR -> right trigger axis
    (9, 6),    # + -> start
    (10, 4),   # - -> back
    (11, 11),  # d-pad up
    (12, 12),  # d-pad down
    (13, 13),  # d-pad left
    (14, 14),  # d-pad right
    (15, 7),   # left stick click
    (16, 8),   # right stick click
    (17, 45),  # left stick up    -> axis Y-
    (18, 39),  # left stick down  -> axis Y+
    (19, 44),  # left stick left  -> axis X-
    (20, 38),  # left stick right -> axis X+
    (21, 47),  # right stick up    -> rotation Y-
    (22, 41),  # right stick down  -> rotation Y+
    (23, 46),  # right stick left  -> rotation X-
    (24, 40),  # right stick right -> rotation X+
    (27, 5),   # home -> guide
)
"""Wii U GamePad mapping ids to Cemu SDL button codes.

Cemu's own default layout for a generic SDL pad: labels map by position
(VPAD A is the east button), ZL/ZR are the analog trigger axes, sticks are
axis half-ranges.
"""


def _crc16(data: bytes) -> int:
    """CRC-16/ARC, the checksum SDL folds into a joystick GUID.

    Args:
        data: The bytes to checksum, the device name in SDL's case.

    Returns:
        The 16-bit checksum.
    """
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _sdl_guid(crc: int) -> str:
    """The SDL joystick GUID the interposer's virtual pad actually reports.

    The interposer only exposes the pad through the legacy /dev/input/jsN
    nodes, which carry no vendor/product ioctls, so SDL can't build the usual
    bus+vendor+product GUID and falls back to its name-based form instead:
    a zero bus, the name CRC, then the name itself (11 bytes, NUL-padded)
    filling the rest.

    Args:
        crc: The name CRC field, zero for SDL before 2.24.

    Returns:
        The 32-character lowercase hex GUID.
    """
    tail = _PAD_NAME.encode()[:11] + b"\x00"
    guid = bytearray(16)
    guid[2] = crc & 0xFF
    guid[3] = (crc >> 8) & 0xFF
    guid[4 : 4 + len(tail)] = tail
    return guid.hex()


def _pad_uuids() -> list[str]:
    """The uuids the profile binds, in Cemu's `<index>_<guid>` form.

    One with the CRC field zero (SDL before 2.24) and one with the name hash,
    unless `CEMU_PAD_UUIDS` overrides the list with comma-separated values.

    Returns:
        The candidate uuids, one controller node each.
    """
    override = os.environ.get("CEMU_PAD_UUIDS", "")
    if override.strip():
        return [u.strip() for u in override.split(",") if u.strip()]
    return [f"0_{_sdl_guid(0)}", f"0_{_sdl_guid(_crc16(_PAD_NAME.encode()))}"]


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Optional[Path]:
    """Pick the best bootable file among `candidates`.

    Hidden files, non-files and anything resolving outside `ROM_ROOT` are
    skipped. Ranking prefers base games over updates and DLC, then the
    `ROM_EXTENSIONS` order, then the shallowest path, then the lowercased name.

    Args:
        candidates: Paths found under `base` by the search globs.
        base: The directory the candidates were searched from.

    Returns:
        The resolved path of the best candidate, or None when nothing qualifies.
    """
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
            continue
        try:
            if not p.is_file():
                continue
            real = p.resolve()
            rel = p.relative_to(base)
        except (OSError, ValueError):
            continue
        if not real.is_relative_to(ROM_ROOT):
            continue
        is_addon = 1 if _ADDON_RE.search(str(rel)) else 0
        ranked.append(
            (is_addon, ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


def _patch_settings() -> None:
    """Force broker-required settings.xml values before every launch.

    A missing file is seeded, which also skips the first-start Getting
    Started dialog. Patched key-wise so every other setting the user tuned
    through the GUI survives untouched.

    A failure is raised rather than logged and stepped over: without a
    settings.xml Cemu parks on the Getting Started modal, so the launch that
    would go ahead anyway hands the player a blocked stream while the
    activate reports success.

    Raises:
        RuntimeError: When settings.xml cannot be written.
    """
    try:
        root = None
        if SETTINGS_PATH.exists():
            try:
                root = ET.parse(SETTINGS_PATH).getroot()
            except ET.ParseError as exc:
                log.warning("settings.xml unreadable (%s), reseeding it", exc)
        if root is None or root.tag != "content":
            root = ET.Element("content")
        for key, value in _SETTINGS_PATCHES.items():
            node = root.find(key)
            if node is None:
                node = ET.SubElement(root, key)
            node.text = value
        audio = root.find("Audio")
        if audio is None:
            audio = ET.SubElement(root, "Audio")
        for key, value in _AUDIO_DEVICE_PATCHES.items():
            node = audio.find(key)
            if node is None:
                node = ET.SubElement(audio, key)
            node.text = value
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        ET.ElementTree(root).write(tmp, encoding="UTF-8", xml_declaration=True)
        tmp.replace(SETTINGS_PATH)
    except OSError as exc:
        log.error("cemu: settings.xml patch failed at %s: %s", SETTINGS_PATH, exc)
        raise RuntimeError(
            f"could not apply broker settings to {SETTINGS_PATH}: {exc}"
        ) from exc


def _pad_profile_xml() -> str:
    """Build the controller0.xml body.

    One Wii U GamePad with a controller node per candidate uuid, all carrying
    the same mapping. Cemu loads controller nodes independently; a node whose
    device never connects is inert.

    Returns:
        The XML document text, declaration included.
    """
    root = ET.Element("emulated_controller")
    ET.SubElement(root, "type").text = "Wii U GamePad"
    for uuid in _pad_uuids():
        controller = ET.SubElement(root, "controller")
        ET.SubElement(controller, "api").text = "SDLController"
        ET.SubElement(controller, "uuid").text = uuid
        ET.SubElement(controller, "display_name").text = _PAD_NAME
        ET.SubElement(controller, "rumble").text = "0"
        for section in ("axis", "rotation", "trigger"):
            sec = ET.SubElement(controller, section)
            ET.SubElement(sec, "deadzone").text = "0.25"
            ET.SubElement(sec, "range").text = "1"
        mappings = ET.SubElement(controller, "mappings")
        for mapping, button in _VPAD_SDL_MAPPINGS:
            entry = ET.SubElement(mappings, "entry")
            ET.SubElement(entry, "mapping").text = str(mapping)
            ET.SubElement(entry, "button").text = str(button)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _seed_pad_profile() -> None:
    """Write the player-0 pad profile once, if the file is not already there.

    Seeded rather than patched so a player's own remapping, which Cemu
    writes back to this same file, survives every later launch.
    """
    if PROFILE_PATH.exists():
        return
    try:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(_pad_profile_xml())
        log.info("seeded %s", PROFILE_PATH)
    except OSError as exc:
        log.warning("could not seed the pad profile at %s: %s", PROFILE_PATH, exc)


class Cemu(Emulator):
    """Wii U via Cemu, driven by command line flags and config file patching.

    Cemu has no control API, so the broker pins settings.xml before every
    launch (no update check, no Discord presence, no boot sound, no menubar),
    seeds the player-0 GamePad profile once, and boots the game fullscreen
    with `-f -m -g`. Cemu installs no signal handler, so the stop is a hard
    SIGTERM kill; that is safe because the game's own save data is already
    on disk the moment the game writes it.

    There are no save states: persistence is the title save tree under
    `usr/save` in the virtual MLC, which is what the archive carries. At exit
    every file in the title save dirs written during the session gets its
    mtime refreshed so the delta dump ships those saves whole while other
    titles' saves stay filtered out. A resume slot is logged and ignored.

    Attributes:
        name: Provider key, `cemu`.
        display_name: Human-readable name.
        save_root: The MLC directory the save subtrees hang off.
        save_subtrees: `usr/save`, the title save tree.
        rom_extensions: Bootable formats, best first.
        log_path: The emulator log file.
        term_timeout: SIGTERM grace before SIGKILL (env `CEMU_STOP_WAIT`, default 5).
    """

    name = "cemu"
    display_name = "Cemu"
    save_root = MLC_DIR
    save_subtrees = ("usr/save",)
    rom_extensions = ROM_EXTENSIONS
    log_path = CEMU_LOG_PATH
    term_timeout = float(os.environ.get("CEMU_STOP_WAIT", "5"))
    """SIGTERM grace before SIGKILL (env `CEMU_STOP_WAIT`, default 5).

    No SIGTERM handler: the default action ends the process at once, saves
    are already on disk. The grace window only covers process-group teardown.
    """

    def __init__(self) -> None:
        """Initialise the process handle and the session baseline."""
        super().__init__()
        self._session_start = float("inf")
        """Unix time `launch` started Cemu at; infinity until it does.

        Infinity rather than zero so an instance that never launched matches
        no file at all. Zero is newer than nothing, so every title in the
        container would read as written this session and `save_and_exit`
        would restamp and ship all of them.
        """

    def prepare_restore(self) -> None:
        """Stop a running Cemu so the archive can be extracted under it."""
        self.stop()

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The file Cemu should boot for `path`.

        Args:
            path: A ROM file, or a folder searched up to three levels deep.

        Returns:
            The file itself, the best-ranked bootable file in the folder, or None.
        """
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError:
                return None
        return _pick_rom_file(candidates, path)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Patch settings, seed the pad profile and boot the game fullscreen.

        Args:
            rom_path: The file to boot.
            resume_slot: Ignored with a log line; Cemu has no save states.

        Raises:
            RuntimeError: When settings.xml cannot be patched, which would
                leave Cemu parked on its Getting Started modal.
        """
        self.stop()
        _patch_settings()
        _seed_pad_profile()
        if resume_slot:
            log.info(
                "cemu has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        self._session_start = time.time()
        binary = os.environ.get("CEMU_BIN", "Cemu")
        # CEMU_MLC_DIR is only one of three ways MLC_DIR moves: CEMU_DATA_DIR
        # and XDG_DATA_HOME shift it too, and Cemu resolves neither the same
        # way. Stating the path every launch is what keeps the emulator
        # writing saves into the tree the dump reads back.
        try:
            MLC_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cemu: could not create the mlc at %s: %s", MLC_DIR, exc)
        cmd = [binary, "-f", "-m", str(MLC_DIR), "-g", str(rom_path)]
        log.info("launching cemu (rom=%s, mlc=%s)", rom_path, MLC_DIR)
        self._spawn(cmd, base_launch_env())

    def _modified_title_saves(self) -> list[Path]:
        """Title save dirs holding a file written while the session ran.

        A listing that fails is logged and skipped rather than raised: the MLC
        can vanish under the walk, and the exit path calling this still has a
        process to stop and a report to hand back.

        Only `<8 hex>/<8 hex>` pairs count. `usr/save` also holds the `system`
        tree, and both halves feed the restamp walk, so a name that is not a
        title id half is not a title save dir.

        Returns:
            The `usr/save/<titleHigh>/<titleLow>` directories touched since
            launch, or nothing at all when no launch set a baseline.
        """
        selected: list[Path] = []
        if not SAVE_DIR.is_dir():
            return selected
        try:
            for high in sorted(SAVE_DIR.iterdir()):
                if not high.is_dir() or not _HEX8_RE.match(high.name):
                    continue
                for title in sorted(high.iterdir()):
                    if not title.is_dir() or not _HEX8_RE.match(title.name):
                        continue
                    try:
                        if any(
                            p.is_file() and p.stat().st_mtime >= self._session_start
                            for p in title.rglob("*")
                        ):
                            selected.append(title)
                    except OSError as exc:
                        log.warning(
                            "cemu: could not walk %s, its saves may be dropped from the dump: %s",
                            title,
                            exc,
                        )
                        continue
        except OSError as exc:
            log.warning(
                "cemu: could not list the save tree at %s, the dump may be incomplete: %s",
                SAVE_DIR,
                exc,
            )
        return selected

    def save_and_exit(self, slot: int) -> dict[str, Any]:
        """Stop Cemu and mark this session's title saves for the dump.

        Args:
            slot: Ignored; there are no save states.

        Returns:
            `state_saved`, `state_slot` and `state_file`, all None.
        """
        self.stop()
        # The dump ships files newer than the session baseline. A save is a
        # directory tree the game rewrites only partially, so refresh every
        # mtime in this session's title save dirs: they ship whole, other
        # titles' saves stay filtered out.
        now = time.time()
        for d in self._modified_title_saves():
            for p in d.rglob("*"):
                if p.is_file():
                    try:
                        os.utime(p, (now, now))
                    except OSError as exc:
                        log.warning("could not restamp %s, may be dropped from the dump: %s", p, exc)
        return {"state_saved": None, "state_slot": None, "state_file": None}
