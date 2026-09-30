"""ScummVM launcher: folder ROMs, ini pinning, and GMM-driven save states.

ScummVM has no control socket and no state hotkeys, so the broker drives it the
way a player does. A game is a folder of data files rather than a single image:
`scummvm --add --path=<folder>` registers it in scummvm.ini under a generated
*target* name (`monkey`, `gob1-cd-fr`), and that target, not the path, is what
boots the game and what every save file is named after. The exit code of
`--add` is not a detection signal (it returns success having added nothing), so
the ini is read back afterwards and a folder with no domain is what "not
bootable" means here.

Save and load go through the Global Main Menu with xdotool: the menu key opens
it, the bare-letter Save/Load hotkey picks the button, `Down` walks the chooser
to the slot and `Return` activates it. Three things are pinned in scummvm.ini
for that to work at all. The chooser is forced to list mode, because the
default grid has no keyboard path to a numbered slot. The menu key is bound on
the global keymap, because ScummVM's own `C+F5` loses its modifier on the way
through this container's Xwayland and the unmodified `F5` belongs to the engine
keymap, which an engine may take for itself (see `MENU_KEY`). The button
letters follow the GUI translation, so they are read back from `gui_language`.
The macro is silent about its result, so a save is only reported once the
slot's file has actually changed on disk.

ScummVM's saves *are* its states: there is no separate state format, so the
save archive and the working slot both come out of the same directory and
`save_file_kind` tells them apart by filename. Slot 0 is ScummVM's autosave and
its own chooser marks it write protected, so the broker never saves there; it
works in `STATE_SLOT` (default 1) and, like every launcher here, resolves
whatever slot RomM asks for to that one.

Two more ini settings are pinned for the stream rather than for ScummVM's own
sake. `fullscreen`, pinned off whatever the launch, because going fullscreen
makes SDL grab and confine the pointer against a stack that feeds it
absolutely; the window is grown to the display afterwards instead, which SDL
reads as an ordinary resize (`SCUMMVM_FILL_SCREEN`). `gfx_mode=surfacesdl`
because
the OpenGL renderer scales mouse coordinates through `getSdlDpiScalingFactor`
(backends/platform/sdl/sdl-window.cpp), which divides by
`SDL_GL_GetDrawableSize` and only means anything for a GL window, so against an
injected absolute pointer the game takes clicks while the cursor stops moving.
Surface SDL has no such path and is ScummVM's own default.
"""

import logging
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from threading import Thread
from typing import Any, Optional, Union

from .. import imports, settings
from . import extraction_cache
from .base import Emulator, base_launch_env
from .extraction_cache import ExtractionCache

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Root of the RomM library mount (env `ROM_ROOT`, default `/romm`).

A resolved game folder must sit under it; anything resolving outside is discarded.
"""

CONFIG_DIR = Path(os.environ.get("SCUMMVM_CONFIG_DIR", "/config/.config/scummvm"))
"""ScummVM's config directory (env `SCUMMVM_CONFIG_DIR`, default `/config/.config/scummvm`).

Safe to move, because every ScummVM the broker runs is told where it is:
`--config` names `INI_PATH` on the launch command line and on `--add`'s.
Without that the knob would move only the file the broker patches while
ScummVM kept resolving its own, so the pinned settings and the registered
game targets would land in a file nothing opens.
"""
INI_PATH = CONFIG_DIR / "scummvm.ini"
"""The config the broker pins before every launch and reads game targets back out of."""
DATA_DIR = Path(os.environ.get("SCUMMVM_DATA_DIR", "/config/.local/share/scummvm"))
"""ScummVM's data directory (env `SCUMMVM_DATA_DIR`, default `/config/.local/share/scummvm`)."""
SAVE_DIR = DATA_DIR / "saves"
"""Where ScummVM writes every save, which is also where the working slot lives."""
SCUMMVM_LOG_PATH = Path(os.environ.get("SCUMMVM_LOG_PATH", "/config/scummvm.log"))
"""Log file the broker appends this emulator's output to (env `SCUMMVM_LOG_PATH`)."""
CACHE_DIR = Path(os.environ.get("SCUMMVM_CACHE_DIR", str(DATA_DIR / "extracted")))
"""Where archived games are extracted and booted from (env `SCUMMVM_CACHE_DIR`, default `DATA_DIR/extracted`).

An extraction is kept and reused by every later launch of the same archive,
and its folder is what gets registered in scummvm.ini, so the target and the
saves named after it stay the same from one launch to the next.
"""
CACHE_MAX_GB = float(os.environ.get("SCUMMVM_CACHE_MAX_GB", "10"))
"""Cap on the extraction cache in GB (env `SCUMMVM_CACHE_MAX_GB`, default 10).

The least recently booted games are evicted to make room for a new one.
"""
_ARCHIVE_EXTS = extraction_cache._ARCHIVE_EXTS
"""Archive formats an archived game can come in: `.7z`, `.zip` and `.rar`."""
_MARKER_EXTS = (".scummvm",)
"""The marker file some libraries put in a game folder, the one loose file a ROM may point at."""
_WRAPPER_DEPTH = 4
"""How many lone wrapper folders an extraction is walked down through to reach the game."""
ARCHIVE_LIST_TIMEOUT = 60.0
"""Seconds `7z` or `unrar` gets to list an archive's members before it is considered hung."""

AUTOSAVE_SLOT = 0
"""ScummVM's own autosave slot.

`MetaEngine::getAutosaveSlot` returns 0 and `MetaEngine::listSaves` marks it
`setWriteProtectedFlag(true)`, so the save chooser refuses to write there. It is
never the working slot, and a save request that names it lands in `STATE_SLOT`
like any other.
"""
STATE_SLOT = int(os.environ.get("SCUMMVM_STATE_SLOT", "1"))
"""The one slot the broker saves into (env `SCUMMVM_STATE_SLOT`, default `1`).

Low on purpose: the chooser is walked with one `Down` per slot, so a high slot
would spend a keystroke per row getting there. Slot 0 is unavailable (see
`AUTOSAVE_SLOT`), which makes 1 the first usable row.
"""
STATE_WAIT = float(os.environ.get("SCUMMVM_STATE_WAIT", "10"))
"""Seconds to wait for the save macro's write to land (env `SCUMMVM_STATE_WAIT`)."""
STATE_STABLE = float(os.environ.get("SCUMMVM_STATE_STABLE", "1.0"))
"""Seconds size and mtime must both hold still before a save counts as written.

From env `SCUMMVM_STATE_STABLE`, default 1.0. Long enough that a write still
in progress on a loaded host does not read as a finished one.
"""
KEY_DELAY = float(os.environ.get("SCUMMVM_KEY_DELAY", "0.8"))
"""Seconds between the macro's steps (env `SCUMMVM_KEY_DELAY`).

The GMM animates itself in and its chooser fades in after it, and a keystroke
sent into either transition is dropped without a trace.
"""
ADD_TIMEOUT = float(os.environ.get("SCUMMVM_ADD_TIMEOUT", "120"))
"""Seconds `scummvm --add` gets to scan a folder (env `SCUMMVM_ADD_TIMEOUT`).

Generous because detection reads through every file in the folder, which on a
network mount holding a CD rip is not fast.
"""
RESUME_LOAD_WAIT = float(os.environ.get("SCUMMVM_RESUME_LOAD_WAIT", "45"))
"""Seconds a deferred resume waits for RomM to push its state (env `SCUMMVM_RESUME_LOAD_WAIT`)."""
RESUME_LOAD_SETTLE = float(os.environ.get("SCUMMVM_RESUME_LOAD_SETTLE", "8"))
"""Seconds a deferred resume gives the game to reach a menu-able state (env `SCUMMVM_RESUME_LOAD_SETTLE`)."""
FILL_SCREEN = os.environ.get("SCUMMVM_FILL_SCREEN", "true").lower() not in (
    "false",
    "0",
    "no",
    "off",
)
"""Whether the game is grown to fill the stream (env `SCUMMVM_FILL_SCREEN`, default on).

A ScummVM window is its game's own resolution, 640x480 for most, which is a
postage stamp in the middle of the stream. The obvious fix is ScummVM's own
fullscreen, and it is the wrong one: going fullscreen makes SDL grab the
pointer and confine it to a rect (`SdlWindow::createOrUpdateWindow`:
`shouldGrab = ... || fullscreenFlags`, then `SDL_SetWindowMouseRect`) and ask
for an explicit display mode on the way in. Both are harmless on a real X
server and both fight a streaming stack that feeds an absolute pointer and owns
the display size itself: the pointer stops tracking while clicks still land,
and these games are nothing but mouse. `fullscreen` is therefore pinned off in
the ini whatever this setting says.

What works is asking the window manager for the size instead. The window is
resized to the display after launch, SDL sees an ordinary resize and scales its
output into it, letterboxing to keep the game's aspect on its own, and never
sets the flag that triggers the grab. The cost is the title bar, which no tool
in this image can remove (xdotool 3.2016 has no `windowstate`, and there is no
wmctrl); a labwc window rule in the image would.

`window_maximized` is deliberately not used either: labwc leaves the window at
its own size regardless, so it fills nothing.
"""

FILL_SCREEN_WAIT = float(os.environ.get("SCUMMVM_FILL_SCREEN_WAIT", "10"))
"""Seconds to wait for the game window before giving up on resizing it."""

