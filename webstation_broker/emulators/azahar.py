"""Azahar (Nintendo 3DS) launcher: qt-config.ini patching and SIGTERM shutdown.

Azahar has no control API reachable from outside the process. Its only
network surface is a UDP RPC server that reads and writes emulated memory,
with nothing for state or shutdown, and its command line takes a ROM path and
little else. So there is no mid-session save state here: persistence is the
game's own save data, written to host files under the emulated SD card and
NAND. Azahar installs no SIGTERM handler, so the stop is a hard kill and the
dump takes whatever the game had already committed to disk.

The 3DS data root is XDG-derived (`$XDG_DATA_HOME/azahar-emu`), with config
alongside it under `$XDG_CONFIG_HOME`. Azahar also has a portable mode that
triggers on a `user/` directory in the *working* directory, which is why the
launch never chdir's anywhere it might find one.

Save data paths are stable across containers: the console and SD card ids
Azahar files saves under are hardcoded to all-zeros rather than generated per
install, so a save dumped in one session restores into the next.
"""

import configparser
import logging
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Library root a resolved ROM must live under (env `ROM_ROOT`, default `/romm`)."""


def _xdg_dir(var: str, fallback: str) -> str:
    """One of Azahar's XDG directories.

    Azahar's Linux layout: one `azahar-emu` directory per XDG root.

    Args:
        var: The XDG environment variable to honour when set to an absolute path.
        fallback: The path under `$HOME` used otherwise, such as `.config`.

    Returns:
        The `azahar-emu` directory under the chosen root.
    """
    xdg = os.environ.get(var)
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "azahar-emu")
    return os.path.join(os.environ.get("HOME", "/config"), fallback, "azahar-emu")


USER_DIR = Path(os.environ.get("AZAHAR_USER_DIR", _xdg_dir("XDG_DATA_HOME", ".local/share")))
"""Azahar's data root, the save root (env `AZAHAR_USER_DIR`, default `$XDG_DATA_HOME/azahar-emu`)."""
CONFIG_DIR = Path(os.environ.get("AZAHAR_CONFIG_DIR", _xdg_dir("XDG_CONFIG_HOME", ".config")))
"""Azahar's config directory (env `AZAHAR_CONFIG_DIR`, default `$XDG_CONFIG_HOME/azahar-emu`)."""
CONFIG_PATH = CONFIG_DIR / "qt-config.ini"
"""The qt-config.ini patched before every launch."""
AZAHAR_LOG_PATH = Path(os.environ.get("AZAHAR_LOG_PATH", "/config/azahar.log"))
"""The emulator log file (env `AZAHAR_LOG_PATH`, default `/config/azahar.log`)."""

SYSTEM_ID = "0" * 32
"""The console id Azahar files saves under.

Fixed all-zeros rather than generated per install, so these paths are the
same in every container and a dumped save restores where the next session
looks.
"""
SDCARD_ID = "0" * 32
"""The SD card id Azahar files saves under, fixed all-zeros like `SYSTEM_ID`."""

SDMC_DIR = USER_DIR / "sdmc" / "Nintendo 3DS" / SYSTEM_ID / SDCARD_ID
"""The emulated SD card's per-console directory."""
NAND_DATA_DIR = USER_DIR / "nand" / "data" / SYSTEM_ID
"""The emulated NAND's per-console data directory."""

_SAVE_GROUP_ROOTS = (
    SDMC_DIR / "title",
    SDMC_DIR / "extdata",
    NAND_DATA_DIR / "extdata",
    NAND_DATA_DIR / "sysdata",
)
"""Roots holding save trees, each keyed `<titleHigh>/<titleLow>`.

SD title saves are where a game's own saves land; extdata carries the larger
side data some games keep (photos, downloaded content), and sysdata the
system's own.
"""

ROM_EXTENSIONS = (
    ".3ds", ".cci", ".zcci", ".cxi", ".zcxi", ".app",
    ".3dsx", ".z3dsx", ".elf", ".axf", ".bin",
)
"""Formats Azahar boots directly, best first; a folder holding several candidates picks by this order.

No .cia: that is an installable package rather than something the emulator
boots.
"""
_ROM_SEARCH_GLOBS = ("*", "*/*")
"""Glob patterns a ROM folder is searched with; a library folder wrapping a game adds a level."""
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)
"""Matches update and DLC names: they sit beside base games in library folders, and the base game boots."""

