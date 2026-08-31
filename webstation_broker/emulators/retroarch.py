"""RetroArch launcher for any libretro core.

Covers the platform to core mapping, the buildbot core download, and the stdin
command protocol for save, state and quit.

This is the general purpose provider: instead of one class per emulator we
keep a map from RomM platform slug to libretro core.

RetroArch is the user's own desktop app, so we never touch its config file.
The launch uses `--appendconfig <broker.cfg>` to layer in only the
protocol-required settings on top of the user's config: the stdin command
interface, and the broker-managed save directories so the save archive logic
tracks real files.

Control plane: RetroArch's stdin command interface (config key
`stdin_cmd_enable`) reads newline-delimited commands from stdin and writes
replies to *stdout*. Commands:

* `SAVE_STATE`: no reply; dispatches `CMD_EVENT_SAVE_STATE` from the runloop
  (the save-state hotkey path).
* `LOAD_STATE_SLOT <n>`: bare echo `LOAD_STATE_SLOT <n>` (no success bit).
* `STATE_SLOT_PLUS`: no reply; current slot +1.
* `STATE_SLOT_MINUS`: no reply; current slot -1, floored at -1 (auto).
* `SAVE_FILES`: `OK` / `NO` (newline-terminated).
* `GET_STATUS`: `GET_STATUS PLAYING <core_id>,<basename>` (newline-terminated)
  or `GET_STATUS CONTENTLESS`.
* `DISK_EJECT_TOGGLE`: open or close the virtual tray.
* `DISK_NEXT`: step to the next disc in the loaded playlist.
* `QUIT`: no reply; queued for the runloop.

Saves are confirmed on the filesystem instead of from a reply.

Save slots: there is no "save to slot n" command. `SAVE_STATE` writes whichever
slot is current, nothing reports which that is, and a `state_slot` in the
appended config does not survive content load (verified on 1.22.2: pinning 10
still wrote slot 0). None of that matters much here, because RomM keeps the
library of states and this only ever works in `STATE_SLOT`. What RetroArch does
have is that hard floor at -1, so counting MINUS presses down to it and PLUS
presses back up parks the slot absolutely. That runs once per launch, and
again only if a save lands somewhere else, which is the one thing that can
happen: the player cycling slots with their own hotkeys.

State file naming:

* `<content_basename>.state` for slot 0.
* `<content_basename>.state<n>` for slot n.
* `<content_basename>.state.auto` for slot -1.
* Each save writes a `<name>.png` thumbnail beside the state file.
* SRAM: `<content_basename>.srm` under the savefile dir.

Because stdout carries only command replies here, the child is spawned with
a real stdout pipe drained by a reader thread, unlike the shared `_spawn`
which merges stderr into stdout (that would corrupt the reply stream).
"""

import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional, Union

import httpx

from .base import Emulator, _record_pid, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Root of the RomM library mount, from `ROM_ROOT` (default `/romm`).

A ROM candidate has to resolve to somewhere under it to be booted.
"""

XDG_DATA_HOME = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
"""The user's data home, from `XDG_DATA_HOME` (default `~/.local/share`)."""
RA_CONFIG_DIR = Path(os.environ.get("RETROARCH_CONFIG_DIR", str(Path.home() / ".config" / "retroarch")))
"""The user's RetroArch config directory, from `RETROARCH_CONFIG_DIR` (default `~/.config/retroarch`)."""