FILL_SCREEN_POLL = float(os.environ.get("SCUMMVM_FILL_SCREEN_POLL", "2"))
"""Seconds between checks that the window still matches the display.

The display is resized by whichever client is connected, so the window has to
follow it rather than being sized once at launch.
"""

_XDOTOOL = os.environ.get("XDOTOOL_BIN", "xdotool")
"""The xdotool binary that drives the GMM (env `XDOTOOL_BIN`)."""

MENU_KEY = os.environ.get("SCUMMVM_MENU_KEY", "F11")
"""Key that opens the Global Main Menu (env `SCUMMVM_MENU_KEY`, default `F11`).

Bound to the *global* keymap in `scummvm.ini` on every launch rather than
trusting a default, for two reasons. ScummVM ships the global menu on `C+F5`,
and a modifier does not survive injection into this container's Xwayland: the
key arrives without its Ctrl, so the menu never opens. The unmodified `F5` that
does work belongs to the engine keymap, which an engine is free to take for
itself (gob answers it with Gobliiins' own panel, which has no Save). Pinning
one unmodified key on the global keymap avoids both.
"""

_GMM_HOTKEYS_DEFAULT = ("s", "l")
"""The `~S~ave` and `~L~oad` hotkeys of ScummVM's untranslated GUI."""

_GMM_HOTKEYS = {
    "be": ("\u0437", "\u0430"),
    "ca": ("d", "c"),
    "cs": ("u", "n"),
    "da": ("g", "n"),
    "de": ("s", "l"),
    "el": ("\u03b1", "\u03c6"),
    "es": ("g", "c"),
    "eu": ("g", "k"),
    "fi": ("t", "l"),
    "fr": ("s", "c"),
    "he": ("\u05e9", "\u05d8"),
    "it": ("s", "c"),
    "nb": ("l", "\u00e5"),
    "pl": ("z", "w"),
    "pt": ("g", "c"),
    "ru": ("\u0430", "\u0437"),
    "tr": ("k", "y"),
}
"""GUI language to its `(save, load)` GMM button hotkeys.

The buttons take their keyboard shortcut from the `~X~` markup in the
translated label (`~S~ave` becomes `~S~auvegarder`, `~L~oad` becomes
`~C~harger`), so the letter that presses them follows `gui_language`. Taken
from ScummVM's po files; a language that keeps the English letters, or leaves
the labels untranslated, falls through to `_GMM_HOTKEYS_DEFAULT`. A few
translations drop the markup altogether (ar, hi, ro, zh) and uk gives both
buttons the same letter, so those have no reliable keyboard path and the macro
reports the failure its timeout finds.
"""

_INI_PINS = {
    "gui_saveload_chooser": "list",
    "gfx_mode": "surfacesdl",
}
"""`[scummvm]` settings written before every launch, whatever the file already says.

`gui_saveload_chooser` because the macros walk a list and the default grid
chooser has no keyboard path to a numbered slot. `gfx_mode` because ScummVM's
OpenGL renderer mis-scales the pointer, explained in the module docstring.
`savepath` and `fullscreen` depend on where the saves live and on the grab
ScummVM's own fullscreen would cost, so `_pins` adds them on top of these.
"""

_SAVE_NAME_RE = re.compile(r"(?P<stem>[^/\\.]+)\.(?P<ext>s\d{2,3}|\d{3})", re.ASCII)
"""A ScummVM save filename: the target, then the slot as `.sNN` or `.NNN`.

Which of the two forms an engine writes is the engine's business, so both are
recognised and a pushed state keeps the form it arrived in.
"""

_BIN_CANDIDATES = ("/usr/games/scummvm", "/usr/bin/scummvm", "/usr/local/bin/scummvm")
"""Where the container's scummvm package puts the binary, best first."""

SCUMMVM_LANGUAGES = {
    "ar", "bg", "ca", "zh", "cn", "tw", "hr", "cs", "da", "nl", "en", "gb", "us",
    "et", "fi", "be", "fr", "fr-ca", "de", "el", "he", "hu", "it", "ja", "ko",
    "lt", "lv", "nb", "fa", "pl", "br", "pt", "ru", "sr", "sk", "es", "eu", "sv",
    "tr", "uk",
}
"""The language codes ScummVM itself accepts (`Common::parseLanguage`).

Not ISO-639: ScummVM keeps a few of its own spellings (Brazilian Portuguese is
`br`, Chinese splits into `zh`/`cn`/`tw`), so a code from outside has to be
translated before it means anything on the command line.
"""

_LANGUAGE_ALIASES = {
    # Obsolete ScummVM codes, still parsed by it, mapped to current spellings.
    "cz": "cs", "gr": "el", "hb": "he", "jp": "ja", "kr": "ko",
    "nz": "zh", "se": "sv", "zh-cn": "cn",
    # ISO-639 spellings ScummVM writes differently.
    "no": "nb",
    "pt-br": "br", "pt_br": "br", "ptbr": "br",
    "zh-hans": "cn", "zh-hant": "tw", "zh-tw": "tw", "zh_tw": "tw",
}
"""Codes a caller may send, mapped to the spelling ScummVM expects."""

_LANGUAGE_FAMILIES = (
    {"en", "gb", "us"},
    {"fr", "fr-ca"},
    {"pt", "br"},
    {"zh", "cn", "tw"},
)
"""Codes close enough that one stands in for another when the exact one is absent.

A `us` variant answers an `en` request far better than a `de` one does.
"""


def normalize_language(raw: Optional[str]) -> Optional[str]:
    """Reduce a caller's language to the code ScummVM accepts, or None.

    Args:
        raw: The language as the activate payload carried it.

    Returns:
        A code from `SCUMMVM_LANGUAGES`, or None for absent, empty or
        unrecognised input. None means "no preference": the game boots the way
        it would have without a language at all, rather than failing over a
        code nobody can act on.
    """
    if not isinstance(raw, str):
        return None
    lang = raw.strip().lower().replace("_", "-")
    if not lang:
        return None
    lang = _LANGUAGE_ALIASES.get(lang, lang)
    if lang in SCUMMVM_LANGUAGES:
        return lang
    # A locale tag whose region ScummVM makes nothing of ("fr-fr", "en-gb")
    # still names a language it knows, so only the region is dropped. The tags
    # whose region does mean something (`pt-br`, `zh-tw`) were translated above.
    base = lang.partition("-")[0]
    return base if base in SCUMMVM_LANGUAGES else None


def _language_rank(keys: dict[str, str], language: Optional[str]) -> int:
    """How well a registered variant fits the language asked for.

    Args:
        keys: The domain's ini keys, whose `language` is what detection found.
        language: The wanted code, already normalized, or None.

    Returns:
        0 for an exact match, 1 for the same family, 2 for no preference
        either way, 3 for an outright mismatch. Sorting by `(rank, name)`
        keeps the pick stable across relaunches, which matters because the
        save files are named after it.
    """
    if not language:
        return 2
    domain_lang = str(keys.get("language", "")).strip().lower()
    if not domain_lang:
        return 2
    if domain_lang == language:
        return 0
    if any(domain_lang in family and language in family for family in _LANGUAGE_FAMILIES):
        return 1
    return 3

_ALREADY_ADDED_RE = re.compile(
    r"Found\s+[\w-]+:(?P<gameid>[\w.-]+), but has already been added"
)
"""`--add` reporting that this game is registered under some other path.

Detection deduplicates by game, not by folder, so a domain left behind by a
library that has since moved makes the same game unregisterable at its new
path forever: `--add` skips it, no domain appears, and the launch has nothing
to boot. Parsing the game out of that line is what lets `register_target`
clear the one dead domain standing in the way.
"""


def scummvm_bin() -> str:
    """The ScummVM binary to run.

    Returns:
        `SCUMMVM_BIN` when set, else the first of `_BIN_CANDIDATES` that
        exists, else the bare name for `PATH` to resolve.
    """
    override = os.environ.get("SCUMMVM_BIN")
    if override:
        return override
    for candidate in _BIN_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return "scummvm"


def _ini_domains() -> dict[str, dict[str, str]]:
    """Parse scummvm.ini into its sections.

    Hand-parsed rather than handed to configparser: ScummVM's own writer is
    what produces this file, and a game domain name can carry characters
    configparser's stricter grammar rejects.

    Returns:
        Section name to its key/value pairs, empty when the file is missing or
        unreadable.
    """
    domains: dict[str, dict[str, str]] = {}
    try:
        lines = INI_PATH.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        log.debug("scummvm: %s does not exist yet", INI_PATH)
        return domains
    except OSError as exc:
        log.error("scummvm: could not read %s: %s", INI_PATH, exc)
        return domains
    section: Optional[str] = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip():
            domains.setdefault(section, {})[key.strip()] = value.strip()
    return domains


def _pins(gui_language: Optional[str] = None) -> dict[str, dict[str, str]]:
    """The settings this launch pins, per ini section.

    Args:
        gui_language: The interface language to pin, or None to leave whatever
            the file says. Absent rather than empty, because writing an empty
            value would override the user's own setting with nothing.

    Returns:
        Section name to the keys pinned in it: `_INI_PINS` plus the save
        directory and `fullscreen=false` under `[scummvm]`, and the menu
        binding the macros depend on under `[keymapper]`. `fullscreen` is
        never true: see `FILL_SCREEN` for the grab it would cost.
    """
    app = {**_INI_PINS, "savepath": str(SAVE_DIR), "fullscreen": "false"}
    if gui_language:
        app["gui_language"] = gui_language
    return {"scummvm": app, "keymapper": {"keymap_global_MENU": MENU_KEY}}


