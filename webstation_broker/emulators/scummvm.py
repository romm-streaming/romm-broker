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
from pathlib import Path
from threading import Thread
from typing import Any, Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Root of the RomM library mount (env `ROM_ROOT`, default `/romm`).

A resolved game folder must sit under it; anything resolving outside is discarded.
"""

CONFIG_DIR = Path(os.environ.get("SCUMMVM_CONFIG_DIR", "/config/.config/scummvm"))
"""ScummVM's config directory (env `SCUMMVM_CONFIG_DIR`, default `/config/.config/scummvm`)."""
INI_PATH = CONFIG_DIR / "scummvm.ini"
"""The config the broker pins before every launch and reads game targets back out of."""
DATA_DIR = Path(os.environ.get("SCUMMVM_DATA_DIR", "/config/.local/share/scummvm"))
"""ScummVM's data directory (env `SCUMMVM_DATA_DIR`, default `/config/.local/share/scummvm`)."""
SAVE_DIR = DATA_DIR / "saves"
"""Where ScummVM writes every save, which is also where the working slot lives."""
SCUMMVM_LOG_PATH = Path(os.environ.get("SCUMMVM_LOG_PATH", "/config/scummvm.log"))
"""Log file the broker appends this emulator's output to (env `SCUMMVM_LOG_PATH`)."""

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
    "be": ("з", "а"),
    "ca": ("d", "c"),
    "cs": ("u", "n"),
    "da": ("g", "n"),
    "de": ("s", "l"),
    "el": ("α", "φ"),
    "es": ("g", "c"),
    "eu": ("g", "k"),
    "fi": ("t", "l"),
    "fr": ("s", "c"),
    "he": ("ש", "ט"),
    "it": ("s", "c"),
    "nb": ("l", "å"),
    "pl": ("z", "w"),
    "pt": ("g", "c"),
    "ru": ("а", "з"),
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