_HEX8_RE = re.compile(r"^[0-9a-fA-F]{8}$")
"""Matches a title id half: title dirs under a save group root are `<titleHigh>/<titleLow>`, both 8 hex."""

_CONFIG_PATCHES: dict[str, dict[str, str]] = {
    "UI": {
        "confirmClose": "false",
        "check_for_update_on_start": "false",
        "enable_discord_presence": "false",
    },
}
"""qt-config.ini keys forced before every launch, by ini section.

Only the ones that would otherwise put something in front of the game: two
that phone home and prompt, and the close confirmation that would leave a
modal behind in a session nobody can click.
"""


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


def _patch_config() -> None:
    """Force broker-required qt-config.ini values before every launch.

    Patched key-wise, and with raw parsing, so everything else in the file
    survives: Azahar rewrites this whole file on exit, and it holds the
    player's own settings alongside QSettings-encoded keys that would not
    round-trip through interpolation.

    Raises:
        OSError: When the file cannot be written or replaced.
        configparser.Error: When the patched values cannot be serialized.
        UnicodeError: When the merged contents cannot be re-encoded as UTF-8.

    All three are fatal to a launch rather than a warning to launch past:
    an unpatched config leaves `confirmClose` on, so the close the broker
    drives ends at a confirmation modal in a session nobody can click, and
    the update check and Discord presence put their own dialogs in front of
    the game.
    """
    try:
        parser = configparser.RawConfigParser()
        # Keys are case-sensitive here; the default would fold them and write
        # back a second, lowercased copy of every setting Azahar wrote.
        parser.optionxform = str
        if CONFIG_PATH.exists():
            try:
                parser.read(CONFIG_PATH, encoding="utf-8")
            except (configparser.Error, UnicodeDecodeError) as exc:
                log.warning("qt-config.ini unreadable (%s), reseeding it", exc)
                parser = configparser.RawConfigParser()
                parser.optionxform = str
        for section, entries in _CONFIG_PATCHES.items():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in entries.items():
                parser.set(section, key, value)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            # QSettings writes `key=value`; keep that shape so a player
            # reading the file sees one style throughout.
            parser.write(fh, space_around_delimiters=False)
        tmp.replace(CONFIG_PATH)
    except (OSError, configparser.Error, UnicodeError):
        log.exception("azahar: qt-config.ini patch failed at %s, refusing to launch", CONFIG_PATH)
        raise