def _configured_dir(setting: str) -> Optional[Path]:
    """A directory setting read out of the user's RetroArch config.

    Args:
        setting: The config key to look up, such as `libretro_directory`.

    Returns:
        The configured path with `~` expanded, or None when the config is
        unreadable, the key is absent, or it is set to `default`.
    """
    try:
        text = (RA_CONFIG_DIR / "retroarch.cfg").read_text(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == setting:
            raw = value.strip().strip('"').strip()
            if raw and raw != "default":
                return Path(os.path.expanduser(raw))
    return None


CORES_DIR = Path(
    os.environ.get("RETROARCH_CORES_DIR")
    or _configured_dir("libretro_directory")
    or Path(XDG_DATA_HOME) / "RetroArch" / "cores"
)
"""Where libretro cores are installed and loaded from.

Taken from `RETROARCH_CORES_DIR`, else the `libretro_directory` in the user's
config, else `$XDG_DATA_HOME/RetroArch/cores`. A core has to land in the dir
RetroArch also reads .info files from. Loading one from anywhere else leaves
its core info unset, and `GET_STATUS` then segfaults RetroArch mid-session
(1.22.2). Following the user's own `libretro_directory` is also what makes a
downloaded core show up in the desktop RetroArch without its in-app core
downloader.
"""
SYSTEM_DIR = Path(
    os.environ.get("RETROARCH_SYSTEM_DIR")
    or _configured_dir("system_directory")
    or Path(XDG_DATA_HOME) / "RetroArch" / "system"
)
"""Where cores look for the assets and firmware they cannot ship themselves.

Taken from `RETROARCH_SYSTEM_DIR`, else the `system_directory` in the user's
config, else `$XDG_DATA_HOME/RetroArch/system`.
"""
CORES_BASE_URL = os.environ.get(
    "RETROARCH_CORES_BASE_URL",
    "https://buildbot.libretro.com/nightly/linux/x86_64/latest",
)
"""Base URL cores are downloaded from, from `RETROARCH_CORES_BASE_URL`.

Defaults to the libretro buildbot's latest linux x86_64 nightly. The buildbot
ships every core as `<core>_libretro.so.zip`.
"""
GITHUB_API_BASE = os.environ.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
"""GitHub API root, from `GITHUB_API_BASE` (default `https://api.github.com`).

Cores the buildbot does not carry name their own release source instead, and
their releases are looked up here.
"""

RA_DATA_DIR = Path(os.environ.get("RETROARCH_DATA_DIR", "/config/.retroarch"))
"""Root of the broker-managed RetroArch data, from `RETROARCH_DATA_DIR`.

Defaults to `/config/.retroarch`. Holds the save data and the append-config we
layer onto the user's RetroArch config at launch.
"""
STATE_DIR = RA_DATA_DIR / "states"
"""The broker-managed savestate directory, `states` under `RA_DATA_DIR`."""
SAVE_DIR = RA_DATA_DIR / "saves"
"""The broker-managed savefile (SRAM) directory, `saves` under `RA_DATA_DIR`."""
BROKER_CFG = RA_DATA_DIR / "broker.cfg"
"""The append-config written per launch, `broker.cfg` under `RA_DATA_DIR`."""
RA_LOG_PATH = Path(os.environ.get("RETROARCH_LOG_PATH", "/config/retroarch.log"))
"""Where RetroArch's stderr is appended, from `RETROARCH_LOG_PATH` (default `/config/retroarch.log`)."""

SAVE_FILES_WAIT = float(os.environ.get("RETROARCH_SAVE_FILES_WAIT", "10.0"))
"""Seconds to wait for the `SAVE_FILES` reply at exit, from `RETROARCH_SAVE_FILES_WAIT` (default 10)."""
STATE_CONFIRM_WAIT = float(os.environ.get("RETROARCH_STATE_CONFIRM_WAIT", "10.0"))
"""Seconds a save gets to land on disk, from `RETROARCH_STATE_CONFIRM_WAIT` (default 10)."""
QUIT_WAIT = float(os.environ.get("RETROARCH_QUIT_WAIT", "10.0"))
"""Seconds the second `QUIT` gets before SIGTERM, from `RETROARCH_QUIT_WAIT` (default 10)."""
QUIT_CONFIRM_GAP = float(os.environ.get("RETROARCH_QUIT_CONFIRM_GAP", "0.1"))
"""Seconds the first `QUIT` gets before a second press, from `RETROARCH_QUIT_CONFIRM_GAP` (default 0.1)."""
RESUME_LOAD_WAIT = float(os.environ.get("RETROARCH_RESUME_WAIT", "90.0"))
"""Seconds a deferred resume load waits for a running game and a state file.

From `RETROARCH_RESUME_WAIT` (default 90).
"""
RESUME_LOAD_SETTLE = float(os.environ.get("RETROARCH_RESUME_SETTLE", "3.0"))
"""Seconds between the core reporting PLAYING and the load, from `RETROARCH_RESUME_SETTLE` (default 3)."""
LOAD_ACK_WAIT = float(os.environ.get("RETROARCH_LOAD_ACK_WAIT", "10.0"))
"""Seconds to wait for the `LOAD_STATE_SLOT` echo, from `RETROARCH_LOAD_ACK_WAIT` (default 10)."""
STATE_SLOT = int(os.environ.get("RETROARCH_STATE_SLOT", "0"))
"""The one slot the broker works in, from `RETROARCH_STATE_SLOT` (default 0).

0 is RetroArch's own default, so a state written here is also the one the
player's own load hotkey reaches for.
"""
SLOT_STEP_DELAY = float(os.environ.get("RETROARCH_SLOT_STEP_DELAY", "0.1"))
"""Seconds between slot-homing presses, from `RETROARCH_SLOT_STEP_DELAY` (default 0.1).

The pause is what keeps RetroArch from dropping presses.
"""
SLOT_HOME_STEPS = int(os.environ.get("RETROARCH_SLOT_HOME_STEPS", "24"))
"""`STATE_SLOT_MINUS` presses used to home the slot, from `RETROARCH_SLOT_HOME_STEPS` (default 24).

The step count has to outrun any slot the player could have cycled to.
"""
DISC_TRAY_SETTLE = float(os.environ.get("RETROARCH_DISC_TRAY_SETTLE", "1.5"))
"""Seconds the tray gets to open before discs are stepped, from `RETROARCH_DISC_TRAY_SETTLE` (default 1.5).

The settle is not optional: RetroArch drops a disc index change that arrives
while the tray is still opening, and the failure is silent (the old disc stays
mounted).
"""
DISC_STEP_DELAY = float(os.environ.get("RETROARCH_DISC_STEP_DELAY", "0.1"))
"""Seconds between `DISK_NEXT` presses, from `RETROARCH_DISC_STEP_DELAY` (default 0.1)."""
DISC_SWAP_WAIT = float(os.environ.get("RETROARCH_DISC_SWAP_WAIT", "90.0"))
"""How long a swap waits for the core to report a running game, from `RETROARCH_DISC_SWAP_WAIT`.

Defaults to 90 seconds. A mid-session swap answers on the first poll; a swap
issued right after launch waits out the boot.
"""
CORE_DOWNLOAD_TIMEOUT = float(os.environ.get("RETROARCH_CORE_DOWNLOAD_TIMEOUT", "180"))
"""HTTP timeout for core downloads and release lookups, from `RETROARCH_CORE_DOWNLOAD_TIMEOUT`.

Defaults to 180 seconds.
"""
JOYPAD_DRIVER = os.environ.get("RETROARCH_JOYPAD_DRIVER", "linuxraw")
"""The joypad driver forced at launch, from `RETROARCH_JOYPAD_DRIVER` (default `linuxraw`).

The pads here are Selkies interposer sockets, not real devices, and the fake
libudev behind them gives every one the same identity. RetroArch's udev joypad
driver reads that as one device plugged eight times ("Device ID 0 is already
plugged") and ends up with no pads at all. linuxraw opens the js nodes
directly, which the interposer does hook, so there is nothing to collide on.
Set empty to leave the user's own driver alone.
"""

_PLATFORMS_FILE = Path(__file__).with_name("retroarch_platforms.json")
"""The RomM platform slug to libretro core table, `retroarch_platforms.json` next to this module."""


def _load_platforms() -> dict[str, dict[str, Any]]:
    """Read the platform table, turning its list fields into tuples.

    Returns:
        Platform slug to its entry, with `extensions` and any `save_subtrees`
        as tuples.
    """
    platforms = json.loads(_PLATFORMS_FILE.read_text())
    for info in platforms.values():
        info["extensions"] = tuple(info["extensions"])
        if "save_subtrees" in info:
            info["save_subtrees"] = tuple(info["save_subtrees"])
    return platforms


PLATFORMS: dict[str, dict[str, Any]] = _load_platforms()
"""RomM platform slug to libretro core, loaded from `_PLATFORMS_FILE`.

Each entry names the `core` and its `extensions`; the extensions order doubles
as the preference order when a folder holds several candidates.

`savestate` is assumed true; only specialized cores opt out.

`thumbnail` is assumed true. Cores that render on the GPU can deadlock
RetroArch's main loop on the framebuffer grab that follows a save, which takes
the stdin command channel down with it for the rest of the session.

`assets` maps a path under the RetroArch system dir to the directory on the
image holding those files, for cores that need data the .so does not carry.

`core_source` names where a core the buildbot does not carry comes from, and
`save_subtrees` narrows the save archive for cores whose savefile dir is also
their app-data dir.
"""

_ROM_SEARCH_GLOBS = ("*", "*/*")
"""Globs a ROM folder is searched with: its top level and one level of subfolders."""
_ADDON_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE
)
"""Matches paths that look like an update, DLC or patch rather than the game itself."""
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)
"""Matches a disc number in a path.

Disc numbering keeps a multi-disc game booting the same disc each session, so
a save state taken for Disc 1 resumes on Disc 1.
"""


def _platform_info(platform: Optional[str]) -> Optional[dict[str, Any]]:
    """Look a RomM platform slug up in `PLATFORMS`, case-insensitively.

    Args:
        platform: The slug from the activate payload, or None.

    Returns:
        The platform's entry, or None when it is unset or unmapped.
    """
    if not platform:
        return None
    return PLATFORMS.get(platform.lower())