_SAVE_NAME_RE = re.compile(r"^(?P<stem>[^/\\.]+)\.(?P<ext>s\d{2,3}|\d{3})$")
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
            INI_PATH.write_text(rendered)
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
        INI_PATH.write_text("\n".join(out) + "\n")
    except OSError as exc:
        log.error("scummvm: could not write %s: %s, ini not pinned", INI_PATH, exc)


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
    domains = _game_domains()
    targets = []
    for name, keys in domains.items():
        try:
            if Path(keys["path"]) == rom_dir:
                targets.append(name)
        except (OSError, ValueError):
            continue
    if not targets:
        return None
    best = min(targets, key=lambda name: (_language_rank(domains[name], language), name))
    if len(targets) > 1:
        log.info(
            "scummvm: %d targets registered for %s, booting %s (language=%s)",
            len(targets),
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
    cmd = [scummvm_bin(), "--add", f"--path={rom_dir}"]
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
            if _drop_dead_domains(gameid):
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
    return target


def _drop_dead_domains(gameid: str) -> int:
    """Remove `gameid`'s domains whose recorded path no longer exists.

    Only domains that are already dead go: a path that is not there cannot
    boot, and ScummVM's own launcher greys those entries out. Anything still
    on disk is left alone, including another copy of the same game, so a
    library that is merely unmounted keeps its registrations and the save
    files named after them.

    Args:
        gameid: The game whose stale domains are in the way.

    Returns:
        How many domains were dropped.
    """
    doomed = set()
    for name, keys in _game_domains().items():
        if keys.get("gameid") != gameid:
            continue
        try:
            if not Path(keys["path"]).is_dir():
                doomed.add(name)
        except (OSError, ValueError):
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
        INI_PATH.write_text("\n".join(out) + "\n")
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
        except OSError:
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
        try:
            st = (SAVE_DIR / name).stat()
        except OSError:
            continue
        stamp[name] = (st.st_mtime, st.st_size)
    return stamp


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

    Attributes:
        name: Registry key, `scummvm`.
        display_name: Human-readable name shown in the UI.
        save_root: ScummVM's data directory, which the save subtree hangs off.
        save_subtrees: `saves`, holding the game's saves and the working slot alike.
        state_subtrees: Empty, because states are not in a subtree of their own.
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
    rom_extensions = (".scummvm",)
    """The marker file some libraries put in a game folder.

    A ScummVM game is the folder itself, so this is only what a ROM pointing at
    a single file is allowed to be; the folder holding it is what gets
    registered either way.
    """
    supports_states = True
    state_slot = STATE_SLOT
    state_dir = SAVE_DIR
    log_path = SCUMMVM_LOG_PATH
    term_timeout = float(os.environ.get("SCUMMVM_STOP_WAIT", "10"))

    def __init__(self) -> None:
        """Start with no game folder, no target, and no launch behind it."""
        super().__init__()
        self._rom_dir: Optional[Path] = None
        """The folder `resolve_rom_file` picked, which `launch` registers."""
        self._target: Optional[str] = None
        """The target the running game booted under, and every save is named after.

        Kept after the process stops: the state routes are read once the game
        is already gone, and they have no other way to name its files.
        """
        self._launch_seq = 0
        """Bumped per launch, so a deferred resume can tell it has been superseded."""

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a RomM path to the game folder to register.

        Kept cheap: this runs on the event loop, while the detection pass that
        can actually answer "is there a game in here" takes seconds and runs in
        `launch`. A folder that holds files is accepted here and only a folder
        ScummVM detects nothing in fails, later, as a launch failure.

        Args:
            path: The ROM as RomM delivered it: the game folder, or a file
                inside it (a `.scummvm` marker, or the single file a library
                pointed at).

        Returns:
            The game folder, or None when the path is a file this launcher
            does not recognise, is outside the ROM root, is not a folder, or
            holds nothing to detect.
        """
        self._rom_dir = None
        if not path.is_dir() and path.suffix.lower() not in self.rom_extensions:
            # Falling through to `path.parent` here would register whatever
            # directory the file happens to sit in. For a library laid out as
            # <root>/<platform>/<file>, that is the platform folder: `--add`
            # would scan every game in it and boot whichever target sorts
            # first. A wrong game is worse than a refused launch.
            log.warning(
                "scummvm: %s is not a game folder or a %s marker; ScummVM games "
                "are folders, extract an archived one first",
                path,
                " or ".join(self.rom_extensions),
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
        self._rom_dir = resolved
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
        """Poll the working slot until its file changes, or `deadline` passes.

        Args:
            before: The slot's `(mtime, size)` per filename, from before the macro.
            deadline: A `time.monotonic()` value to give up at.

        Returns:
            True once a slot file appeared or changed.
        """
        while time.monotonic() < deadline:
            if _slot_stamp(self._target, STATE_SLOT) != before:
                return True
            time.sleep(0.3)
        return _slot_stamp(self._target, STATE_SLOT) != before

    def launch(self, rom_path: Optional[Path], resume_slot: Optional[int]) -> None:
        """Register the game, pin the ini, and boot ScummVM on its target.

        The folder is registered with `--add` and the target read back out of
        the ini; a folder ScummVM detects nothing in is a launch failure, since
        there is nothing to boot. With `resume_slot` set and the working slot
        already holding this target's save, the state loads at boot through
        `--save-slot`; otherwise a deferred thread waits for RomM's push and
        loads it over the menu.

        Args:
            rom_path: The game folder, as returned by `resolve_rom_file`.
            resume_slot: The slot to resume from, or None to boot clean.

        Raises:
            RuntimeError: When no ROM folder was resolved, or ScummVM detects
                no game in it.
        """
        self.stop()
        rom_dir = rom_path or self._rom_dir
        if rom_dir is None:
            raise RuntimeError("scummvm: no game folder to launch")

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

        cmd = [scummvm_bin(), f"--savepath={SAVE_DIR}"]
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
        return self._keys(win_id, ["Down"] * (STATE_SLOT + 1) + ["Return"])

    def state_path(self) -> Optional[Path]:
        """The working slot's save file for the booted target, or None when empty."""
        return slot_file(self._target, STATE_SLOT)

    def state_screenshot_path(self) -> Optional[Path]:
        """None: ScummVM embeds the thumbnail in the save itself.

        Every GUI-made save carries a `THMB` block inside it (see
        `Graphics::saveThumbnail`), so there is no separate file to point at and
        the frame travels inside the state RomM already fetched.

        Returns:
            Always None.
        """
        return None

    def clear_working_slot(self) -> None:
        """Delete every game's working-slot save before a new session boots.

        The target only exists once a game has been registered and booted, so
        at activate time a leftover cannot be told apart from the save of the
        game about to start. Anything still in this slot belongs to a session
        that has already exited and whose state RomM holds; the incoming
        archive restores whatever should be here.
        """
        if not SAVE_DIR.is_dir():
            return
        for pattern in (f"*.s{STATE_SLOT:02d}", f"*.{STATE_SLOT:03d}"):
            for stale in SAVE_DIR.glob(pattern):
                try:
                    stale.unlink()
                    log.info("scummvm: cleared stale save %s", stale.name)
                except OSError as exc:
                    log.warning("scummvm: could not clear %s: %s", stale.name, exc)

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
        match = _SAVE_NAME_RE.match(filename)
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