class Azahar(Emulator):
    """Nintendo 3DS via Azahar, driven by command line flags and config file patching.

    Azahar has no control API reachable from outside the process, so the
    broker pins qt-config.ini before every launch (no close confirmation, no
    update check, no Discord presence) and boots windowed with `-w`. A patch
    that fails aborts the launch: those keys are the only thing keeping a
    modal nobody can click out of the session.
    Fullscreen Azahar stops rendering when the display resizes under it,
    which happens whenever the player resizes their browser. Azahar installs
    no SIGTERM handler, so the stop is a hard kill and the dump takes
    whatever the game had already committed to disk.

    There are no reachable save states: persistence is the game's own save
    data under the emulated SD card and NAND, and the archive is scoped to
    those save trees. At exit every file in the title save dirs written
    during the session gets its mtime refreshed so the delta dump ships
    those saves whole while other titles' saves stay filtered out. A resume
    slot is logged and ignored.

    Attributes:
        name: Provider key, `azahar`.
        display_name: Human-readable name.
        save_root: The data root the save subtrees hang off.
        save_subtrees: The SD title and extdata trees plus the NAND extdata and sysdata trees.
        rom_extensions: Bootable formats, best first.
        log_path: The emulator log file.
        term_timeout: SIGTERM grace before SIGKILL (env `AZAHAR_STOP_WAIT`, default 5).
    """

    name = "azahar"
    display_name = "Azahar"
    save_root = USER_DIR
    save_subtrees = (
        f"sdmc/Nintendo 3DS/{SYSTEM_ID}/{SDCARD_ID}/title",
        f"sdmc/Nintendo 3DS/{SYSTEM_ID}/{SDCARD_ID}/extdata",
        f"nand/data/{SYSTEM_ID}/extdata",
        f"nand/data/{SYSTEM_ID}/sysdata",
    )
    """The save trees under the data root.

    Scoped to the save trees: the rest of the data root is config, cache,
    shaders and system titles, none of which belong in a save archive.
    """
    rom_extensions = ROM_EXTENSIONS
    log_path = AZAHAR_LOG_PATH
    term_timeout = float(os.environ.get("AZAHAR_STOP_WAIT", "5"))
    """SIGTERM grace before SIGKILL (env `AZAHAR_STOP_WAIT`, default 5).

    No SIGTERM handler: the default action ends the process at once, and
    whatever the game committed is already on disk. The grace window only
    covers process-group teardown.
    """

    def __init__(self) -> None:
        """Initialise the process handle and the session baseline."""
        super().__init__()
        self._session_start = float("inf")
        """Unix time `launch` started the emulator at; infinity until it does.

        Infinity rather than zero so an instance whose launch never completed
        matches no file at all. A baseline of zero is newer than nothing, so
        every title in the container would read as touched this session and
        `save_and_exit` would restamp and ship all of them.
        """

    def prepare_restore(self) -> None:
        """Stop a running Azahar so the archive can be extracted under it."""
        self.stop()

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The file Azahar should boot for `path`.

        Args:
            path: A ROM file, or a folder searched up to two levels deep.

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
            except OSError as exc:
                # One unreadable subdirectory must not discard what the other
                # patterns already found and report the title as unbootable.
                log.warning("azahar: search of %s for %s failed: %s", path, pattern, exc)
        return _pick_rom_file(candidates, path)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Patch qt-config.ini and boot the game windowed.

        Args:
            rom_path: The file to boot.
            resume_slot: Ignored with a log line; there are no reachable save states.

        Raises:
            OSError: When qt-config.ini could not be patched, so nothing is
                spawned; see `_patch_config`.
            configparser.Error: Likewise, when the patched values cannot be serialized.
            UnicodeError: Likewise, when the merged contents cannot be encoded.
        """
        self.stop()
        _patch_config()
        if resume_slot is not None:
            log.info(
                "azahar has no reachable save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        self._session_start = time.time()
        binary = os.environ.get("AZAHAR_BIN", "/opt/azahar/AppRun")
        # Windowed, not fullscreen: a fullscreen Azahar stops rendering the
        # moment the display resizes under it, and the display resizes
        # whenever the player resizes their browser window.
        cmd = [binary, "-w", str(rom_path)]
        log.info("launching azahar (rom=%s)", rom_path)
        self._spawn(cmd, base_launch_env())

    def _modified_title_saves(self) -> list[Path]:
        """Title save dirs holding a file written while the session ran.

        Returns:
            The `<titleHigh>/<titleLow>` directories under every save group
            root touched since launch.
        """
        selected: list[Path] = []
        for root in _SAVE_GROUP_ROOTS:
            if not root.is_dir():
                continue
            try:
                for high in sorted(root.iterdir()):
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
                                "azahar: could not scan the title save dir at %s, "
                                "its saves may be dropped from the dump: %s",
                                title,
                                exc,
                            )
            except OSError as exc:
                log.warning(
                    "azahar: could not list the save tree at %s, the dump may be incomplete: %s",
                    root,
                    exc,
                )
        return selected

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Stop Azahar and mark this session's title saves for the dump.

        Args:
            slot: Ignored, including None; there are no save states to write,
                and the save-data restamp is what an exit does either way.

        Returns:
            `state_saved`, `state_slot` and `state_file`, all None.
        """
        self.stop()
        # The dump ships files newer than the session baseline. A 3DS save is
        # a directory tree the game rewrites only partially, so refresh every
        # mtime in this session's title save dirs: they ship whole, other
        # titles' saves stay filtered out.
        now = time.time()
        for d in self._modified_title_saves():
            try:
                for p in d.rglob("*"):
                    if p.is_file():
                        try:
                            os.utime(p, (now, now))
                        except OSError as exc:
                            log.warning("could not restamp %s, may be dropped from the dump: %s", p, exc)
            except OSError as exc:
                log.warning("could not walk %s, save dump may be incomplete: %s", d, exc)
        return {"state_saved": None, "state_slot": None, "state_file": None}