def _github_release_asset(repo: str, asset_pattern: str) -> str:
    """Download URL of the newest release asset matching `asset_pattern`.

    /releases/latest is the stable channel: GitHub leaves prereleases out of
    it, which is what keeps a nightly build from installing itself into a
    player's session. A `GITHUB_TOKEN` in the environment is sent as a bearer
    token when present.

    Args:
        repo: The `owner/name` of the GitHub repository.
        asset_pattern: A regular expression an asset's full name must match.

    Returns:
        The matching asset's `browser_download_url`.

    Raises:
        RuntimeError: When no asset in the latest release matches.
        httpx.HTTPError: When the release lookup fails.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=CORE_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    release = resp.json()
    tag = release.get("tag_name")
    pattern = re.compile(asset_pattern)
    for asset in release.get("assets", []):
        if pattern.fullmatch(asset.get("name", "")):
            log.info("retroarch: %s stable release %s -> %s", repo, tag, asset["name"])
            return asset["browser_download_url"]
    raise RuntimeError(f"no asset matching {asset_pattern!r} in {repo} release {tag!r}")


def _core_url(core: str, source: Optional[dict[str, Any]]) -> str:
    """Where `core`'s zip comes from.

    The buildbot unless the platform names a `core_source`; an env override
    (`RETROARCH_CORE_URL_<CORE>`) pins a build without editing the table.

    Args:
        core: The libretro core name, without the `_libretro.so` suffix.
        source: The platform's `core_source` entry, holding either a direct
            `url` or a `github_release` repo plus an `asset` pattern, or None.

    Returns:
        The URL of the zip to download.
    """
    override = os.environ.get(f"RETROARCH_CORE_URL_{core.upper()}", "").strip()
    if override:
        return override
    if source:
        if source.get("url"):
            return source["url"]
        if source.get("github_release"):
            return _github_release_asset(source["github_release"], source["asset"])
    return f"{CORES_BASE_URL}/{core}_libretro.so.zip"


def _ensure_core(core: str, source: Optional[dict[str, Any]] = None) -> Path:
    """Return the core's .so, downloading it if missing.

    The zip is fetched into memory, the first `_libretro.so` inside it is
    written to a temp file in `CORES_DIR`, made executable, and moved into
    place.

    Args:
        core: The libretro core name, without the `_libretro.so` suffix.
        source: The platform's `core_source` entry, or None for the buildbot.

    Returns:
        The path of the installed `.so`.

    Raises:
        RuntimeError: When no download can be resolved, the download fails, or
            the zip holds no core.
    """
    so = CORES_DIR / f"{core}_libretro.so"
    if so.is_file():
        return so
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        url = _core_url(core, source)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise RuntimeError(f"could not resolve a download for core {core}: {exc}") from exc
    log.info("retroarch: downloading core %s from %s", core, url)
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=CORE_DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = [n for n in zf.namelist() if n.endswith("_libretro.so")]
            if not names:
                raise RuntimeError(f"core zip for {core} contained no _libretro.so")
            data = zf.read(sorted(names)[0])
    except (httpx.HTTPError, zipfile.BadZipFile, OSError) as exc:
        raise RuntimeError(f"failed to download libretro core {core}: {exc}") from exc
    tmp = CORES_DIR / f"{core}_libretro.so.tmp"
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    os.replace(tmp, so)
    log.info("retroarch: core %s installed at %s", core, so)
    return so


def _ensure_core_assets(assets: dict[str, str]) -> None:
    """Link a core's asset directories into the RetroArch system dir.

    Some cores need data files the buildbot .so does not carry: the ppsspp
    core refuses to boot without PPSSPP's own asset tree. The container
    already ships those alongside the standalone app, so this points the core
    at them rather than downloading a second copy. Symlinks, so an image that
    updates the app updates what the core reads too.

    Best effort: a missing source is left alone, and the core reports the
    missing asset itself, which is a clearer error than one raised here.

    Args:
        assets: Path under `SYSTEM_DIR` to the directory on the image that
            should appear there.
    """
    for target, source in assets.items():
        src = Path(source)
        dest = SYSTEM_DIR / target
        if not src.is_dir():
            log.warning("retroarch: asset source %s is missing, skipping %s", src, dest)
            continue
        if dest.exists() and not dest.is_symlink():
            # Something real is already there; whoever put it there wins.
            continue
        try:
            if dest.is_symlink():
                if dest.readlink() == src:
                    continue
                dest.unlink()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(src, target_is_directory=True)
        except OSError as exc:
            log.error("retroarch: could not link assets %s -> %s: %s", dest, src, exc)
            continue
        log.info("retroarch: linked core assets %s -> %s", dest, src)


def _write_broker_cfg(thumbnail: bool = True) -> Path:
    """Write the minimal per-launch config, applied *on top of* the user's config.

    The stdin interface, the broker save dirs, and the joypad driver the
    streamed pads need; nothing else. The broker data directories are created
    first, and the file is written through a temp file.

    Args:
        thumbnail: Whether RetroArch should capture a thumbnail with each save
            state; off for cores where the framebuffer grab deadlocks.

    Returns:
        The path of the written config, `BROKER_CFG`.
    """
    RA_DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = (
        'stdin_cmd_enable = "true"\n'
        f'savestate_directory = "{STATE_DIR}"\n'
        f'savefile_directory = "{SAVE_DIR}"\n'
        'savestate_auto_save = "false"\n'
        'savestate_auto_load = "false"\n'
        'savestate_auto_index = "false"\n'
        f'savestate_thumbnail_enable = "{"true" if thumbnail else "false"}"\n'
        # The stdin QUIT must exit immediately, not arm a "press again"
        # confirmation.
        'confirm_quit = "false"\n'
        'quit_press_twice = "false"\n'
    )
    if JOYPAD_DRIVER:
        cfg += f'input_joypad_driver = "{JOYPAD_DRIVER}"\n'
    tmp = BROKER_CFG.with_suffix(".tmp")
    tmp.write_text(cfg)
    os.replace(tmp, BROKER_CFG)
    return BROKER_CFG


def _state_suffix(slot: int) -> str:
    """The slot suffix RetroArch appends to a state's content basename.

    Args:
        slot: The slot number; negative means the auto slot.

    Returns:
        `.state` for slot 0, `.state<n>` for slot n, `.state.auto` for the
        auto slot.
    """
    if slot < 0:
        return ".state.auto"
    return ".state" if slot == 0 else f".state{slot}"


def _state_name(base: str, slot: int) -> str:
    """RetroArch's state filename for a slot.

    Args:
        base: The content basename the state is named after.
        slot: The slot number; negative means the auto slot.

    Returns:
        `base.state` for slot 0, `base.state<n>` for slot n, and
        `base.state.auto` for the auto slot.
    """
    return base + _state_suffix(slot)


_STATE_SUFFIX_RE = re.compile(r"\.state(?:\d{1,2}|\.auto)?$")
"""Matches the state suffix RetroArch writes: `.state`, `.state<n>` (up to two digits) or `.state.auto`."""


def _is_state_name(filename: str, base: str) -> bool:
    """Whether `filename` is a state RetroArch would write for `base`, in any slot.

    Which slot does not matter: RomM keeps the library, so a stored state
    carries whatever slot it was captured in and this broker files it into its
    own. What does matter is the basename, since that is what says the state
    belongs to the content currently loaded.

    Args:
        filename: The bare filename a pushed state arrived under.
        base: The content basename of the loaded game.

    Returns:
        True when the name is a plain filename with `base`'s state prefix and
        a valid state suffix.
    """
    return (
        "/" not in filename
        and filename.startswith(f"{base}.state")
        and _STATE_SUFFIX_RE.search(filename) is not None
    )


def _state_snapshot(dir_path: Path, base: str) -> dict[Path, tuple[int, float]]:
    """Snapshot the state files of one content basename.

    Recursive because cores like dolphin redirect state paths into their own
    subdir (e.g. states/dolphin-emu/), where a flat lookup would never see
    the write.

    Args:
        dir_path: The savestate directory to walk.
        base: The content basename whose states to collect.

    Returns:
        Path to `(size, mtime)` for every file under `dir_path` whose name
        starts with `base.state`; empty when the directory is missing.
    """
    if not dir_path.is_dir():
        return {}
    prefix = f"{base}.state"
    snap: dict[Path, tuple[int, float]] = {}
    try:
        for p in dir_path.rglob("*"):
            if not p.is_file() or not p.name.startswith(prefix):
                continue
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
    except OSError:
        pass
    return snap


def _stable_file_wait(
    before: dict[Path, tuple[int, float]], dir_path: Path, base: str, target_name: str, deadline: float
) -> Optional[int]:
    """Poll until `target_name` is rewritten and holds a stable, non-empty size.

    Shared by the state-file wait and the thumbnail wait: both confirm a
    write landed the same way, a changed mtime since `before` followed by a
    non-zero size holding steady, only the watched filename and stop time
    differ. Zero bytes never counts as stable: a file RetroArch created but
    stalled on before writing anything would otherwise pass as complete.

    Args:
        before: The `_state_snapshot` taken before the write was triggered.
        dir_path: The savestate directory to watch.
        base: The content basename of the loaded game.
        target_name: The exact filename to watch for.
        deadline: A `time.monotonic()` value to stop polling at.

    Returns:
        The file's final size once it has changed since `before`, is
        non-empty, and has held that size for half a second; None once
        `deadline` passes first.
    """
    STABLE = 0.5
    POLL = 0.1
    last_size: Optional[int] = None
    stable_since: Optional[float] = None
    seen_change = False
    while time.monotonic() < deadline:
        after = _state_snapshot(dir_path, base)
        targets = [p for p in after if p.name == target_name]
        if not targets:
            time.sleep(POLL)
            continue
        cur_path = max(targets, key=lambda p: after[p][1])
        cur = after[cur_path]
        prev = before.get(cur_path)
        if not seen_change and (prev is None or prev[1] != cur[1]):
            seen_change = True
        if not seen_change:
            time.sleep(POLL)
            continue
        if last_size is None or cur[0] != last_size:
            last_size = cur[0]
            stable_since = time.monotonic()
        elif last_size > 0 and time.monotonic() - stable_since >= STABLE:
            return last_size
        time.sleep(POLL)
    return None


def _wait_for_state_file(
    before: dict[Path, tuple[int, float]], dir_path: Path, base: str, slot: int, timeout: float
) -> bool:
    """Poll until `slot`'s state file is rewritten and its size is stable and non-empty.

    That is the only reliable confirmation a save-state landed. The name has
    to match `slot` exactly: accepting any state file would let a save that
    landed on the wrong slot pass as a save of the requested one.

    Args:
        before: The `_state_snapshot` taken before the save was sent.
        dir_path: The savestate directory to watch.
        base: The content basename of the loaded game.
        slot: The slot whose file has to change.
        timeout: Seconds to keep polling.

    Returns:
        True once the slot's file has changed since `before`, is non-empty,
        and held a stable size for half a second; False on timeout, with a
        warning that names any other state files that changed instead.
    """
    target_name = _state_name(base, slot)
    size = _stable_file_wait(before, dir_path, base, target_name, time.monotonic() + timeout)
    if size is not None:
        log.info("retroarch: save state write complete: %s (%d bytes)", target_name, size)
        return True
    stray = sorted(
        p.name
        for p, meta in _state_snapshot(dir_path, base).items()
        if not p.name.endswith(".png") and before.get(p) != meta
    )
    log.warning(
        "retroarch: %s not confirmed on disk within %.1fs%s",
        target_name,
        timeout,
        f"; {', '.join(stray)} changed instead" if stray else "",
    )
    return False


def _newest_state(dir_path: Path, base: str, slot: int) -> Optional[Path]:
    """Find the most recently written file named for `base` and `slot`.

    Searched recursively, since a core may redirect states into its own
    subdir; where several copies exist the newest by mtime wins.

    Args:
        dir_path: The savestate directory to walk.
        base: The content basename of the loaded game.
        slot: The slot whose file to find.

    Returns:
        The newest matching file, or None when there is none or the directory
        cannot be read.
    """
    name = _state_name(base, slot)
    best: Optional[tuple[float, Path]] = None
    try:
        for p in dir_path.rglob(f"{base}.state*"):
            if not p.is_file() or p.name != name:
                continue
            st = p.stat()
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, p)
    except OSError:
        return None
    return best[1] if best is not None else None


def _disc_number(rel: Path) -> int:
    """The disc number a ROM path names, for ranking multi-disc candidates.

    Args:
        rel: The candidate's path relative to the ROM folder.

    Returns:
        The number matched by `_DISC_RE`, floored at 1, or 1 when the path
        names no disc.
    """
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _m3u_entries(playlist: Path) -> list[Path]:
    """Disc paths a .m3u lists, in playlist order, resolved absolute.

    Entry order is the order RetroArch assigns disc indices in, so the list
    index is the number of `DISK_NEXT` presses from the first disc.

    Args:
        playlist: The .m3u file to read.

    Returns:
        The listed discs, resolved relative to the playlist's directory, with
        blank lines and `#` comments skipped; empty when the file is unreadable.
    """
    try:
        text = playlist.read_text(errors="replace")
    except OSError as exc:
        log.warning("retroarch: could not read playlist %s: %s", playlist, exc)
        return []
    entries: list[Path] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append((playlist.parent / line).resolve())
    return entries


def _m3u_index_for_path(playlist: Path, target: Path) -> Optional[int]:
    """Disc index `target` occupies in `playlist`, or None if unlisted.

    Args:
        playlist: The .m3u file the session booted.
        target: The disc image to look for.

    Returns:
        The zero-based index of `target` among the playlist's entries, or None.
    """
    wanted = target.resolve()
    for index, entry in enumerate(_m3u_entries(playlist)):
        if entry == wanted:
            return index
    return None


def _pick_rom_file(candidates: Iterable[Path], base: Path, extensions: tuple[str, ...]) -> Optional[Path]:
    """Choose the file to boot out of a ROM folder's candidates.

    Hidden files, unsupported extensions, non-files and anything resolving
    outside `ROM_ROOT` are dropped. The rest are ranked so that the game beats
    its add-ons, the lowest disc number wins, then the platform's extension
    preference, then the shallowest path, then the name.

    Args:
        candidates: The paths found under `base`.
        base: The ROM folder the candidates came from.
        extensions: The platform's extensions, in preference order.

    Returns:
        The resolved path of the best candidate, or None if nothing qualifies.
    """
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in extensions:
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
            (
                is_addon,
                _disc_number(rel),
                extensions.index(ext),
                len(rel.parts),
                p.name.lower(),
                real,
            )
        )
    if not ranked:
        return None
    return min(ranked)[5]


def _find_reply(buf: bytes, prefixes: tuple[str, ...]) -> Optional[tuple[str, int]]:
    """Find the first reply line in `buf` beginning with one of `prefixes`.

    Matching is anchored to the start of a line: an unanchored search lets the
    basename inside a `GET_STATUS PLAYING <core>,<basename>` line answer a wait
    for `OK` or `NO`. The offset is counted in bytes rather than decoded
    characters, because the caller slices a byte buffer with it and a non-ASCII
    basename would otherwise leave half a line behind and corrupt every reply
    after it.

    Args:
        buf: The bytes read off RetroArch's stdout and not yet consumed.
        prefixes: Reply prefixes the caller is waiting for.

    Returns:
        The matched line and the number of bytes to drop from the front of
        `buf` to consume it, or None when no line matches. A match with no
        newline after it consumes the whole buffer, since the bare echoes of
        the `*_SLOT` commands and `GET_STATUS CONTENTLESS` are not
        newline-terminated.
    """
    start = 0
    while True:
        newline = buf.find(b"\n", start)
        end = len(buf) if newline == -1 else newline
        line = buf[start:end].decode("utf-8", errors="replace")
        if line.startswith(prefixes):
            return line, len(buf) if newline == -1 else newline + 1
        if newline == -1:
            return None
        start = newline + 1


class Retroarch(Emulator):
    """RetroArch driven over its stdin command interface.

    One launcher for every platform in `PLATFORMS`: `platform` is set from the
    activate payload before `launch`, and picks the core, the ROM extensions and
    the save scope. Saves, loads and quit go over stdin; replies come back on a
    stdout pipe drained by a reader thread, and saves are confirmed on disk.

    Attributes:
        name: Registry key, `retroarch`.
        display_name: Shown as "RetroArch".
        save_root: `RA_DATA_DIR`, the broker-managed data root.
        log_path: `RA_LOG_PATH`, where RetroArch's stderr goes.
        supports_states: Whether the loaded platform's core can save states.
        state_subtrees: `states`, where savestates land.
        supports_disc_swap: On; discs are swapped through the virtual tray.
        state_slot: `STATE_SLOT`, the one slot the broker works in.
        state_dir: `STATE_DIR`, the broker-managed savestate directory.
        term_timeout: Seconds `QUIT` gets before SIGTERM, from
            `RETROARCH_STOP_WAIT` (default 15).
        platform: The RomM platform slug, set before launch.
    """

    name = "retroarch"
    """Registry key for the RetroArch launcher."""
    display_name = "RetroArch"
    """Name the UI shows for RetroArch."""
    save_root = RA_DATA_DIR
    """The broker-managed data root; saves and states live under it."""
    state_subtrees = ("states",)
    """Savestates live under `states`, whichever subtrees the platform scopes the dump to."""
    log_path = RA_LOG_PATH
    """Where RetroArch's stderr is appended."""
    supports_disc_swap = True
    """Discs are swapped through RetroArch's virtual tray."""
    state_slot = STATE_SLOT
    """The one slot the broker works in."""
    state_dir = STATE_DIR
    """The broker-managed savestate directory."""
    term_timeout = float(os.environ.get("RETROARCH_STOP_WAIT", "15"))
    """Seconds the graceful exit gets before SIGTERM, from `RETROARCH_STOP_WAIT` (default 15).

    `QUIT` walks a graceful core teardown; give it room before SIGTERM.
    """

    def __init__(self) -> None:
        """Set up the reply buffer, reader thread slot and tray tracking for a session."""
        super().__init__()
        self.platform: Optional[str] = None
        """The RomM platform slug, set from the activate payload's `rom.platform` before launch."""
        self._rom_base: str = ""
        """The loaded content's basename, which RetroArch names its state and SRAM files after."""
        self._slot_homed = False
        """Whether the current slot has been parked on `STATE_SLOT` since launch."""
        self._launch_seq = 0
        """Launch generation, bumped on every launch and stop so stale background waits bail out."""
        self._thumbnail_enabled = True
        """Whether the loaded platform writes a save thumbnail; set from the platform table at launch."""
        self._resume_settle = RESUME_LOAD_SETTLE
        """Seconds between PLAYING and a deferred resume load; set from the platform table at launch."""
        self._stdout_buf = bytearray()
        """Replies read off RetroArch's stdout and not yet consumed."""
        self._stdout_lock = threading.Lock()
        """Guards `_stdout_buf` between the reader thread and the callers waiting on replies."""
        self._reply_lock = threading.Lock()
        """Serializes command/reply exchanges.

        Replies carry no request id, so two threads waiting at once would each
        be free to consume the other's line.
        """
        self._reader: Optional[threading.Thread] = None
        """The thread draining RetroArch's stdout into `_stdout_buf`."""
        self._playlist: Optional[Path] = None
        """The playlist this session booted, or None when the content was not an .m3u."""
        self._disc_index: int = 0
        """Where the tray currently sits.

        RetroArch has no way to read the mounted disc back, so the broker
        remembers what it did and steps relative to that.
        """
        self._disc_lock = threading.Lock()
        """Serializes `swap_disc` against itself and against the deferred resume load.

        Both poll for PLAYING and then act on the live process, and a
        `LOAD_STATE` landing inside a tray-settle window is the collision.
        """

    @property
    def save_subtrees(self) -> tuple[str, ...]:
        """Per-platform dump scope.

        Cores whose savefile dir is also their app-data dir (dolphin) narrow
        the archive to real save files.

        Returns:
            The platform's `save_subtrees`, or `("states", "saves")` by default.
        """
        info = _platform_info(self.platform)
        scoped = info.get("save_subtrees") if info else None
        return scoped or ("states", "saves")

    @property
    def supports_states(self) -> bool:
        """Whether the loaded platform's core can save and load states.

        A core that stubs out the libretro serialize calls (jaguar) opts out
        in its platform entry, so the flag has to follow the platform rather
        than the launcher.

        Returns:
            True unless the platform entry sets `savestate` false.
        """
        info = _platform_info(self.platform)
        return True if info is None else bool(info.get("savestate", True))

    def archive_core(self) -> Optional[str]:
        """The libretro core booting the loaded platform, or None when unmapped."""
        info = _platform_info(self.platform)
        return info["core"] if info else None

    @property
    def rom_extensions(self) -> tuple[str, ...]:
        """The loaded platform's ROM extensions, or none when no platform is mapped."""
        info = _platform_info(self.platform)
        return info["extensions"] if info else ()

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """File the core should boot for `path`.

        A file is taken as is. A folder is searched one level deep and ranked by
        `_pick_rom_file` against the platform's extensions.

        Args:
            path: The ROM as RomM delivered it, a file or a folder.

        Returns:
            The file to boot, or None when the platform is unmapped, the path
            is neither file nor folder, or nothing in it qualifies.
        """
        info = _platform_info(self.platform)
        if info is None:
            log.warning(
                "retroarch: no core mapped for platform %r; mapped: %s",
                self.platform,
                ", ".join(sorted(PLATFORMS)),
            )
            return None
        if path.is_file():
            # Defense in depth: api.py already validates path is under
            # ROM_ROOT before calling in, but this checks it independently
            # rather than trusting every future caller to do the same.
            try:
                if not path.resolve().is_relative_to(ROM_ROOT):
                    return None
            except OSError:
                return None
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError:
                return None
        return _pick_rom_file(candidates, path, info["extensions"])

    def _spawn_ra(self, cmd: list[str], env: dict[str, str]) -> None:
        """Spawn with a real stdout pipe (stderr to the log).

        stdout carries the command replies, so it must stay clean. A reader
        thread drains it into `_stdout_buf`. The pid is recorded the way the
        shared `_spawn` records it, so a broker that restarts mid-session can
        still reap the RetroArch it no longer has a handle on.

        Args:
            cmd: The argv to run.
            env: The environment to run it in.
        """
        log_fh = None
        try:
            log_fh = open(self.log_path, "ab", buffering=0)
            log_fh.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch ({' '.join(cmd)}) ===\n".encode()
            )
        except OSError:
            log_fh = None
        try:
            self._proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_fh if log_fh else subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            if log_fh:
                log_fh.close()
        _record_pid(self.name, self._proc.pid, cmd)
        self._stdout_buf = bytearray()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        """Reader thread body: copy RetroArch's stdout into `_stdout_buf` until it closes."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        fd = proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                with self._stdout_lock:
                    self._stdout_buf.extend(chunk)
        except (OSError, ValueError):
            pass

    def _wait_for_reply(self, prefixes: tuple[str, ...], timeout: float) -> Optional[str]:
        """Poll the reply buffer for a line beginning with one of `prefixes`.

        Only `_send` calls this, and only while holding `_reply_lock`, so the
        line consumed here belongs to the command that exchange just sent.

        Args:
            prefixes: Reply prefixes the caller is waiting for.
            timeout: Seconds to keep polling the buffer.

        Returns:
            The matched line, with everything up to and including it dropped
            from the buffer, or None on timeout or if the process dies first.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return None
            with self._stdout_lock:
                found = _find_reply(bytes(self._stdout_buf), prefixes)
                if found is not None:
                    line, consumed = found
                    del self._stdout_buf[:consumed]
                    return line
            time.sleep(0.05)
        return None

    def _write_cmd(self, cmd: str) -> bool:
        """Write one command line to RetroArch's stdin.

        Args:
            cmd: The command line to send; the newline is added here.

        Returns:
            True once the line is flushed. False when there is no running
            process or the pipe is broken, which the caller has to treat as a
            command that never reached the emulator: nothing but this return
            says a press was dropped.
        """
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            log.warning(
                "retroarch: dropping %r, no running process (platform=%s, rom=%s)",
                cmd, self.platform, self._rom_base,
            )
            return False
        try:
            proc.stdin.write(f"{cmd}\n".encode())
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            log.warning(
                "retroarch: could not send %r (platform=%s, rom=%s): %s",
                cmd, self.platform, self._rom_base, exc,
            )
            return False
        return True

    def _send(
        self, cmd: str, wait_prefix: Union[str, tuple[str, ...]], timeout: float = 5.0
    ) -> Optional[str]:
        """Send one command and return the reply it answers with.

        A reply carries nothing naming the command that produced it, so one
        exchange runs at a time and the buffer is emptied before the command
        goes out. Without that, a background `GET_STATUS` poll consumes the
        `OK` or the `LOAD_STATE_SLOT` echo another thread is waiting on, and a
        reply left over from an earlier command answers this one.

        Args:
            cmd: The command line to send; the newline is added here.
            wait_prefix: One reply prefix, or several of which the earliest
                matching line wins.
            timeout: Seconds to wait for the reply.

        Returns:
            The reply line, or None when the command could not be sent or no
            reply arrived in time.
        """
        prefixes = (wait_prefix,) if isinstance(wait_prefix, str) else tuple(wait_prefix)
        with self._reply_lock:
            with self._stdout_lock:
                self._stdout_buf.clear()
            if not self._write_cmd(cmd):
                return None
            return self._wait_for_reply(prefixes, timeout)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Start RetroArch on `rom_path` with the platform's core.

        Any running session is stopped first. The core is downloaded if
        missing, its assets linked, and the broker config written; then
        RetroArch starts fullscreen with that config appended. A resume is
        deferred to a background thread that waits for the game to be up.

        Args:
            rom_path: The file to boot, as returned by `resolve_rom_file`.
            resume_slot: The slot to load once the game is running, or None.

        Raises:
            RuntimeError: When `platform` has no core mapped, or the
                RetroArch binary (`RETROARCH_BIN`) is not on `PATH`.
        """
        self.stop()
        info = _platform_info(self.platform)
        if info is None:
            raise RuntimeError(
                f"no retroarch core mapped for platform {self.platform!r}; "
                f"mapped: {', '.join(sorted(PLATFORMS))}"
            )
        core = _ensure_core(info["core"], info.get("core_source"))
        _ensure_core_assets(info.get("assets", {}))
        self._thumbnail_enabled = info.get("thumbnail", True)
        self._resume_settle = info.get("resume_settle", RESUME_LOAD_SETTLE)
        cfg_path = _write_broker_cfg(self._thumbnail_enabled)

        env = base_launch_env()
        binary = os.environ.get("RETROARCH_BIN", "retroarch")
        # Probed against the PATH the child gets, not the broker's own: the
        # launch env appends /usr/games, where the packaged binary lives.
        launch_path = env.get("PATH")
        if "/" not in binary and shutil.which(binary, path=launch_path) is None:
            raise RuntimeError(f"retroarch binary not found in PATH ({launch_path}): {binary}")

        self._rom_base = rom_path.stem
        # A fresh process starts on whatever slot the config left it on.
        self._slot_homed = False
        self._launch_seq += 1
        seq = self._launch_seq
        self._playlist = rom_path if rom_path.suffix.lower() == ".m3u" else None
        self._disc_index = 0

        cmd = [
            binary,
            "-L",
            str(core),
            "--appendconfig",
            str(cfg_path),
            "--fullscreen",
            str(rom_path),
        ]
        log.info(
            "launching retroarch (core=%s, rom=%s, resume_slot=%s)",
            core.name,
            rom_path,
            resume_slot,
        )
        self._spawn_ra(cmd, env)

        # Slot 0 is a real slot here, so the gate is on the request, not on the
        # number: `if resume_slot` would drop every resume this broker asks for.
        if resume_slot is not None:
            if self.supports_states:
                threading.Thread(
                    target=self._deferred_load_state, args=(resume_slot, seq), daemon=True
                ).start()
            else:
                log.warning(
                    "retroarch: ignoring resume_slot=%s, platform %r has no savestate support",
                    resume_slot,
                    self.platform,
                )

    def _deferred_load_state(self, slot: int, seq: int) -> None:
        """Load `slot` once RetroArch reports the content PLAYING.

        Cores with no game running yet (Dolphin boot screen) reject loads, so
        this polls `GET_STATUS` first, then waits out `_resume_settle` and
        for the slot to hold a state file before loading. `_resume_settle`
        defaults to `RESUME_LOAD_SETTLE` but a platform can ask for longer:
        PPSSPP keeps loading and registering HLE module state well after
        RetroArch reports PLAYING, and a load attempted too early corrupts
        the core's HLE event table instead of restoring the game.

        Args:
            slot: The slot to load.
            seq: The launch generation this load belongs to; a relaunch or stop
                bumps `_launch_seq` and ends the wait.
        """
        deadline = time.monotonic() + RESUME_LOAD_WAIT
        while time.monotonic() < deadline:
            if self._launch_seq != seq or not self.alive():
                return
            reply = self._send("GET_STATUS", wait_prefix="GET_STATUS", timeout=2.0)
            if reply and reply.startswith("GET_STATUS PLAYING"):
                # Shares the tray lock with swap_disc: both poll for PLAYING
                # and then act, and a LOAD_STATE landing inside a swap's tray
                # settle is the collision this guards against.
                with self._disc_lock:
                    if self._launch_seq != seq or not self.alive():
                        return
                    time.sleep(self._resume_settle)
                    if not self.wait_for_state(deadline):
                        log.warning("resume: slot %d never got a state file", slot)
                        return
                    if self._launch_seq != seq:
                        return
                    log.info(
                        "resume: load of slot %d %s",
                        slot,
                        "delivered" if self.load_state(slot) else "failed",
                    )
                return
            time.sleep(1.0)
        log.warning(
            "resume: retroarch never reported PLAYING, slot %d not loaded", slot
        )

    def _wait_until_playing(self, deadline: float, seq: int) -> bool:
        """Block until the core reports a running game, or `deadline` passes.

        A tray command sent before the content is up is dropped, and there is
        no error to catch when it is. `seq` is the launch generation this
        wait belongs to, checked the same way `_deferred_load_state` checks
        `_launch_seq`, so a relaunch during the wait ends it rather than
        letting a stale wait later act on the new session.

        Args:
            deadline: A `time.monotonic()` value to give up at.
            seq: The launch generation this wait belongs to.

        Returns:
            True once `GET_STATUS` reports PLAYING; False on timeout, relaunch
            or the process dying.
        """
        while time.monotonic() < deadline:
            if self._launch_seq != seq or not self.alive():
                return False
            reply = self._send("GET_STATUS", wait_prefix="GET_STATUS", timeout=2.0)
            if reply and reply.startswith("GET_STATUS PLAYING"):
                return True
            time.sleep(1.0)
        return False

    def swap_disc(self, path: Path) -> bool:
        """Mount the playlist entry at `path` without restarting the core.

        Stepping is relative and wraps, because the command protocol has
        `DISK_NEXT` but no way to set an index or read the current one back.
        The tray is opened, stepped the needed number of times, and closed;
        the tracked index only moves once the session is confirmed to have
        outlived the sequence.

        Args:
            path: The disc image to mount; it must be listed in the session's
                playlist.

        Returns:
            True once the new disc is in the tray (or was already mounted);
            False when no playlist is loaded, the core is not running, the
            disc is unlisted, another swap or resume holds the tray, or the
            session ended mid-swap.
        """
        playlist = self._playlist
        if playlist is None:
            log.warning("disc swap: no playlist loaded for this session")
            return False
        if not self.alive():
            log.warning("disc swap: core is not running")
            return False
        entries = _m3u_entries(playlist)
        index = _m3u_index_for_path(playlist, path)
        if not entries or index is None:
            log.warning("disc swap: %s is not listed in %s", path, playlist)
            return False
        if index == self._disc_index:
            return True

        # Non-blocking: a second swap (or a resume load) already driving the
        # tray must fail fast rather than queue up behind a multi-second
        # sequence and then interleave with it.
        if not self._disc_lock.acquire(blocking=False):
            log.warning("disc swap: another disc swap or resume is already in progress")
            return False
        try:
            seq = self._launch_seq
            if not self._wait_until_playing(time.monotonic() + DISC_SWAP_WAIT, seq):
                log.warning("disc swap: core never reported a running game")
                return False
            if self._launch_seq != seq:
                log.warning("disc swap: session ended mid-swap, index %d not committed", index)
                return False

            steps = (index - self._disc_index) % len(entries)
            # A command that never reached the emulator leaves the tray
            # somewhere other than where the tracked index would say.
            if not self._write_cmd("DISK_EJECT_TOGGLE"):
                log.warning("disc swap: tray open not delivered, index %d not committed", index)
                return False
            time.sleep(DISC_TRAY_SETTLE)
            for _ in range(steps):
                # Re-checked between every step: a relaunch landing here must
                # not keep sending DISK_NEXT into the new process's stdin.
                if self._launch_seq != seq:
                    break
                if not self._write_cmd("DISK_NEXT"):
                    log.warning("disc swap: disc step not delivered, index %d not committed", index)
                    return False
                time.sleep(DISC_STEP_DELAY)
            if self._launch_seq == seq and not self._write_cmd("DISK_EJECT_TOGGLE"):
                log.warning("disc swap: tray close not delivered, index %d not committed", index)
                return False
            # The wait can outlast the session it started for (a relaunch bumps
            # _launch_seq) or the core can die mid-sequence; either way nothing
            # confirms the commands above actually landed, so the tracked index
            # only moves once both are still true.
            if self._launch_seq != seq or not self.alive():
                log.warning("disc swap: session ended mid-swap, index %d not committed", index)
                return False
            self._disc_index = index
            log.info("disc swap: now on index %d (%s)", index, path.name)
            return True
        finally:
            self._disc_lock.release()

    def _home_state_slot(self) -> bool:
        """Park RetroArch's current state slot on `STATE_SLOT`.

        Absolute, not relative: MINUS runs the slot down onto its -1 floor
        first, so this holds wherever the slot was, as long as it was no
        higher than `SLOT_HOME_STEPS` presses above the floor. Nothing in the
        protocol reads the slot back, so a player who cycled past that is only
        caught by the save confirmation missing the slot afterwards.

        Returns:
            True when every press was delivered. A dropped press leaves the
            slot somewhere unknown, so `_slot_homed` stays off and the next
            save homes again rather than trusting this one.
        """
        for _ in range(SLOT_HOME_STEPS):
            if not self._write_cmd("STATE_SLOT_MINUS"):
                log.warning(
                    "retroarch: slot homing lost a press (platform=%s, rom=%s)",
                    self.platform, self._rom_base,
                )
                return False
            time.sleep(SLOT_STEP_DELAY)
        for _ in range(STATE_SLOT + 1):
            if not self._write_cmd("STATE_SLOT_PLUS"):
                log.warning(
                    "retroarch: slot homing lost a press (platform=%s, rom=%s)",
                    self.platform, self._rom_base,
                )
                return False
            time.sleep(SLOT_STEP_DELAY)
        self._slot_homed = True
        return True

    def _try_save(self) -> bool:
        """Send `SAVE_STATE` once and confirm the file landed in `STATE_SLOT`.

        When the platform writes thumbnails, the sibling `.png` is also
        waited on (within what is left of the same deadline) once the state
        file itself is confirmed. A thumbnail that never lands is only logged:
        the state file is already good, and losing a preview should not read
        as losing the save.

        Returns:
            True when the slot's state file changed on disk, is non-empty,
            and held a stable size within `STATE_CONFIRM_WAIT`.
        """
        before = _state_snapshot(STATE_DIR, self._rom_base)
        if not self._write_cmd("SAVE_STATE"):
            return False
        deadline = time.monotonic() + STATE_CONFIRM_WAIT
        if not _wait_for_state_file(before, STATE_DIR, self._rom_base, STATE_SLOT, STATE_CONFIRM_WAIT):
            return False
        if self._thumbnail_enabled:
            self._wait_for_state_thumbnail(before, deadline)
        return True

    def _wait_for_state_thumbnail(self, before: dict[Path, tuple[int, float]], deadline: float) -> None:
        """Wait for the working slot's save thumbnail, without failing the save on a miss.

        Args:
            before: The `_state_snapshot` taken before `SAVE_STATE` was sent.
            deadline: A `time.monotonic()` value shared with the state-file
                wait, not a fresh window of its own.
        """
        target_name = _state_name(self._rom_base, STATE_SLOT) + ".png"
        if _stable_file_wait(before, STATE_DIR, self._rom_base, target_name, deadline) is None:
            log.warning(
                "retroarch: save thumbnail %s not confirmed on disk (platform=%s, rom=%s)",
                target_name, self.platform, self._rom_base,
            )

    def save_state(self, slot: int) -> bool:
        """Save the running game into `STATE_SLOT`.

        `slot` is what RomM asked for and is ignored: this saves into
        `STATE_SLOT` and the caller reads the effective slot back off
        `state_slot`.

        Homing costs a couple of seconds, so it runs once per launch and then
        only to recover: a save landing on another slot means the player moved
        it with their own hotkeys, and re-homing puts the next one back.

        Args:
            slot: The slot RomM asked for; ignored.

        Returns:
            True once the state file is confirmed on disk; False when the core
            is not running or both the save and the re-homed retry miss.
        """
        if not self.alive():
            return False
        if not self._slot_homed and not self._home_state_slot():
            log.warning(
                "retroarch: state slot not parked on %d, saving into whatever slot is current "
                "(platform=%s, rom=%s)",
                STATE_SLOT, self.platform, self._rom_base,
            )
        if self._try_save():
            return True
        if not self.alive():
            return False
        log.info("retroarch: save missed slot %d, re-homing and retrying", STATE_SLOT)
        self._home_state_slot()
        return self._try_save()

    def load_state(self, slot: int) -> bool:
        """Load `STATE_SLOT` into the running game.

        `LOAD_STATE_SLOT` is absolute and does not move the current slot, so
        this needs no homing. The echo carries no success bit and a load writes
        nothing to disk, so an empty slot is ruled out here instead.

        Args:
            slot: The slot RomM asked for; ignored in favour of `STATE_SLOT`.

        Returns:
            True once RetroArch echoes the command; False when the core is not
            running, the slot holds no state file, or no echo arrives within
            `LOAD_ACK_WAIT`.
        """
        if not self.alive():
            return False
        if self.state_path() is None:
            log.warning("load state: slot %d holds no state file", STATE_SLOT)
            return False
        echo = self._send(
            f"LOAD_STATE_SLOT {STATE_SLOT}",
            wait_prefix="LOAD_STATE_SLOT",
            timeout=LOAD_ACK_WAIT,
        )
        if echo is None:
            log.warning("load state: retroarch did not acknowledge slot %d", STATE_SLOT)
            return False
        return True

    def state_path(self) -> Optional[Path]:
        """The newest state file for the loaded content in `STATE_SLOT`, or None.

        Returns:
            The file found by `_newest_state`, or None before a launch has set
            the content basename or when the slot is empty.
        """
        if not self._rom_base:
            return None
        return _newest_state(STATE_DIR, self._rom_base, STATE_SLOT)

    def state_screenshot_path(self) -> Optional[Path]:
        """The thumbnail RetroArch wrote beside the working slot's state, or None.

        RetroArch writes the thumbnail as `<state file>.png` beside the state,
        so it is only meaningful next to the state it was taken with.

        Returns:
            The `.png` next to `state_path`, or None when either is missing.
        """
        state = self.state_path()
        if state is None:
            return None
        shot = state.with_name(f"{state.name}.png")
        return shot if shot.is_file() else None

    def clear_working_slot(self) -> None:
        """Delete every content's state in the broker's slot, and its thumbnail.

        RetroArch names a state after the loaded content, so a leftover for a
        different game is already invisible to a resume. One for the *same*
        game is not: in a shared container the previous player's state would
        be served to the next, since nothing about the name says whose it is.

        Searched recursively, since a core may redirect states into a subdir
        of its own.
        """
        if not STATE_DIR.is_dir():
            return
        suffix = _state_suffix(STATE_SLOT)
        try:
            stale_states = [p for p in STATE_DIR.rglob(f"*{suffix}") if p.is_file()]
        except OSError as exc:
            log.warning("could not scan %s for stale states: %s", STATE_DIR, exc)
            return
        for stale in stale_states:
            try:
                stale.unlink()
                log.info("cleared stale state %s", stale.name)
            except OSError as exc:
                log.warning("could not clear stale state %s: %s", stale.name, exc)
                continue
            shot = stale.with_name(f"{stale.name}.png")
            try:
                shot.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("could not clear stale state thumbnail %s: %s", shot.name, exc)

    def state_target(self, filename: str) -> Optional[Path]:
        """Where a pushed state called `filename` belongs.

        RetroArch looks a state up by the name it derives from the loaded
        content, so the target is that name in this broker's slot and a pushed
        one only has to say which content it belongs to.

        Args:
            filename: The name the pushed state was stored under.

        Returns:
            The existing slot file when there is one, else the slot's path at
            the top of `STATE_DIR`; None when nothing is loaded or the name is
            not a state RetroArch would write for the loaded content.
        """
        if not self._rom_base or not _is_state_name(filename, self._rom_base):
            return None
        existing = self.state_path()
        # Cores that redirect states into their own subdir keep writing there,
        # so a push has to land where the last save did, not at the top.
        if existing is not None:
            return existing
        return STATE_DIR / _state_name(self._rom_base, STATE_SLOT)

    def _flush_sram(self) -> bool:
        """Ask RetroArch to write the game's SRAM out before the archive is dumped.

        `SAVE_FILES` answers `OK` or `NO`, and a refusal has to be reported:
        the dump that follows ships whatever is on disk either way, so an
        unflushed session ships the previous save as if it were current.

        Returns:
            True when RetroArch answered `OK`; False when it refused or never
            answered, both of which are logged with the loaded content.
        """
        reply = self._send("SAVE_FILES", wait_prefix=("OK", "NO"), timeout=SAVE_FILES_WAIT)
        if reply is None:
            log.error(
                "retroarch: no SAVE_FILES reply within %.1fs, the save archive may ship stale "
                "save data (platform=%s, rom=%s, core=%s)",
                SAVE_FILES_WAIT, self.platform, self._rom_base, self.archive_core(),
            )
            return False
        if reply.startswith("NO"):
            log.error(
                "retroarch: SAVE_FILES refused, SRAM was not flushed and the save archive may "
                "ship stale save data (platform=%s, rom=%s, core=%s)",
                self.platform, self._rom_base, self.archive_core(),
            )
            return False
        log.info("retroarch: SRAM flushed for %s", self._rom_base)
        return True

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Save state when asked, flush SRAM, and quit RetroArch.

        The state save is skipped when `slot` is None or the platform entry
        opts out of `savestate`. The SRAM flush always runs so the save dump
        ships current save data, then the process is quit through `_quit`. A
        flush RetroArch refuses is logged by `_flush_sram`; the exit still goes
        ahead, since holding the session open would not make the refusal any
        more likely to succeed.

        Args:
            slot: The slot to save into, or None to exit without writing a
                state.

        Returns:
            A dict with `{"state_saved", "state_slot", "state_file"}`: whether
            the save was confirmed, `STATE_SLOT` (or None when no save was
            asked for), and the saved file's `{"path", "size", "mtime"}` or
            None.
        """
        saved = False
        state_file = None
        if self.alive():
            if slot is not None and not self.supports_states:
                log.warning(
                    "retroarch: not saving state on exit, platform %r has no savestate support",
                    self.platform,
                )
            if slot is not None and self.supports_states:
                saved = self.save_state(slot)
                if saved:
                    p = self.state_path()
                    if p is not None:
                        try:
                            st = p.stat()
                        except OSError as exc:
                            log.warning("could not stat saved state %s: %s", p, exc)
                            saved = False
                        else:
                            state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}
            self._flush_sram()
        self._quit()
        return {
            "state_saved": saved,
            "state_slot": STATE_SLOT if slot is not None else None,
            "state_file": state_file,
        }

    def _quit(self) -> None:
        """Quit RetroArch gracefully over stdin, escalating to `stop` if it stays up.

        `QUIT` is sent, then sent again after `QUIT_CONFIRM_GAP` if the process
        is still running, and given `QUIT_WAIT` to exit. A graceful exit
        forgets the handle; anything else falls through to the base `stop` and
        its SIGTERM.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None and proc.stdin is not None:
            log.info("stopping %s (pid %d) via QUIT", self.name, proc.pid)
            try:
                proc.stdin.write(b"QUIT\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=QUIT_CONFIRM_GAP)
                self._forget()
                log.info("%s exited gracefully", self.name)
                return
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() is None:
                log.info(
                    "%s (pid %d) still up after first QUIT, pressing again",
                    self.name, proc.pid,
                )
                try:
                    proc.stdin.write(b"QUIT\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            try:
                proc.wait(timeout=QUIT_WAIT)
                self._forget()
                log.info("%s exited gracefully", self.name)
                return
            except subprocess.TimeoutExpired:
                log.warning(
                    "%s did not exit after QUIT, escalating to SIGTERM", self.name
                )
        super().stop()

    def stop(self) -> None:
        """Stop RetroArch, invalidating any in-flight deferred state load before the kill."""
        self._launch_seq += 1
        super().stop()