def _write_ini(text: str) -> None:
    """Replace scummvm.ini with `text`, all of it or none of it.

    Written to a sibling and renamed over the original: a plain write truncates
    the file first, so anything that interrupts it (the container stopping, the
    disk filling) leaves a half written ini behind. ScummVM reads that file to
    find `savepath`, and one that stops short of it sends the next session's
    saves to the default directory, where the dump does not look for them.

    A trailing newline is ensured for the same reason every other config the
    broker writes gets one: a final line without it is not always parsed.

    Args:
        text: The full contents of the file.

    Raises:
        OSError: If the file could not be written or renamed into place.
    """
    if not text.endswith("\n"):
        text += "\n"
    tmp = INI_PATH.with_suffix(".ini.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, INI_PATH)
    except OSError:
        # The rename is what publishes the file, so a failure before it leaves
        # the original untouched and the temp file behind.
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("scummvm: could not remove the partial %s: %s", tmp, exc)
        raise


def patch_ini(gui_language: Optional[str] = None) -> None:
    """Write the broker's pinned settings into scummvm.ini.

    Existing keys are rewritten in place and missing ones appended to their
    section, so everything the user set that the broker does not pin survives,
    including any other keymap they have bound. A missing file is created
    holding only the pins, which is enough for ScummVM to start and for `--add`
    to write into.

    Failures are logged and swallowed: a game that boots with the user's own
    settings is worth more than a launch refused over a config file, and the
    macros report their own failure if the chooser or the menu key turn out not
    to be the pinned ones.

    Args:
        gui_language: The interface language to pin, or None to leave the
            file's own. Pinning it is also what makes `gmm_hotkeys` read the
            right letters, since the GMM buttons take their shortcut from the
            translated label.
    """
    pins = _pins(gui_language)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("scummvm: could not create %s: %s, ini not pinned", CONFIG_DIR, exc)
        return

    rendered = "\n".join(
        f"[{section}]\n" + "".join(f"{k}={v}\n" for k, v in keys.items())
        for section, keys in pins.items()
    )
    if not INI_PATH.exists():
        try:
            _write_ini(rendered)
        except OSError as exc:
            log.error("scummvm: could not create %s: %s, ini not pinned", INI_PATH, exc)
        else:
            log.info("scummvm: created %s with the broker's settings", INI_PATH)
        return

    try:
        lines = INI_PATH.read_text(errors="replace").splitlines()
    except OSError as exc:
        log.error("scummvm: could not read %s: %s, ini not pinned", INI_PATH, exc)
        return

    out: list[str] = []
    written: dict[str, set[str]] = {section: set() for section in pins}
    section: Optional[str] = None

    def flush(current: Optional[str]) -> None:
        """Append whatever `current` still owes before the next section starts.

        Args:
            current: The section being left, or None at the top of the file.
        """
        if current not in pins:
            return
        out.extend(f"{k}={v}" for k, v in pins[current].items() if k not in written[current])
        written[current].update(pins[current])

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            flush(section)
            section = stripped[1:-1]
            out.append(line)
            continue
        key = line.partition("=")[0].strip()
        if section in pins and key in pins[section]:
            if key not in written[section]:
                out.append(f"{key}={pins[section][key]}")
                written[section].add(key)
            continue
        out.append(line)
    flush(section)

    for name, keys in pins.items():
        missing = {k: v for k, v in keys.items() if k not in written[name]}
        if missing:
            out.append(f"[{name}]")
            out.extend(f"{k}={v}" for k, v in missing.items())

    try:
        _write_ini("\n".join(out) + "\n")
    except OSError as exc:
        log.error("scummvm: could not write %s: %s, ini not pinned", INI_PATH, exc)
    else:
        log.debug("scummvm: patched %s with the broker's settings", INI_PATH)


def gmm_hotkeys() -> tuple[str, str]:
    """The `(save, load)` GMM button hotkeys for the configured GUI language.

    Returns:
        The pair from `_GMM_HOTKEYS`, or `_GMM_HOTKEYS_DEFAULT` when the GUI
        language is unset or keeps the English letters.
    """
    lang = _ini_domains().get("scummvm", {}).get("gui_language", "").strip().lower()
    return _GMM_HOTKEYS.get(lang, _GMM_HOTKEYS_DEFAULT)


def _game_domains() -> dict[str, dict[str, str]]:
    """The sections of scummvm.ini that describe a registered game.

    Returns:
        Domain name to its keys, for domains carrying both a path and a
        gameid/engineid. That pair is what separates a game from the
        `[scummvm]` application section and the keymap sections.
    """
    return {
        name: keys
        for name, keys in _ini_domains().items()
        if "path" in keys and ("gameid" in keys or "engineid" in keys)
    }


def _domains_at(rom_dir: Path) -> dict[str, dict[str, str]]:
    """The game domains registered for `rom_dir`, in ini order.

    Args:
        rom_dir: The game folder to look up.

    Returns:
        Domain name to its keys, for every domain whose `path` is `rom_dir`.
    """
    found: dict[str, dict[str, str]] = {}
    for name, keys in _game_domains().items():
        try:
            if Path(keys["path"]) == rom_dir:
                found[name] = keys
        except (OSError, ValueError) as exc:
            log.debug("scummvm: skipping domain %s, bad path %r: %s", name, keys.get("path"), exc)
    return found


def target_for_path(rom_dir: Path, language: Optional[str] = None) -> Optional[str]:
    """The target registered in scummvm.ini for `rom_dir`, or None.

    A multilingual folder registers one domain per detected language
    (`gob1-cd-de`, `gob1-cd-fr`), and the domain is what decides which
    variant's resources the engine loads: `--language` alone does not reroute
    a launch. Picking by language is therefore what actually boots the game in
    the language asked for, and without one the name breaks the tie, which
    otherwise hands a French player whichever variant sorts first.

    Args:
        rom_dir: The game folder to look up.
        language: The wanted code, already normalized, or None for no preference.

    Returns:
        The target name, or None when no domain points at that folder.
    """
    domains = _domains_at(rom_dir)
    if not domains:
        return None
    best = min(domains, key=lambda name: (_language_rank(domains[name], language), name))
    if len(domains) > 1:
        log.info(
            "scummvm: %d targets registered for %s, booting %s (language=%s)",
            len(domains),
            rom_dir,
            best,
            language or "-",
        )
    return best


def _run_add(rom_dir: Path) -> Optional[subprocess.CompletedProcess]:
    """Run `scummvm --add` against `rom_dir`.

    Args:
        rom_dir: The game folder to scan.

    Returns:
        The finished process, or None when it could not be run at all.
    """
    # --config, because the domain this writes is only there for the launch to
    # boot if both command lines name the same ini.
    cmd = [scummvm_bin(), f"--config={INI_PATH}", "--add", f"--path={rom_dir}"]
    try:
        result = subprocess.run(
            cmd,
            env=base_launch_env(),
            capture_output=True,
            text=True,
            timeout=ADD_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("scummvm: --add %s failed: %s", rom_dir, exc)
        return None
    if result.returncode != 0:
        log.warning(
            "scummvm: --add %s exited %d: %s",
            rom_dir,
            result.returncode,
            result.stderr.strip()[:400],
        )
    return result


def register_target(rom_dir: Path, language: Optional[str] = None) -> Optional[str]:
    """Register `rom_dir` with `scummvm --add` and read its target back.

    `--add` is idempotent, so a folder already registered costs a detection
    pass and nothing else. Its exit code is not a detection signal: it returns
    success having added nothing, so the ini read is the only honest check.

    Detection deduplicates by game rather than by folder, so a domain left
    behind by a library that has since moved makes the same game skippable at
    its new path. When that is what happened, the dead domain is cleared and
    the scan retried once, which is the difference between a library that can
    be remounted somewhere else and one that can never boot again.

    Args:
        rom_dir: The game folder to register.
        language: The wanted code, already normalized, or None.

    Returns:
        The target ScummVM registered, or None when it detected no game there.
    """
    result = _run_add(rom_dir)
    if result is None:
        return None
    target = target_for_path(rom_dir, language)

    if target is None:
        blocked = _ALREADY_ADDED_RE.search(result.stdout)
        if blocked is not None:
            gameid = blocked.group("gameid")
            log.info(
                "scummvm: %s is registered elsewhere, clearing dead domains for %s",
                rom_dir,
                gameid,
            )
            if _drop_dead_domains(gameid, keep=rom_dir):
                retry = _run_add(rom_dir)
                if retry is not None:
                    result = retry
                    target = target_for_path(rom_dir, language)

    if target is None and "Game Added" in result.stdout:
        # Detection worked and the config flush did not, which is only ever a
        # permission problem on the directory ScummVM writes the ini into.
        # Without this line the symptom is a 422 that blames the ROM folder.
        log.error(
            "scummvm: --add detected a game in %s but %s gained no domain; is %s writable?",
            rom_dir,
            INI_PATH,
            CONFIG_DIR,
        )
    if target is not None:
        log.debug("scummvm: registered %s as target %s", rom_dir, target)
    return target


def _drop_dead_domains(gameid: str, keep: Optional[Path] = None) -> int:
    """Remove `gameid`'s domains whose recorded path no longer exists.

    Only domains that are already dead go: a path that is not there cannot
    boot, and ScummVM's own launcher greys those entries out. Anything still
    on disk is left alone, including another copy of the same game, so a
    library that is merely unmounted keeps its registrations and the save
    files named after them.

    The one exception is an older extraction of the same archive when `keep`
    is itself an extraction. A re-uploaded archive gets a fresh extraction
    while the old one waits for eviction, and the old one's domain would
    otherwise block the new one for as long as it sits there. Dropping it
    frees the target name, so the new extraction registers under the same
    target and the saves named after it still load. An extraction of a
    different archive is another copy of the game and is left alone, as a
    library folder would be: dropping it would hand both copies one target
    and one set of saves.

    Args:
        gameid: The game whose stale domains are in the way.
        keep: The folder being registered, or None.

    Returns:
        How many domains were dropped.
    """
    source = _extraction_source(keep) if keep is not None else None
    doomed = set()
    for name, keys in _game_domains().items():
        if keys.get("gameid") != gameid:
            continue
        try:
            path = Path(keys["path"])
            if not path.is_dir():
                doomed.add(name)
            elif (
                source is not None
                and path.resolve() != keep.resolve()
                and _extraction_source(path) == source
            ):
                doomed.add(name)
        except (OSError, ValueError) as exc:
            log.warning(
                "scummvm: domain %s path unreadable, dropping it as dead: %s", name, exc
            )
            doomed.add(name)
    if not doomed:
        return 0

    try:
        lines = INI_PATH.read_text(errors="replace").splitlines()
    except OSError as exc:
        log.error("scummvm: could not read %s to clear dead domains: %s", INI_PATH, exc)
        return 0

    out: list[str] = []
    dropping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            dropping = stripped[1:-1] in doomed
        if not dropping:
            out.append(line)

    try:
        _write_ini("\n".join(out) + "\n")
    except OSError as exc:
        log.error("scummvm: could not rewrite %s: %s", INI_PATH, exc)
        return 0
    log.info(
        "scummvm: cleared %d dead domain(s) for %s: %s",
        len(doomed),
        gameid,
        ", ".join(sorted(doomed)),
    )
    return len(doomed)


_SOURCE_FILE = ".source"
"""File in an extraction's root naming the archive it came from.

Hidden, so `_extracted_game_dir` never mistakes it for part of the game.
"""


def _extraction_root(path: Path) -> Optional[Path]:
    """The cache entry `path` lies in, or None when it is not in the cache.

    Args:
        path: A folder, typically a registered domain's path.

    Returns:
        The top-level extraction folder under CACHE_DIR.

    Raises:
        OSError: When a path cannot be resolved.
    """
    cache = CACHE_DIR.resolve()
    resolved = path.resolve()
    if resolved == cache or not resolved.is_relative_to(cache):
        return None
    return cache / resolved.relative_to(cache).parts[0]


def _extraction_source(path: Path) -> Optional[str]:
    """The archive the extraction holding `path` came from, as recorded at extraction.

    Args:
        path: A folder inside an extraction.

    Returns:
        The archive's resolved path, or None outside the cache or when the
        extraction carries no record.
    """
    try:
        root = _extraction_root(path)
        if root is None:
            return None
        return (root / _SOURCE_FILE).read_text().strip() or None
    except OSError:
        return None


def _visible_entries(folder: Path) -> list[Path]:
    """`folder`'s entries, minus hidden ones and the `__MACOSX` fork a Mac-made zip carries.

    Args:
        folder: The folder to list.

    Returns:
        The entries that can be part of a game.

    Raises:
        OSError: When the folder cannot be listed.
    """
    return [e for e in folder.iterdir() if not e.name.startswith(".") and e.name != "__MACOSX"]


def _extracted_game_dir(root: Path) -> Optional[Path]:
    """The game folder inside an extracted archive.

    Most zipped games wrap their files in one folder named after the game, and
    `--add` does not recurse, so registering the extraction root would detect
    nothing. A lone real folder is walked into until a level holds anything
    else.

    Args:
        root: Where the archive was extracted to.

    Returns:
        The folder to register, or None when the extraction holds nothing.
    """
    folder = root
    try:
        for _ in range(_WRAPPER_DEPTH):
            entries = _visible_entries(folder)
            if not entries:
                return None
            only = entries[0]
            if len(entries) > 1 or only.is_symlink() or not only.is_dir():
                break
            folder = only
    except OSError as exc:
        log.warning("scummvm: could not read the extraction under %s: %s", root, exc)
        return None
    return folder


def _archive_holds_files(archive: Path) -> bool:
    """Whether an archive's listing names anything that could be part of a game.

    Hidden members and a Mac-made zip's `__MACOSX` fork do not count. A 7z or
    rar listing does not mark folders, so an archive of empty folders still
    passes here and fails at extraction; an empty or junk-only one is refused
    before the launch.

    Args:
        archive: The archive.

    Returns:
        True when a member could be a game file; a refusal is logged.
    """
    try:
        members = extraction_cache.list_members(archive, ARCHIVE_LIST_TIMEOUT)
    except RuntimeError as exc:
        log.warning("scummvm: could not list %s: %s", archive.name, exc)
        return False
    for member in members:
        parts = PurePosixPath(member.replace("\\", "/")).parts
        if member.endswith("/") or not parts:
            continue
        if not any(part.startswith(".") or part == "__MACOSX" for part in parts):
            return True
    log.warning("scummvm: %s holds no game files", archive.name)
    return False


def _lone_archive(folder: Path) -> Optional[Path]:
    """The archive a game folder holds in place of the game's files, or None.

    Only a folder whose sole content, marker files aside, is one archive
    qualifies. An archive beside anything else (a manual, an extras bundle)
    leaves the folder as the game.

    Args:
        folder: The game folder, already resolved.

    Returns:
        The archive, or None when the folder holds anything else.
    """
    try:
        entries = [e for e in _visible_entries(folder) if e.suffix.lower() not in _MARKER_EXTS]
        if len(entries) != 1 or entries[0].suffix.lower() not in _ARCHIVE_EXTS or not entries[0].is_file():
            return None
    except OSError as exc:
        log.warning("scummvm: could not list %s: %s", folder, exc)
        return None
    return entries[0]


_CACHE = ExtractionCache(
    name="scummvm",
    cache_dir=lambda: CACHE_DIR,
    enabled=lambda: settings.SCUMMVM_CACHE_ENABLED,
    max_gb=lambda: CACHE_MAX_GB,
    find_boot_target=_extracted_game_dir,
    missing_target_error="held no game files",
)
"""Extracts archived games into CACHE_DIR, one folder per archive, reused on every later launch."""


def _is_archive(rom: Path) -> bool:
    """Whether `rom` is an archived game rather than a game folder or marker.

    Args:
        rom: What `resolve_rom_file` returned.

    Returns:
        True for an archive file.
    """
    return rom.suffix.lower() in _ARCHIVE_EXTS and rom.is_file()


def _game_dir(rom: Path, emu: Emulator) -> Path:
    """The folder to register for `rom`, extracting an archived game into the cache first.

    Args:
        rom: What `resolve_rom_file` returned.
        emu: The launcher, whose `extraction_phase` reports a first extraction.

    Returns:
        `rom` itself for a folder, or the game folder inside its extraction.

    Raises:
        RuntimeError: When the archive cannot be extracted or holds nothing.
    """
    if not _is_archive(rom):
        return rom
    game = _CACHE.extract(rom, emu)
    # Rewritten on every launch, so a record lost to a failed write comes
    # back. Without it a re-upload's old extraction is indistinguishable from
    # another copy, and blocks the new one.
    try:
        root = _extraction_root(game)
        if root is not None:
            (root / _SOURCE_FILE).write_text(str(rom.resolve()))
    except OSError as exc:
        log.warning("scummvm: could not record the source of %s's extraction: %s", rom.name, exc)
    return game


def _cached_game_dir(rom: Path) -> Optional[Path]:
    """The folder registered for `rom`, without extracting anything.

    Args:
        rom: What `resolve_rom_file` returned.

    Returns:
        `rom` itself for a folder, the game folder of an archive extracted
        earlier, or None for an archive not extracted yet.
    """
    if not _is_archive(rom):
        return rom
    try:
        extracted = CACHE_DIR / extraction_cache._cache_key(rom)
    except RuntimeError:
        return None
    return _extracted_game_dir(extracted) if extracted.is_dir() else None


def sweep_stale_extractions() -> None:
    """Remove extraction scratch dirs orphaned by a crashed broker process.

    Call once at broker startup, so the space is reclaimed before the first
    launch rather than only when the next extraction happens to run.
    """
    try:
        _CACHE.sweep_stale_extractions()
    except RuntimeError as exc:
        log.warning("scummvm cache: startup scratch sweep skipped: %s", exc)


def slot_names(target: str, slot: int) -> tuple[str, ...]:
    """The filenames `target`'s save in `slot` can have.

    Args:
        target: The ScummVM target the game booted under.
        slot: The slot number.

    Returns:
        Both canonical spellings, `<target>.sNN` first.
    """
    return (f"{target}.s{slot:02d}", f"{target}.{slot:03d}")


def slot_file(target: Optional[str], slot: int) -> Optional[Path]:
    """The newest existing save file for `target` in `slot`.

    Args:
        target: The booted target, or None when nothing has booted yet.
        slot: The slot number.

    Returns:
        The file's path, or None when the slot is empty or no target is known.
        Naming a target is what keeps another game's save in the same slot from
        answering in this one's place.
    """
    if target is None:
        return None
    found = []
    for name in slot_names(target, slot):
        path = SAVE_DIR / name
        try:
            found.append((path.stat().st_mtime, path))
        except OSError as exc:
            log.debug("scummvm: skipping %s, could not stat: %s", path, exc)
            continue
    return max(found)[1] if found else None


def _slot_stamp(target: Optional[str], slot: int) -> dict[str, tuple[float, int]]:
    """Size and mtime of every file the slot could be written to.

    Args:
        target: The booted target, or None.
        slot: The slot number.

    Returns:
        Filename to its `(mtime, size)`, skipping names that do not exist.
    """
    stamp: dict[str, tuple[float, int]] = {}
    if target is None:
        return stamp
    for name in slot_names(target, slot):
        path = SAVE_DIR / name
        try:
            st = path.stat()
        except OSError as exc:
            log.debug("scummvm: skipping %s, could not stat: %s", path, exc)
            continue
        stamp[name] = (st.st_mtime, st.st_size)
    return stamp


_IMPORT_NAME_RE = re.compile(r"(?P<stem>[^/\\.]+)\.(?P<ext>s\d{2,3}|\d{3})", re.ASCII | re.IGNORECASE)
"""A declared save's filename, matched without regard to case.

`_SAVE_NAME_RE` stays case-sensitive because it reads what ScummVM wrote. An
import is a stranger's file, and `MONKEY.S02` is as good a save as `monkey.s02`.
"""

_GENERIC_STEM = "savegame"
"""The stem some engines use for a save that names no game, which no target owns."""

_SAVE_EXPECTED = "a single <game>.NNN or <game>.sNN save file"
"""The shape a declared save takes, in words."""

_STATE_EXPECTED = "a single <game>.sNN or <game>.NNN state file"
"""The shape a declared state takes, in words."""


def _wanted_language(emu: Emulator) -> Optional[str]:
    """The language `launch` picks a variant by: the rom's own, else the interface's.

    Args:
        emu: The emulator, carrying the activate payload's languages.

    Returns:
        The normalized code, or None for no preference.
    """
    return normalize_language(emu.language) or normalize_language(emu.gui_language)


def _folder_names(rom_dir: Path) -> list[str]:
    """Every name a save for the game in `rom_dir` can carry its stem as.

    Args:
        rom_dir: The game folder.

    Returns:
        Per domain registered there, in ini order, the target, then its
        `gameid`, then its `engineid`, each spelling once and as the ini has it.
    """
    names: dict[str, None] = {}
    for name, keys in _domains_at(rom_dir).items():
        for spelling in (name, keys.get("gameid"), keys.get("engineid")):
            if spelling:
                names.setdefault(spelling)
    return list(names)


def _match_name(stem: str, names: list[str]) -> Optional[str]:
    """Find the name a save's stem stands for.

    Args:
        stem: The member's stem.
        names: The candidates, from `_folder_names`.

    Returns:
        The identical spelling when there is one, else the first that differs
        only in case, else None.
    """
    if stem in names:
        return stem
    folded = stem.casefold()
    return next((name for name in names if name.casefold() == folded), None)


_NO_GAME_KEY = ("scummvm", "no_game")
"""Memo key for why `_session_game` found no game, when that is not a detection miss."""


def _session_game(emu: Emulator, ctx: imports.ImportCtx) -> tuple[Optional[str], list[str]]:
    """Work out the target the session boots, and every name a save may carry.

    This is what `launch` will do, in the same order: extract an archived
    game, look the folder up, and register it when it is not there. It runs
    once per preflight, and it can take as long as an extraction and a
    detection pass. Registering writes `scummvm.ini`, and extracting fills the
    cache, both of which a refused import leaves behind, exactly as a refused
    launch would; the launch after an accepted one reuses both.

    Args:
        emu: The emulator, carrying the activate payload's languages.
        ctx: The launch context; its `memo` keeps the answer.

    Returns:
        The target and the names, or None and an empty list when there is no
        game folder or ScummVM detects no game in it.
    """
    key = ("scummvm", "game")
    cached = ctx.memo.get(key)
    if isinstance(cached, tuple):
        return cached
    target: Optional[str] = None
    names: list[str] = []
    rom_dir = ctx.rom_file
    if rom_dir is not None:
        try:
            rom_dir = _game_dir(rom_dir, emu)
        except RuntimeError as exc:
            log.warning("scummvm: import preflight could not extract %s: %s", rom_dir, exc)
            ctx.memo[_NO_GAME_KEY] = f"the archived game could not be extracted: {exc}"
            rom_dir = None
    if rom_dir is not None:
        language = _wanted_language(emu)
        target = target_for_path(rom_dir, language)
        if target is None:
            patch_ini(normalize_language(emu.gui_language))
            target = register_target(rom_dir, language)
        if target is not None:
            names = _folder_names(rom_dir)
    game = (target, names)
    ctx.memo[key] = game
    return game


def _split_name(
    member: imports.ImportMember, expected: str
) -> Union[tuple[str, str], imports.ImportRefusal]:
    """Split a declared member into its stem and lower-cased extension.

    Args:
        member: The member.
        expected: The accepted shape, in words.

    Returns:
        The stem and extension, or `unrecognised_layout` when the member is not
        one save file (a bare name, or one under `saves/`) with a ScummVM slot
        extension and a stem that names a game.
    """
    parts = member.parts
    if len(parts) == 2 and parts[0] == "saves":
        parts = parts[1:]
    match = _IMPORT_NAME_RE.fullmatch(parts[0]) if len(parts) == 1 else None
    if match is None or match.group("stem").casefold() == _GENERIC_STEM:
        return imports.ImportRefusal("unrecognised_layout", member.name, expected)
    return match.group("stem"), match.group("ext").lower()


def _game_name(
    member: imports.ImportMember, stem: str, expected: str, names: list[str], ctx: imports.ImportCtx
) -> Union[str, imports.ImportRefusal]:
    """Hold a member's stem to the game the session runs.

    Args:
        member: The member.
        stem: Its stem.
        expected: The accepted shape, in words.
        names: The session's names, from `_session_game`.
        ctx: The launch context, whose memo says why there is no game when
            an archived one would not extract.

    Returns:
        The name to file it under, as the ini spells it, or `identity_unknown`
        when there is no game and `identity_mismatch` when the stem is another's.
    """
    if not names:
        return imports.ImportRefusal(
            "identity_unknown",
            member.name,
            expected,
            detail=ctx.memo.get(_NO_GAME_KEY) or "ScummVM detects no game in this rom folder",
        )
    matched = _match_name(stem, names)
    if matched is None:
        return imports.ImportRefusal(
            "identity_mismatch",
            member.name,
            expected,
            detail=f"member {stem}, this game {', '.join(names)}",
        )
    return matched


def _place_save(
    member: imports.ImportMember, emu: Emulator, ctx: imports.ImportCtx
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place a save under the name of the game it belongs to.

    A save is filed under the spelling the ini has, whatever case the archive
    used, because ScummVM finds a save by exact name. One for another variant
    of the folder keeps that variant's name and stays out of sight until that
    variant boots.

    Args:
        member: The member.
        emu: The emulator.
        ctx: The launch context.

    Returns:
        The placement, or a refusal. The working slot under the booted target's
        name is `destination_conflict`: it is the broker's, so it is declared
        as a state.
    """
    split = _split_name(member, _SAVE_EXPECTED)
    if isinstance(split, imports.ImportRefusal):
        return split
    target, names = _session_game(emu, ctx)
    named = _game_name(member, split[0], _SAVE_EXPECTED, names, ctx)
    if isinstance(named, imports.ImportRefusal):
        return named
    dest = imports.build_dest("saves", (), (f"{named}.{split[1]}",), member=member, expected=_SAVE_EXPECTED)
    if isinstance(dest, imports.ImportRefusal):
        return dest
    if target is not None and dest.name in slot_names(target, STATE_SLOT):
        return imports.ImportRefusal(
            "destination_conflict",
            member.name,
            "a save outside the working slot",
            detail=f"slot {STATE_SLOT} is the broker's working slot; declare it as a state",
        )
    return imports.Placement(member, dest)


def _place_state(
    member: imports.ImportMember, emu: Emulator, ctx: imports.ImportCtx
) -> Union[imports.Placement, imports.ImportRefusal]:
    """Place the state in the working slot, under the target the session boots.

    The member's own slot is dropped, as `state_target` drops it for a pushed
    state, and its extension form is kept. The stem still has to name a game of
    this folder, but any of its variants will do: a state captured under one
    language's target is renamed onto the one this session boots.

    Args:
        member: The member.
        emu: The emulator.
        ctx: The launch context.

    Returns:
        The placement, or a refusal.
    """
    split = _split_name(member, _STATE_EXPECTED)
    if isinstance(split, imports.ImportRefusal):
        return split
    target, names = _session_game(emu, ctx)
    named = _game_name(member, split[0], _STATE_EXPECTED, names, ctx)
    if isinstance(named, imports.ImportRefusal):
        return named
    slot_ext = f"s{STATE_SLOT:02d}" if split[1].startswith("s") else f"{STATE_SLOT:03d}"
    dest = imports.build_dest("saves", (), (f"{target}.{slot_ext}",), member=member, expected=_STATE_EXPECTED)
    if isinstance(dest, imports.ImportRefusal):
        return dest
    return imports.Placement(member, dest)


class Scummvm(Emulator):
    """ScummVM sessions, driven through the launcher's own config and menus.

    A launch registers the game folder with `scummvm --add`, pins the settings
    the macros and the stream depend on, and boots `scummvm <target>`. A resume
    whose state is already on disk goes in on the command line with
    `--save-slot`, which every engine reads in its startup path, so the game
    never shows its title screen first; a state RomM pushes after activate
    returns is delivered by a deferred thread over the GMM instead.

    Saving and loading drive the Global Main Menu with xdotool, and the macro
    is silent, so a save is confirmed by watching the slot's file rather than
    by the keystrokes going out. Engines without runtime save support (gob's
    password-based games are the canonical case) put up a message dialog
    instead: the write never lands, the dialogs are dismissed, and the failure
    is reported rather than left as a game paused behind a menu.

    Saves and states share `saves/`, so `save_file_kind` splits them by name
    rather than by subtree, and the archive carries both.

    A multilingual folder registers one target per language it detects, so the
    language the session was activated for decides which of them boots; without
    one the pick is alphabetical, which hands a French player a German game.

    Declared imports take a save file per member, and one of them as the state.
    The stem has to name a game of the folder, and the target the session boots
    is worked out the way `launch` does, registering the folder when it is new.

    Attributes:
        name: Registry key, `scummvm`.
        display_name: Human-readable name shown in the UI.
        save_root: ScummVM's data directory, which the save subtree hangs off.
        save_subtrees: `saves`, holding the game's saves and the working slot alike.
        state_subtrees: Empty, because states are not in a subtree of their own.
        clears_stale_saves: On; activate empties the save directory.
        rom_extensions: The `.scummvm` marker file some libraries use; a game is a folder.
        supports_states: True, over the Global Main Menu.
        state_slot: The one slot the broker works in, echoed back as the effective slot.
        state_dir: Where ScummVM writes every save.
        log_path: The ScummVM output the broker exposes.
        term_timeout: Seconds SIGTERM gets, which ScummVM spends flushing its config.
    """

    name = "scummvm"
    display_name = "ScummVM"
    save_root = DATA_DIR
    save_subtrees = ("saves",)
    state_subtrees = ()
    """Empty on purpose: ScummVM has no state format, so its states are saves
    living beside the game's own, and `save_file_kind` is what tells them apart."""
    clears_stale_saves = True
    supports_states = True
    state_slot = STATE_SLOT
    state_dir = SAVE_DIR
    log_path = SCUMMVM_LOG_PATH
    term_timeout = float(os.environ.get("SCUMMVM_STOP_WAIT", "10"))

    def __init__(self) -> None:
        """Start with no game folder, no target, and no launch behind it."""
        super().__init__()
        self._rom_dir: Optional[Path] = None
        """What `resolve_rom_file` picked: the folder `launch` registers, or an archive it extracts first."""
        self._target: Optional[str] = None
        """The target the running game booted under, and every save is named after.

        Kept after the process stops: the state routes are read once the game
        is already gone, and they have no other way to name its files.
        """
        self._launch_seq = 0
        """Bumped per launch, so a deferred resume can tell it has been superseded."""

    @property
    def rom_extensions(self) -> tuple[str, ...]:
        """The loose files a ROM may point at: the marker, and archives while the cache is on.

        A ScummVM game is the folder itself, so a marker only stands for the
        folder holding it, and an archive is booted from its extraction.
        """
        if settings.SCUMMVM_CACHE_ENABLED:
            return _MARKER_EXTS + _ARCHIVE_EXTS
        return _MARKER_EXTS

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a RomM path to the game folder to register, or the archive to extract.

        Kept cheap: the detection pass that can actually answer "is there a
        game in here" takes seconds, and extracting an archive can take
        longer, so both run in `launch`. A folder that holds files is accepted
        here and only a folder ScummVM detects nothing in fails, later, as a
        launch failure.

        Args:
            path: The ROM as RomM delivered it: the game folder, a file inside
                it (a `.scummvm` marker, or the single file a library pointed
                at), or an archived game.

        Returns:
            The game folder, or the archive when the game is archived (a loose
            one, or the only thing in its folder). None when the path is a
            file this launcher does not recognise, an archive with the cache
            off, outside the ROM root, not a folder, or holds nothing to
            detect.
        """
        self._rom_dir = None
        suffix = path.suffix.lower()
        if not path.is_dir() and suffix in _ARCHIVE_EXTS:
            return self._resolve_archive(path)
        if not path.is_dir() and suffix not in _MARKER_EXTS:
            # Falling through to `path.parent` here would register whatever
            # directory the file happens to sit in. For a library laid out as
            # <root>/<platform>/<file>, that is the platform folder: `--add`
            # would scan every game in it and boot whichever target sorts
            # first. A wrong game is worse than a refused launch.
            log.warning(
                "scummvm: %s is not a game folder or a %s marker; ScummVM games are folders",
                path,
                " or ".join(_MARKER_EXTS),
            )
            return None
        rom_dir = path if path.is_dir() else path.parent
        try:
            resolved = rom_dir.resolve()
            # Defense in depth: the activate route validates the path it was
            # given, this validates the folder actually about to be registered.
            if not resolved.is_relative_to(ROM_ROOT.resolve()):
                log.warning("scummvm: %s resolves outside %s", rom_dir, ROM_ROOT)
                return None
            if not resolved.is_dir() or not any(resolved.iterdir()):
                log.warning("scummvm: %s is not a folder holding game files", rom_dir)
                return None
        except OSError as exc:
            log.warning("scummvm: could not read %s: %s", rom_dir, exc)
            return None
        archive = _lone_archive(resolved)
        if archive is not None:
            return self._resolve_archive(archive)
        self._rom_dir = resolved
        log.debug("scummvm: resolved rom folder %s", resolved)
        return resolved

    def _resolve_archive(self, archive: Path) -> Optional[Path]:
        """Accept an archived game for `launch` to extract.

        Args:
            archive: The archive, a loose ROM file or the lone file in its folder.

        Returns:
            The resolved archive, or None when the cache is off, it is not a
            file, it resolves outside the ROM root, or its listing names no
            game files.
        """
        if not settings.SCUMMVM_CACHE_ENABLED:
            log.warning(
                "scummvm: refusing %s, an archived game needs the extraction cache "
                "(set SCUMMVM_CACHE_ENABLED=true, or extract it into a folder)",
                archive.name,
            )
            return None
        try:
            resolved = archive.resolve()
            # Defense in depth, as for a folder: this is the file about to be
            # opened and extracted, whatever path the caller handed over.
            if not resolved.is_relative_to(ROM_ROOT.resolve()):
                log.warning("scummvm: %s resolves outside %s", archive, ROM_ROOT)
                return None
            if not resolved.is_file():
                log.warning("scummvm: %s is not a file", archive)
                return None
        except OSError as exc:
            log.warning("scummvm: could not read %s: %s", archive, exc)
            return None
        if not _archive_holds_files(resolved):
            return None
        self._rom_dir = resolved
        log.debug("scummvm: resolved archived game %s", resolved)
        return resolved

    def _xdotool(self, *args: str, quiet: bool = False) -> Optional[str]:
        """Run one xdotool command against the session display.

        Args:
            *args: Arguments passed to the xdotool binary.
            quiet: Suppress the failure warning. For a caller polling for
                something that is not there yet, where `search` exiting
                non-zero is the expected answer rather than a fault.

        Returns:
            Its stdout, or None if it could not be run, timed out, or exited non-zero.
        """
        try:
            result = subprocess.run(
                [_XDOTOOL, *args],
                env=base_launch_env(),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not quiet:
                log.warning("scummvm: xdotool %s failed: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            if not quiet:
                log.warning(
                    "scummvm: xdotool %s: %s", " ".join(args), result.stderr.strip()
                )
            return None
        return result.stdout

    def _window(self, quiet: bool = False) -> Optional[str]:
        """The X window this launch's ScummVM renders into.

        Matched on this process's own pid, so a window left behind by an
        earlier session cannot swallow the macro. The last id wins: xdotool
        lists in creation order and ScummVM's game window is created after the
        transient ones.

        Args:
            quiet: Suppress the "no window" warning. For a caller polling for
                a window that is not mapped yet, where absence is the normal
                answer until it is not.

        Returns:
            The window id as xdotool prints it, or None when this process has
            no visible window.
        """
        proc = self._proc
        if proc is None:
            if not quiet:
                log.warning("scummvm: no process to find a window for")
            return None
        out = self._xdotool(
            "search", "--onlyvisible", "--pid", str(proc.pid), quiet=quiet
        )
        ids = out.split() if out else []
        if not ids:
            if not quiet:
                log.warning("scummvm: no visible window for pid %d", proc.pid)
            return None
        return ids[-1]

    def _activate(self, win_id: str) -> bool:
        """Give `win_id` the input focus before keys are sent at it.

        The menu key and the button hotkey go through XTEST, which delivers to
        whatever holds focus, so a key sent at an unfocused game lands on the
        desktop instead.

        Args:
            win_id: The window to focus.

        Returns:
            True once the window is active.
        """
        return self._xdotool("windowactivate", "--sync", win_id) is not None

    def _open_gmm(self) -> bool:
        """Open the Global Main Menu with the pinned menu key.

        One unmodified key, bound to the global keymap in the ini before the
        launch: see `MENU_KEY` for why neither ScummVM's own `C+F5` nor the
        engine-level `F5` can be relied on here.

        Returns:
            True when the keystroke went out.
        """
        return self._xdotool("key", "--clearmodifiers", MENU_KEY) is not None

    def _type(self, text: str) -> bool:
        """Type `text` into the focused window.

        `type` rather than `key`: the GMM's button hotkeys follow the GUI
        translation and can be Cyrillic, Greek or Hebrew, and only `type`
        synthesizes the keysym whatever the X keyboard layout is.

        Args:
            text: The hotkey letter to send.

        Returns:
            True when the keystroke went out.
        """
        return self._xdotool("type", text) is not None

    def _keys(self, win_id: str, keys: list[str]) -> bool:
        """Send several keys to `win_id` in one xdotool call.

        One call rather than one per key: each costs a process spawn, and the
        chooser walk sends one key per slot.

        Args:
            win_id: The window to send to.
            keys: Key names in xdotool's syntax.

        Returns:
            True when the keystrokes went out.
        """
        return self._xdotool("key", "--window", win_id, "--delay", "120", *keys) is not None

    def _wait_for_write(self, before: dict[str, tuple[float, int]], deadline: float) -> bool:
        """Poll the working slot until this save's write settles, or `deadline` passes.

        The first change is not the finished save. The caller stops ScummVM as
        soon as this returns and the broker zips the save directory right after,
        so returning on the first differing stat sends SIGTERM into a write
        still in progress and ships whatever landed to RomM as the player's
        progress. A write therefore only counts once the slot differs from
        `before`, holds at least one non-empty file, and has held every size and
        mtime still for `STATE_STABLE`.

        Args:
            before: The slot's `(mtime, size)` per filename, from before the macro.
            deadline: A `time.monotonic()` value to give up at.

        Returns:
            True once a slot file appeared or changed and its write settled,
            False when nothing was written or the write never settled in time.
        """
        last: Optional[dict[str, tuple[float, int]]] = None
        stable_since = 0.0
        while True:
            cur = _slot_stamp(self._target, STATE_SLOT)
            if cur != before:
                if cur != last:
                    last = cur
                    stable_since = time.monotonic()
                elif any(size > 0 for _mtime, size in cur.values()) and (
                    time.monotonic() - stable_since >= STATE_STABLE
                ):
                    return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        if last is not None:
            log.warning(
                "scummvm: slot %d was written but never settled within %.1fs; "
                "treating it as unfinished rather than shipping a torn save",
                STATE_SLOT, STATE_WAIT,
            )
        return False

    def launch(self, rom_path: Optional[Path], resume_slot: Optional[int]) -> None:
        """Register the game, pin the ini, and boot ScummVM on its target.

        The folder is registered with `--add` and the target read back out of
        the ini; a folder ScummVM detects nothing in is a launch failure, since
        there is nothing to boot. With `resume_slot` set and the working slot
        already holding this target's save, the state loads at boot through
        `--save-slot`; otherwise a deferred thread waits for RomM's push and
        loads it over the menu.

        An archived game is extracted into the cache first (or its earlier
        extraction reused), and the game folder inside it is what gets
        registered.

        Args:
            rom_path: The game folder or archive, as returned by `resolve_rom_file`.
            resume_slot: The slot to resume from, or None to boot clean.

        Raises:
            RuntimeError: When no ROM folder was resolved, an archive could
                not be extracted, or ScummVM detects no game in it.
        """
        self.stop()
        rom_dir = rom_path or self._rom_dir
        if rom_dir is None:
            raise RuntimeError("scummvm: no game folder to launch")
        rom_dir = _game_dir(rom_dir, self)

        self._launch_seq += 1
        seq = self._launch_seq

        gui_language = normalize_language(self.gui_language)
        if self.gui_language and gui_language is None:
            log.warning(
                "scummvm: ignoring unrecognised gui_language %r, leaving the "
                "interface as configured",
                self.gui_language,
            )
        patch_ini(gui_language)

        language = normalize_language(self.language)
        if self.language and language is None:
            log.warning(
                "scummvm: ignoring unrecognised language %r, falling back to "
                "the interface language or the game's own default",
                self.language,
            )
        # A multilingual folder registers one target per detected language and
        # the target is what boots, so a rom that names no language of its own
        # leaves the player's interface language as the only thing saying which
        # variant they want. Without either, the name breaks the tie.
        language = language or gui_language
        target = target_for_path(rom_dir, language) or register_target(rom_dir, language)
        if target is None:
            raise RuntimeError(f"scummvm: no detectable game in {rom_dir}")
        self._target = target

        try:
            SAVE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("scummvm: could not create %s: %s", SAVE_DIR, exc)

        env = base_launch_env()
        # SDL would pick Wayland, where the menu macros could never be injected.
        env["SDL_VIDEODRIVER"] = "x11"

        # Both directories are stated, so the ini the broker just pinned and
        # the saves it dumps afterwards are the ones this run uses, whatever
        # the env knobs have been set to.
        cmd = [scummvm_bin(), f"--config={INI_PATH}", f"--savepath={SAVE_DIR}"]
        if language:
            # Authoritative for this run: --add wrote whatever it detected into
            # the game domain, and the flag overrides that for the session.
            cmd.append(f"--language={language}")
        resume_path = slot_file(target, STATE_SLOT) if resume_slot is not None else None
        if resume_path is not None:
            cmd.append(f"--save-slot={STATE_SLOT}")
        # ScummVM's option parsing stops at the first non-option argument, so
        # the target is always last or the options after it are read as stray
        # arguments and nothing launches.
        cmd.append(target)

        log.info(
            "scummvm: launching %s (rom=%s, language=%s, gui_language=%s, "
            "resume_slot=%s, boot resume=%s)",
            target,
            rom_dir,
            language or "-",
            gui_language or "-",
            resume_slot,
            resume_path is not None,
        )
        self._spawn(cmd, env)

        if FILL_SCREEN:
            Thread(target=self._fill_screen, args=(seq,), daemon=True).start()
        if resume_slot is not None and resume_path is None:
            Thread(target=self._deferred_load_state, args=(seq,), daemon=True).start()

    def _fill_screen(self, seq: int) -> None:
        """Keep the game window the size of the display, for as long as it runs.

        Resizing once at launch is not enough: the display is sized by the
        streaming client, so a game that starts before a browser has connected
        is grown to whatever the last session left behind and then sits in the
        top left corner of a screen that changed under it. The same happens
        mid-session when a viewer resizes their browser. So this follows the
        display instead of photographing it, and exits when the launch is
        superseded or the game is gone.

        Purely cosmetic, so every failure is logged and swallowed: a game
        running at its own size is worth more than a launch reported as broken.

        Args:
            seq: The launch sequence number this belongs to.
        """
        deadline = time.monotonic() + FILL_SCREEN_WAIT
        win_id = None
        while time.monotonic() < deadline:
            if self._launch_seq != seq:
                return
            win_id = self._window(quiet=True)
            if win_id:
                break
            time.sleep(0.5)
        if not win_id:
            log.warning("scummvm: no window to fill the screen with")
            return

        applied: Optional[tuple[str, str]] = None
        while self._launch_seq == seq and self.alive():
            size = self._display_size()
            if size and size != applied:
                # Move first: a window the WM placed at an offset would
                # otherwise be sized to the display and hang off the bottom
                # right of it.
                width, height = size
                if (
                    self._xdotool("windowmove", win_id, "0", "0") is not None
                    and self._xdotool("windowsize", win_id, width, height) is not None
                ):
                    applied = size
                    log.info("scummvm: window %s sized to %sx%s", win_id, width, height)
            time.sleep(FILL_SCREEN_POLL)

    def _display_size(self) -> Optional[tuple[str, str]]:
        """The display's current size as a `(width, height)` pair of digits.

        Returns:
            The pair, or None when xdotool could not be asked or answered
            something that is not two numbers.
        """
        geometry = self._xdotool("getdisplaygeometry", quiet=True)
        parts = geometry.split() if geometry else []
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        return parts[0], parts[1]

    def _deferred_load_state(self, seq: int) -> None:
        """Wait for a pushed state to arrive, then load it through the menu.

        Gives the file `RESUME_LOAD_WAIT` to turn up and then the game
        `RESUME_LOAD_SETTLE` to be far enough into its startup to answer the
        menu. Abandons itself as soon as `seq` no longer matches the current
        launch, so a superseded launch never gets a stray load.

        Args:
            seq: The launch sequence number this load belongs to.
        """
        if not self.wait_for_state(time.monotonic() + RESUME_LOAD_WAIT):
            log.warning("scummvm: resume state never arrived, booting unresumed")
            return
        if self._launch_seq != seq:
            log.info("scummvm: launch superseded, deferred resume abandoned")
            return
        time.sleep(RESUME_LOAD_SETTLE)
        if self._launch_seq != seq:
            return
        ok = self.load_state(STATE_SLOT)
        log.info("scummvm: deferred resume %s", "delivered" if ok else "failed")

    def save_state(self, slot: int) -> bool:
        """Save the running game into the broker's slot through the GMM.

        `slot` is what RomM asked for and is ignored: the save lands in
        `STATE_SLOT` and the caller reads the effective slot back off
        `state_slot`. The chooser opens with its list focused and nothing
        selected, so the first `Down` lands on slot 0 and slot N needs N+1 of
        them; the list index is the slot number because empty slots still take
        a row. In save mode the first `Return` starts editing the slot's
        description and the second commits it, which is also the save.

        Args:
            slot: The slot RomM requested; not used.

        Returns:
            True once the slot's file has changed on disk within `STATE_WAIT`,
            False when the window could not be found, a keystroke failed, or
            the write never landed.
        """
        win_id = self._window()
        if win_id is None:
            return False
        save_key, _ = gmm_hotkeys()
        before = _slot_stamp(self._target, STATE_SLOT)

        if not self._activate(win_id):
            return False
        if not self._open_gmm():
            return False
        time.sleep(KEY_DELAY)
        if not self._type(save_key):
            return False
        time.sleep(KEY_DELAY)
        if not self._keys(win_id, ["Down"] * (STATE_SLOT + 1) + ["Return", "Return"]):
            return False

        if self._wait_for_write(before, time.monotonic() + STATE_WAIT):
            log.info("scummvm: saved %s into slot %d", self._target, STATE_SLOT)
            return True

        log.warning(
            "scummvm: no slot %d write within %.1fs; the engine running %s may not "
            "support saving from the menu",
            STATE_SLOT,
            STATE_WAIT,
            self._target,
        )
        # First Escape closes the chooser or the dialog that blocked the save,
        # the second closes the GMM, so the game is not left paused in a menu.
        self._keys(win_id, ["Escape"])
        time.sleep(0.3)
        self._keys(win_id, ["Escape"])
        return False

    def load_state(self, slot: int) -> bool:
        """Load the broker's slot into the running game through the GMM.

        In load mode the list is not editable, so a single `Return` activates
        the selected slot and the GMM closes itself.

        Args:
            slot: The slot RomM requested; `STATE_SLOT` is what gets loaded.

        Returns:
            True when the slot holds a save and the keystrokes went out, False
            otherwise. An empty slot is caught here because the macro would
            otherwise walk to an empty row and report success having loaded
            nothing.
        """
        if self.state_path() is None:
            log.warning("scummvm: slot %d holds no save to load", STATE_SLOT)
            return False
        win_id = self._window()
        if win_id is None:
            return False
        _, load_key = gmm_hotkeys()
        if not self._activate(win_id):
            return False
        if not self._open_gmm():
            return False
        time.sleep(KEY_DELAY)
        if not self._type(load_key):
            return False
        time.sleep(KEY_DELAY)
        ok = self._keys(win_id, ["Down"] * (STATE_SLOT + 1) + ["Return"])
        if ok:
            log.info("scummvm: loaded %s from slot %d", self._target, STATE_SLOT)
        return ok

    def state_path(self) -> Optional[Path]:
        """The working slot's save file for the booted target, or None when empty."""
        return slot_file(self._target, STATE_SLOT)

    def clear_working_slot(self, excluded: tuple[str, ...] = ()) -> None:
        """Empty the save directory before a new session boots.

        The target only exists once a game has been registered and booted, so
        at activate time a leftover cannot be told apart from the save of the
        game about to start. Anything still here belongs to a session that has
        already exited and whose saves RomM holds; the incoming archive
        restores whatever should be here.

        The whole directory goes, not just the broker's slot. ScummVM names a
        save `<target>.<NNN>` for the slot the player chose in the game's own
        menu, and nothing in that name says whose session wrote it, so
        clearing only the broker's slot leaves every save the last player made
        from inside the game readable by this one and swept into their dump.

        Args:
            excluded: Subtrees carried by the whole-card routes. ScummVM has
                no memory card, so this is always empty.
        """
        self._clear_save_subtrees(excluded)

    def state_target(self, filename: str) -> Optional[Path]:
        """Where a pushed state called `filename` belongs.

        The name is rewritten onto the booted target and the broker's slot:
        ScummVM finds a save by name alone, so a state captured under another
        target (a multilingual folder registers one per language) has to be
        renamed or the engine will not see it. The suffix form the state
        arrived in is kept, since which of the two an engine writes is the
        engine's business.

        Args:
            filename: The name RomM stored the state under.

        Returns:
            The path to write to, or None when the name is not a ScummVM save
            name or nothing has booted to name it after.
        """
        match = _SAVE_NAME_RE.fullmatch(filename)
        if match is None:
            return None
        if self._target is None:
            log.warning("scummvm: no booted target to file %s under", filename)
            return None
        ext = match.group("ext")
        restamped = (
            f"s{STATE_SLOT:02d}" if ext.startswith("s") else f"{STATE_SLOT:03d}"
        )
        return SAVE_DIR / f"{self._target}.{restamped}"

    def save_file_kind(self, rel: str) -> str:
        """Classify an archive member for the manifest.

        ScummVM has no state format of its own, so the working slot's file is a
        save like every other and only its name says otherwise. Naming the
        target as well as the slot is what keeps another game's save in the
        same slot from being filed as this session's state.

        Args:
            rel: The member path, relative to `save_root` and posix-separated.

        Returns:
            `state` for the booted target's working-slot save, `save` for
            everything else.
        """
        if self._target is None:
            return "save"
        name = rel.rsplit("/", 1)[-1]
        return "state" if name in slot_names(self._target, STATE_SLOT) else "save"

    def import_spec(self) -> imports.ImportSpec:
        """Declare what ScummVM takes: save files, and one of them as the state.

        Saves and states share `saves/`, so the two kinds have the same shapes.
        The state rides the archive and resumes through `save.resume_slot`,
        which boots the game with `--save-slot`.

        Returns:
            The spec.
        """
        shapes = ("<game>.NNN", "<game>.sNN")
        return imports.ImportSpec(
            kinds=(
                imports.KindSpec("save", shapes),
                imports.KindSpec(
                    "state", shapes, requires_resume_slot=True, max_members=1, counts_v1=True
                ),
            ),
            state_channel="archive",
        )

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Place one declared member: a save, or the state.

        Args:
            member: The member, already past the kind gate.
            spec: This emulator's spec.
            ctx: The launch context.

        Returns:
            The placement, or a refusal.
        """
        if member.kind == "state":
            return _place_state(member, self, ctx)
        return _place_save(member, self, ctx)

    def validate_import_plan(
        self, plan: list[imports.Placement], ctx: imports.ImportCtx
    ) -> list[imports.ImportRefusal]:
        """Refuse a state that shares the working slot with a save the archive carries.

        `check_plan` counts the archive's own states by `save_file_kind`, which
        answers `save` for everything until a game has booted. So a v1 save in
        the working slot under the target's name goes uncounted there, and
        would be restored on top of the state, or beside it under the other
        spelling.

        Args:
            plan: The placements that passed every per-member check.
            ctx: The launch context.

        Returns:
            One `destination_conflict` for the state, or nothing when the state
            is alone, is already refused, or `check_plan` already counted the
            v1 slot files.
        """
        states = [p for p in plan if p.member.kind == "state"]
        if len(states) != 1:
            return []
        target, _ = _session_game(self, ctx)
        if target is None:
            return []
        state = states[0]
        if state.member.name in imports.destination_conflicts(
            plan, ctx.archive_paths, self.import_spec().case_insensitive_dest
        ):
            return []
        working = slot_names(target, STATE_SLOT)
        carried = sorted(
            PurePosixPath(rel).as_posix()
            for rel in ctx.archive_paths
            if PurePosixPath(rel).parts[:1] == ("saves",)
            and len(PurePosixPath(rel).parts) == 2
            and PurePosixPath(rel).name in working
        )
        if not carried or any(self.save_file_kind(rel) == "state" for rel in carried):
            return []
        return [
            imports.ImportRefusal(
                "destination_conflict",
                state.member.name,
                "one state per archive",
                detail=f"the archive already carries {', '.join(carried)}",
            )
        ]

    def identity_source(self) -> Optional[imports.IdentitySource]:
        """Read the target the folder is registered under, for the session's identity.

        Returns:
            The source.
        """
        return imports.IdentitySource("scummvm_target", rom_reader=self._read_target)

    def _read_target(self, rom: Path) -> Optional[str]:
        """Look up the target registered for `rom`, without registering it.

        Only a lookup: a launch that ran no preflight reads this in a worker
        thread, and registering can take up to two minutes. The identity is
        informational, so an unregistered folder reads as no identity.

        Args:
            rom: The game folder, or an archived game.

        Returns:
            The target, or None when the ini has no domain for the folder, or
            the archive has not been extracted yet.
        """
        rom_dir = _cached_game_dir(rom)
        if rom_dir is None:
            return None
        return target_for_path(rom_dir, _wanted_language(self))

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Save through the menu if asked, then stop ScummVM.

        Args:
            slot: The slot RomM asked to save into, resolved to `STATE_SLOT`,
                or None to exit without writing a state.

        Returns:
            A dict with `state_saved` (bool), `state_slot` (the effective slot,
            or None when no save was asked for) and `state_file` (the written
            file's `path`, `size` and `mtime`, or None).
        """
        saved = False
        state_file: Optional[dict[str, Any]] = None
        if slot is not None and self.alive():
            saved = self.save_state(slot)
            if saved:
                path = self.state_path()
                if path is not None:
                    try:
                        st = path.stat()
                    except OSError as exc:
                        log.warning("scummvm: could not stat %s: %s", path, exc)
                        saved = False
                    else:
                        state_file = {
                            "path": str(path),
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                        }
        self.stop()
        return {
            "state_saved": saved,
            "state_slot": STATE_SLOT if slot is not None else None,
            "state_file": state_file,
        }
