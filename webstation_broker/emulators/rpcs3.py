"""RPCS3 (PlayStation 3) emulator launcher.

Handles config.yml/ipc.yml patching, the PKG install hook, PINE-driven boot
verification, and save states via the emulator's own exit-on-save hotkey.

Boot formats: decrypted .iso images and disc folder rips
(PS3_GAME/USRDIR/EBOOT.BIN) boot directly. A .pkg is an installer, not an
image: the first activation installs it via `--headless --installpkg` into
`dev_hdd0/game/<TITLEID>/` and later activations boot the installed
EBOOT.BIN. .rap/.edat licenses are plain files copied into
`dev_hdd0/home/00000001/exdata/`.

Save states: unlike PCSX2, RPCS3 has no live save/load. Pressing the
save-state hotkey always terminates the process once the state is written,
and loading one means booting RPCS3 pointed straight at the .SAVESTAT file
(auto-detected by magic bytes, not extension) instead of a normal boot
target, functionally the same as booting a ROM. So the broker follows
DuckStation's model rather than PCSX2's: no live supports_states, a resume
loads by choosing what to boot before the process starts, and a save
happens by triggering the hotkey and waiting for the process to exit rather
than by keeping it running. States are per-title, at
DATA_DIR/savestates/<title_id>/, which is a sibling of dev_hdd0 rather than
something under it; a symlink into dev_hdd0/savestates is what lets the
existing save archive (keyed to dev_hdd0-relative paths) carry it without
moving save_root out from under the subtrees already archived.

PINE: RPCS3's IPC is PINE-compatible but only implements the protocol's
generic opcodes (memory access, version/title/status queries) - there is no
save/load-state opcode the way PCSX2's variant adds one. The broker uses it
for nothing but boot verification (MsgStatus) and, for boots (a bare .iso)
whose title id the on-disk layout cannot supply, a title id lookup (MsgID)
once the game is confirmed running.

RPCS3 installs no signal handler, so a plain kill (no state requested, or
the hotkey/wait having failed) is a safe stop: whatever the game already
wrote to its own save data is on disk the moment it's written. cellSaveData
saves under `dev_hdd0/home/00000001/savedata/`, cellGameData saves under
`dev_hdd0/game/`.

The emulator is expected to be brought to a working state in desktop mode
(firmware, GPU settings, controllers) before automated launching.
"""

import hashlib
import logging
import os
import re
import shutil
import signal
import socket as _socket
import struct
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Iterable
from pathlib import Path
from threading import Lock, Thread
from typing import Optional

from .. import settings
from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))


def _default_data_dir() -> str:
    """Resolve RPCS3's Linux data root.

    $XDG_CONFIG_HOME/rpcs3 when set, otherwise ~/.config/rpcs3. Config,
    dev_flash and dev_hdd0 all live under it.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "rpcs3")
    return os.path.join(os.environ.get("HOME", "/config"), ".config/rpcs3")


DATA_DIR = Path(os.environ.get("RPCS3_DATA_DIR", _default_data_dir()))
CONFIG_PATH = DATA_DIR / "config.yml"
IPC_PATH = DATA_DIR / "ipc.yml"
DEV_HDD0 = DATA_DIR / "dev_hdd0"
USER_HOME = DEV_HDD0 / "home" / "00000001"
EXDATA_DIR = USER_HOME / "exdata"
GAME_DIR = DEV_HDD0 / "game"
# RPCS3 always writes states here, never under dev_hdd0; see _ensure_sstate_link.
SSTATE_ROOT = DATA_DIR / "savestates"
_SSTATE_LINK = DEV_HDD0 / "savestates"
RPCS3_LOG_PATH = Path(os.environ.get("RPCS3_LOG_PATH", "/config/rpcs3.log"))
# PKG decryption and archive extraction can both run minutes for multi-GB
# dumps, so this timeout is shared between them.
INSTALL_TIMEOUT = float(os.environ.get("RPCS3_INSTALL_TIMEOUT", "1800"))

# Boot formats, best first: decrypted ISO and PKG installer beat a bare
# EBOOT so a folder holding both the rip and its installer picks the image.
# Archives sort last since booting one costs an extraction first.
ROM_EXTENSIONS = (".iso", ".pkg", ".bin", ".self", ".elf", ".7z", ".zip", ".rar")
# EBOOT.BIN sits three levels down in a disc rip (PS3_GAME/USRDIR/EBOOT.BIN).
_ROM_SEARCH_GLOBS = ("*", "*/*", "*/*/*")
# Only executables named EBOOT.* are bootable; other .bin/.self files in a
# rip (licenses, sdata) are not.
_EBOOT_EXTS = (".bin", ".self", ".elf")
_ARCHIVE_EXTS = (".7z", ".zip", ".rar")
_GB = 1024**3
_LICENSE_EXTS = (".rap", ".edat")
# Title ids are alphanumeric (BLUS30443, NPUB30638). Everything parsed out of
# a PKG header or a PARAM.SFO is attacker-supplied and gets joined onto
# GAME_DIR/SSTATE_ROOT, so anything with a separator in it is refused rather
# than allowed to walk out of those trees.
_TITLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


# Decrypted PS3 dumps run 2-25GB. An archive is extracted once into
# CACHE_DIR and reused on relaunch; with the cache disabled (the default)
# an archived ROM is refused outright, since re-extracting multiple GB on
# every launch and discarding it buys nothing.
CACHE_DIR = Path(os.environ.get("RPCS3_CACHE_DIR", str(DATA_DIR / "extracted")))
# Off by default: local disk headroom is limited on a typical host, and an
# unattended cache would otherwise grow until an operator notices. 30GB
# comfortably fits one large title's decompressed size for anyone who opts
# in via RPCS3_CACHE_ENABLED.
CACHE_ENABLED = _truthy(os.environ.get("RPCS3_CACHE_ENABLED", "false"))
CACHE_MAX_GB = float(os.environ.get("RPCS3_CACHE_MAX_GB", "30"))
# Stands in for a member listing that could not be read. A compressed PS3
# dump expands several-fold, so budgeting the archive's own size would wave
# through exactly the extractions the space guard exists to stop.
EXPANSION_FACTOR = float(os.environ.get("RPCS3_ARCHIVE_EXPANSION", "4.0"))
_LAST_ACCESSED_MARKER = ".last_accessed"
_SCRATCH_DIR_NAME = ".scratch"
"""Subdirectory of CACHE_DIR every in-progress extraction is staged under.

Staging keeps a partial extraction out of CACHE_DIR's top level, so a game
dir either does not exist or is complete: a process killed mid-extraction
leaves scratch to be reclaimed rather than a truncated EBOOT.BIN the next
launch would cache-hit on forever.
"""

# Serializes cache-dir mutation: eviction picking a victim, extraction of a
# new one, and the boot-target lookup that follows all touch the same
# CACHE_DIR tree, so one launch's eviction can't rmtree a directory another
# launch is mid-extracting into or about to boot from.
_CACHE_LOCK = Lock()

# config.yml values forced before every launch. RPCS3 fills missing keys
# with defaults, so a partial file is a valid config. Keyed ("section", key);
# section "" is a flat top-level key, for ipc.yml which has no sections.
_CONFIG_PATCHES: dict[tuple[str, str], str] = {
    ("Miscellaneous", "Automatically start games after boot"): "true",
    # Game quit (or XMB exit) ends the process, so alive() tracks the game.
    ("Miscellaneous", "Exit RPCS3 when process finishes"): "true",
    # The labwc session gives no focus guarantees; a focus-loss pause would
    # freeze the game invisibly.
    ("Miscellaneous", "Pause emulation on RPCS3 focus loss"): "false",
}
_IPC_PATCHES: dict[tuple[str, str], str] = {
    # Off by default; the broker only ever reads status/title over it, so the
    # port is left at whatever it already is (Linux uses the fixed
    # $XDG_RUNTIME_DIR/rpcs3.sock path below regardless of the configured port).
    ("", "IPC Server enabled"): "true",
}

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")
PINE_SOCKET = Path(XDG_RUNTIME_DIR) / "rpcs3.sock"

# RPCS3's generic PINE opcodes (rpcs3/3rdparty/pine/pine_server.h). No
# save/load-state opcode exists in this set, unlike PCSX2's PINE variant.
_PINE_MSG_ID = 0x0C
_PINE_MSG_STATUS = 0x0F
# The two opcodes used here answer with four bytes and a short title id. A
# peer declaring more than this is misframed or wedged, and the declared
# size is what the read loop would otherwise sit and accumulate toward.
_PINE_MAX_REPLY_BYTES = 64 * 1024

BOOT_WAIT = float(os.environ.get("RPCS3_BOOT_WAIT", "90.0"))
# A full state write (compressed PS3 RAM + VRAM) is a heavier write than the
# other cores' states, so this is generous.
STATE_WAIT = float(os.environ.get("RPCS3_STATE_WAIT", "60.0"))

_XDOTOOL = os.environ.get("XDOTOOL_BIN", "xdotool")
# StartupWMClass in rpcs3/rpcs3.desktop. --no-gui suppresses the Qt main
# window entirely, so the render window is the only one carrying this class
# once a game is running - no title filtering needed the way PPSSPP's shared
# menu/game class needs.
_WINDOW_CLASS = "rpcs3"
# Default binding for gw_savestate_1 (rpcs3qt/shortcut_settings.cpp). RPCS3
# has four such shortcuts, but every one just writes a new auto-numbered
# state (rpcs3/Emu/savestate_utils.cpp), so slot 1 is as good as any other.
# Configurable because it's a GUI-rebindable shortcut, unlike the rest of
# this module's constants.
_SAVE_STATE_KEY = os.environ.get("RPCS3_SAVE_STATE_KEY", "ctrl+alt+1")


def _rpcs3_bin() -> str:
    return os.environ.get("RPCS3_BIN", "/opt/rpcs3/AppRun")


def _launch_env() -> dict[str, str]:
    env = base_launch_env()
    # The AppImage's desktop entry pins xcb; the Qt wayland platform is not
    # bundled.
    env["QT_QPA_PLATFORM"] = "xcb"
    return env


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Optional[Path]:
    """Best boot target among `candidates`, ranked by ROM_EXTENSIONS preference.

    Candidates escaping ROM_ROOT by symlink are dropped, as are archives
    when the extraction cache is disabled: those only reach a boot target
    through CACHE_DIR, so picking one would fail deep inside `launch()`
    instead of here.

    Args:
        candidates: Paths found by the caller's directory search.
        base: The directory the search started from, used to rank shallower
            hits above deeper ones.

    Returns:
        The resolved path to boot, or None when nothing qualifies.
    """
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
            continue
        if ext in _ARCHIVE_EXTS and not CACHE_ENABLED:
            log.debug(
                "rpcs3: skipping %s, %s needs the extraction cache "
                "(set RPCS3_CACHE_ENABLED=true to boot this format)",
                p.name,
                ext,
            )
            continue
        if ext in _EBOOT_EXTS and not p.name.upper().startswith("EBOOT"):
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
        ranked.append((ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real))
    if not ranked:
        return None
    return min(ranked)[3]


def _patch_yaml_file(path: Path, patches: dict[tuple[str, str], str]) -> None:
    """Force config patches into a two-level YAML file before every launch.

    Each ("section", "key") -> value pair is applied to unindented
    "Section:" headers over 2-space "key: value" lines, or a flat
    "key: value" when section is "". RPCS3 fills missing keys with
    defaults, so a partial file is a valid config.

    Args:
        path: The config.yml or ipc.yml to rewrite.
        patches: The values to force, keyed ("section", key).

    Raises:
        RuntimeError: If the file could not be rewritten. Launching anyway
            would leave exit-on-game-finish and the IPC server at whatever
            they already were, which silently breaks `alive()` and the boot
            watchdog while the activate still reports success.
    """
    try:
        if not path.exists():
            log.info("%s not found, seeding one", path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        lines = path.read_text().splitlines()
        section = ""
        applied: set[tuple[str, str]] = set()
        new_lines: list[str] = []
        for line in lines:
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                section = line.strip()[:-1]
                new_lines.append(line)
                continue
            stripped = line.strip()
            matched = False
            for (sec, key), val in patches.items():
                if section == sec and stripped.startswith(f"{key}:"):
                    prefix = "  " if sec else ""
                    new_lines.append(f"{prefix}{key}: {val}")
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        missing = [(s, k, v) for (s, k), v in patches.items() if (s, k) not in applied]
        if missing:
            present = {
                ln.strip()[:-1]
                for ln in new_lines
                if ln and not ln[0].isspace() and ln.rstrip().endswith(":")
            }
            for sec, key, val in missing:
                if not sec:
                    new_lines.append(f"{key}: {val}")
                elif sec in present:
                    out: list[str] = []
                    inserted = False
                    for ln in new_lines:
                        out.append(ln)
                        if not inserted and ln.strip() == f"{sec}:":
                            out.append(f"  {key}: {val}")
                            inserted = True
                    new_lines = out
                else:
                    new_lines.extend([f"{sec}:", f"  {key}: {val}"])
                    present.add(sec)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(path)
    except Exception as exc:
        log.exception("%s patch failed, broker settings NOT applied", path.name)
        raise RuntimeError(f"could not apply broker settings to {path}: {exc}") from exc


def _patch_config() -> None:
    _patch_yaml_file(CONFIG_PATH, _CONFIG_PATCHES)


def _patch_ipc() -> None:
    _patch_yaml_file(IPC_PATH, _IPC_PATCHES)


def _ensure_sstate_link() -> None:
    """Make dev_hdd0/savestates resolve to DATA_DIR/savestates.

    RPCS3 always writes states to DATA_DIR/savestates, a sibling of
    dev_hdd0 rather than something under it, but save_root has to stay
    dev_hdd0 so the archive subtrees already in use (home/00000001/savedata,
    game/) keep resolving as they always have. A symlink is what lets
    "savestates" join save_subtrees without moving save_root.
    """
    try:
        SSTATE_ROOT.mkdir(parents=True, exist_ok=True)
        DEV_HDD0.mkdir(parents=True, exist_ok=True)
        if _SSTATE_LINK.is_symlink():
            if _SSTATE_LINK.resolve() != SSTATE_ROOT.resolve():
                _SSTATE_LINK.unlink()
                _SSTATE_LINK.symlink_to(SSTATE_ROOT, target_is_directory=True)
        elif not _SSTATE_LINK.exists():
            _SSTATE_LINK.symlink_to(SSTATE_ROOT, target_is_directory=True)
        else:
            # Deleting whatever put a real directory there is worse than
            # running without states in the archive, but the operator has to
            # be told which of the two trees the dump is actually reading.
            log.warning(
                "rpcs3: %s is a real directory, not a link to %s, so savestates rpcs3 "
                "writes will be missing from the save archive; move or remove it to "
                "restore state archiving",
                _SSTATE_LINK,
                SSTATE_ROOT,
            )
    except OSError as exc:
        log.warning("could not link savestates dir: %s", exc)


def _kill_headless_group(proc: "subprocess.Popen[bytes]", what: str) -> None:
    """Kill a timed-out headless run and everything it spawned.

    RPCS3_BIN is the AppImage's AppRun wrapper, which execs the real
    emulator as a child, so signalling the handle alone reaps the wrapper
    and leaves rpcs3 itself running against /config. The whole process
    group goes instead. SIGKILL rather than SIGTERM: rpcs3 installs no
    signal handler, and this run has already blown a timeout measured in
    minutes.

    Args:
        proc: The handle for the process group leader.
        what: The operation being killed, for the log lines.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError) as exc:
        log.warning("rpcs3 %s: could not kill process group of pid %d: %s", what, proc.pid, exc)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log.error("rpcs3 %s: pid %d survived SIGKILL", what, proc.pid)


def _run_headless(args: list[str], what: str) -> None:
    """Run a one-shot `rpcs3 --headless` operation (installer CLI).

    The process exits when the operation completes. Exit code is always 0,
    so callers verify success by checking the expected files afterwards. It
    runs in its own process group so a timeout can take the AppImage's real
    emulator child down with the wrapper.

    Args:
        args: Arguments appended after `--headless`.
        what: Short label for the operation, used in logs and errors.

    Raises:
        RuntimeError: If the run does not finish within INSTALL_TIMEOUT.
    """
    cmd = [_rpcs3_bin(), "--headless", *args]
    log.info("rpcs3 %s: %s", what, " ".join(cmd))
    try:
        log_fh = open(RPCS3_LOG_PATH, "ab", buffering=0)
        log_fh.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {what} ({' '.join(cmd)}) ===\n".encode()
        )
    except OSError:
        log_fh = None
    try:
        proc = subprocess.Popen(
            cmd,
            env=_launch_env(),
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        if log_fh:
            log_fh.close()
    try:
        proc.wait(timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.error("rpcs3 %s: no exit within %.0fs, killing the process group", what, INSTALL_TIMEOUT)
        _kill_headless_group(proc, what)
        raise RuntimeError(f"rpcs3 {what} did not finish within {INSTALL_TIMEOUT:.0f}s")


def _gamedata_dirs() -> list[Path]:
    """List CellGameData save dirs under game/.

    Everything except installed titles (which have a bootable EBOOT.BIN)
    and RPCS3's ＄locks dir.
    """
    dirs = []
    if GAME_DIR.is_dir():
        for d in sorted(GAME_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith(("$", "＄")):
                continue
            if (d / "USRDIR" / "EBOOT.BIN").is_file():
                continue
            dirs.append(d)
    return dirs


def _installed_title_dirs() -> set[str]:
    """Names of the title dirs under game/, minus RPCS3's own ＄locks bookkeeping."""
    if not GAME_DIR.is_dir():
        return set()
    try:
        return {
            d.name
            for d in GAME_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(("$", "＄", "."))
        }
    except OSError as exc:
        log.warning("rpcs3: could not list installed titles in %s: %s", GAME_DIR, exc)
        return set()


def _valid_title_id(title_id: Optional[str]) -> Optional[str]:
    """Return `title_id` when it is a plausible title id, else None.

    Every id here is parsed out of a ROM file's own bytes and then joined
    onto GAME_DIR or SSTATE_ROOT, so a value carrying a path separator would
    address a directory outside those trees.
    """
    if title_id and _TITLE_ID_RE.match(title_id):
        return title_id
    if title_id:
        log.warning("rpcs3: refusing implausible title id %r", title_id)
    return None


def _sfo_title_id(sfo: Path) -> Optional[str]:
    """TITLE_ID string from a PARAM.SFO (key/data table pairs indexed from a fixed-size header)."""
    try:
        data = sfo.read_bytes()
    except OSError:
        return None
    if len(data) < 0x14 or data[:4] != b"\x00PSF":
        return None
    key_start = int.from_bytes(data[0x08:0x0C], "little")
    data_start = int.from_bytes(data[0x0C:0x10], "little")
    entries = int.from_bytes(data[0x10:0x14], "little")
    for i in range(entries):
        off = 0x14 + i * 16
        entry = data[off : off + 16]
        if len(entry) < 16:
            return None
        key_off = int.from_bytes(entry[0:2], "little")
        data_len = int.from_bytes(entry[4:8], "little")
        data_off = int.from_bytes(entry[12:16], "little")
        key = data[key_start + key_off : key_start + key_off + 16].split(b"\0", 1)[0]
        if key == b"TITLE_ID":
            value = data[data_start + data_off : data_start + data_off + data_len]
            return _valid_title_id(value.split(b"\0", 1)[0].decode("ascii", "replace"))
    return None


def _archive_dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.name == _LAST_ACCESSED_MARKER:
            continue
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _cache_size_bytes() -> int:
    if not CACHE_DIR.is_dir():
        return 0
    return sum(_archive_dir_size(d) for d in CACHE_DIR.iterdir() if d.is_dir())


def _touch_last_accessed(game_dir: Path) -> None:
    try:
        (game_dir / _LAST_ACCESSED_MARKER).write_text(str(time.time()))
    except OSError as exc:
        log.warning("rpcs3 cache: could not update last-accessed marker for %s: %s", game_dir, exc)


def _require_room(needed_bytes: int, archive_name: str) -> None:
    """Refuse an extraction that cannot fit before any of it is written.

    Eviction leaves two ceilings standing: an empty cache still cannot hold a
    title larger than CACHE_MAX_GB, and the cap counts only the cache's own
    contents, not the free space on the filesystem it shares with the rest of
    /config. Without this the unpack starts anyway, spends minutes filling the
    disk, and dies on a write error from inside the extractor, having taken
    the free space every other service on that filesystem needs with it.

    One figure covers both ceilings here: the staged tree is renamed into
    place rather than copied, so what is on disk at the peak is what stays.

    Args:
        needed_bytes: Bytes the finished extraction takes, on disk and in the cache.
        archive_name: The archive being extracted, named in the error.

    Raises:
        RuntimeError: If the cache cap or the filesystem cannot hold it.
    """
    max_bytes = int(CACHE_MAX_GB * _GB)
    current = _cache_size_bytes()
    if current + needed_bytes > max_bytes:
        raise RuntimeError(
            f"{archive_name} would leave about {needed_bytes / _GB:.1f} GB cached, more than "
            f"RPCS3_CACHE_MAX_GB ({CACHE_MAX_GB:.0f} GB) allows with "
            f"{current / _GB:.1f} GB already there"
        )
    try:
        free = shutil.disk_usage(CACHE_DIR).free
    except OSError as exc:
        log.warning("rpcs3 cache: could not read free space on %s: %s", CACHE_DIR, exc)
        return
    if free < needed_bytes:
        raise RuntimeError(
            f"{archive_name} needs about {needed_bytes / _GB:.1f} GB to extract, but only "
            f"{free / _GB:.1f} GB is free on {CACHE_DIR}"
        )


def _clear_scratch() -> None:
    """Remove every staged extraction under CACHE_DIR. Callers must hold _CACHE_LOCK.

    The lock is what makes this safe: no extraction can be mid-flight while
    it is held, so anything still sitting here was orphaned by a process
    that died.
    """
    scratch_root = CACHE_DIR / _SCRATCH_DIR_NAME
    if not scratch_root.is_dir():
        return
    for entry in scratch_root.iterdir():
        log.warning("rpcs3 cache: removing orphaned scratch dir %s", entry.name)
        shutil.rmtree(entry, ignore_errors=True)


def sweep_stale_extractions() -> None:
    """Remove extraction scratch dirs orphaned by a crashed broker process.

    `tempfile.TemporaryDirectory` cleans up on normal exit, but a killed
    process leaves its scratch dir behind forever. Call once at broker
    startup: the only other caller is an extraction, which a library of
    already-extracted (or never-archived) games may never run again.
    """
    with _CACHE_LOCK:
        _clear_scratch()


def _evict_lru(needed_bytes: int, keep: str) -> None:
    """Evict least-recently-used extracted games until `needed_bytes` fits within CACHE_MAX_GB.

    Args:
        needed_bytes: Additional bytes that must fit under the cache cap.
        keep: The cache key currently being (re-)extracted, so a stale
            entry for it already removed by the caller is never chosen.
    """
    if not CACHE_ENABLED or not CACHE_DIR.is_dir():
        return
    max_bytes = int(CACHE_MAX_GB * _GB)
    current = _cache_size_bytes()
    while current + needed_bytes > max_bytes:
        candidates = []
        for game_dir in CACHE_DIR.iterdir():
            if not game_dir.is_dir() or game_dir.name in (keep, _SCRATCH_DIR_NAME):
                continue
            marker = game_dir / _LAST_ACCESSED_MARKER
            try:
                mtime = marker.stat().st_mtime if marker.exists() else 0.0
            except OSError:
                mtime = 0.0
            candidates.append((mtime, game_dir))
        if not candidates:
            log.warning("rpcs3 cache: nothing left to evict under the %.0f GB cap", CACHE_MAX_GB)
            return
        candidates.sort(key=lambda c: c[0])
        victim = candidates[0][1]
        victim_size = _archive_dir_size(victim)
        log.info("rpcs3 cache: evicting %s (least recently used)", victim.name)
        try:
            shutil.rmtree(victim)
        except OSError as exc:
            log.warning("rpcs3 cache: could not evict %s: %s", victim, exc)
            return
        current -= victim_size


def _is_safe_boot_candidate(candidate: Path, root_real: Path) -> bool:
    """Check that `candidate` is safe to boot.

    False for anything but a regular file resolving inside root_real, so a
    symlink planted by the archive cannot point rpcs3 at a path elsewhere
    on the host.
    """
    try:
        return candidate.is_file() and candidate.resolve().is_relative_to(root_real)
    except OSError:
        return False


def _archive_boot_target(root: Path) -> Optional[Path]:
    """Find the best boot target inside an extracted archive.

    An EBOOT.BIN anywhere (disc rip or PKG-installed layout), or failing
    that a bare decrypted .iso an archive may hold instead of an unpacked
    JB folder.
    """
    root_real = root.resolve()
    for eboot in root.rglob("EBOOT.BIN"):
        if _is_safe_boot_candidate(eboot, root_real):
            return eboot
    for iso in root.rglob("*.iso"):
        if _is_safe_boot_candidate(iso, root_real):
            return iso
    return None


def _run_extractor(cmd: list[str], what: str) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{what} failed to run: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{what} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _reject_unsafe_members(dest: Path, members: list[str]) -> None:
    """Reject any archive member whose path would land outside dest.

    A `../` (or absolute) member path can escape dest on extraction (Zip
    Slip); this is checked before anything is written.
    """
    dest_real = dest.resolve()
    for member in members:
        target = (dest / member).resolve()
        if target != dest_real and dest_real not in target.parents:
            raise RuntimeError(f"archive member escapes extraction dir: {member}")


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract `zf` into dest after rejecting any Zip Slip member.

    zf.extractall() writes a member's path verbatim, so a `../` name in
    the archive can escape dest; reject any member that would first.
    """
    _reject_unsafe_members(dest, zf.namelist())
    zf.extractall(dest)


def _rar_member_paths(archive: Path) -> list[str]:
    """List member paths from an archive.

    Bare paths from `unrar lb`, one per line, no header or column
    formatting to parse around.
    """
    listing = _run_extractor(["unrar", "lb", "-y", str(archive)], f"unrar list ({archive.name})")
    return [line for line in listing.splitlines() if line]


def _7z_member_paths(archive: Path) -> list[str]:
    """List member paths from an archive.

    Parsed from `7z l -slt`, the only 7z listing mode that gives a full
    untruncated path per entry. Everything before the `----------`
    separator describes the archive itself, not its contents.
    """
    listing = _run_extractor(["7z", "l", "-slt", str(archive)], f"7z list ({archive.name})")
    _, _, body = listing.partition("----------\n")
    return [line[len("Path = ") :] for line in body.splitlines() if line.startswith("Path = ")]


def _reject_escaped_tree(dest: Path) -> None:
    """Post-extraction safety net for the .rar/.7z paths.

    unrar/7z extraction is trusted to confine writes under dest -- 7z's own
    extractor collapses `..` path components against the destination root --
    but the pre-extraction member-name check above parses each tool's own
    *text listing* to decide what's "safe" before anything is written, and a
    member name holding a raw control character can render differently (or
    get silently merged with the next line) in that listing than in the
    archive's real central directory. Rather than trust the listing as a
    proxy for what actually landed on disk, walk the real result: any
    symlink whose target resolves outside dest is a real filesystem object
    the listing-based check could never catch (no such thing exists until
    after extraction), and any entry that isn't contained under dest at all
    means the extractor's own traversal protection didn't hold -- both are
    treated as fatal for the whole archive rather than silently dropped.
    """
    dest_real = dest.resolve()
    # followlinks=False means os.walk never descends through a symlinked
    # directory, but it still lists one in dirnames for its parent's
    # iteration -- exactly where this loop catches it.
    for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
        base = Path(dirpath)
        for name in dirnames + filenames:
            p = base / name
            try:
                target_real = p.resolve()
            except OSError as exc:
                raise RuntimeError(f"could not resolve extracted member {p}: {exc}") from exc
            if target_real != dest_real and dest_real not in target_real.parents:
                raise RuntimeError(f"extracted member escapes cache dir: {p}")


def _extract_archive(archive: Path, dest: Path) -> None:
    ext = archive.suffix.lower()
    log.info("rpcs3: extracting %s (%s)", archive.name, ext)
    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive) as zf:
                _safe_extract_zip(zf, dest)
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError(f"zip extraction of {archive.name} failed: {exc}") from exc
    elif ext == ".rar":
        _reject_unsafe_members(dest, _rar_member_paths(archive))
        _run_extractor(["unrar", "x", "-y", str(archive), f"{dest}/"], f"unrar ({archive.name})")
    else:
        # .7z, plus a fallback attempt for any other archive format 7z can
        # identify (RAR5, tar-in-7z JB dumps, etc).
        _reject_unsafe_members(dest, _7z_member_paths(archive))
        _run_extractor(["7z", "x", "-y", str(archive), f"-o{dest}"], f"7z ({archive.name})")
    if ext != ".zip":
        _reject_escaped_tree(dest)


def _cache_key(archive: Path) -> str:
    """Cache dir name for archive: its stem plus a short hash of the file's identity.

    A bare stem collides two archives that share a name but differ in
    extension, and survives a same-named re-upload with different content,
    either of which would otherwise serve up whatever is sitting in the old
    cache dir as if it were the new ROM. The hash therefore covers the
    resolved path, the size, and the nanosecond mtime: same-second rewrites
    are exactly how a library sync replaces a dump, so second granularity
    would let a replacement keep the old key.

    Args:
        archive: The archive being extracted.

    Returns:
        The cache directory name for this archive.

    Raises:
        RuntimeError: If the archive cannot be read. Falling back to the
            bare name here would hand back the collision-prone key this
            function exists to avoid, and the extraction that follows would
            fail on the same unreadable file anyway.
    """
    try:
        st = archive.stat()
        fingerprint = f"{archive.resolve()}:{st.st_size}:{st.st_mtime_ns}"
    except OSError as exc:
        log.error("rpcs3 cache: could not read %s to key its extraction: %s", archive, exc)
        raise RuntimeError(f"could not read {archive.name} to key its extraction: {exc}") from exc
    digest = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    return f"{archive.stem}-{digest}"


def _sum_listed_sizes(listing: str, prefix: str) -> Optional[int]:
    """Total the integers on every `prefix` line of an extractor's listing.

    Args:
        listing: The extractor's stdout.
        prefix: Line prefix introducing an uncompressed member size, matched
            after stripping indentation ("Size =" for 7z, "Size:" for unrar).

    Returns:
        The total, or None when the listing carried no such line at all.
    """
    total = 0
    found = False
    for line in listing.splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix) :].strip()
        if value.isdigit():
            total += int(value)
            found = True
    return total if found else None


def _listed_extracted_size(archive: Path) -> Optional[int]:
    """Uncompressed total the archive's own member listing reports.

    Args:
        archive: The archive to interrogate.

    Returns:
        The sum of the members' uncompressed sizes, or None when the listing
        could not be read or carried no sizes.
    """
    ext = archive.suffix.lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(archive) as zf:
                return sum(i.file_size for i in zf.infolist()) or None
        if ext == ".rar":
            listing = _run_extractor(
                ["unrar", "lt", "-y", str(archive)], f"unrar sizes ({archive.name})"
            )
            return _sum_listed_sizes(listing, "Size:")
        listing = _run_extractor(["7z", "l", "-slt", str(archive)], f"7z sizes ({archive.name})")
        _, _, body = listing.partition("----------\n")
        return _sum_listed_sizes(body, "Size =")
    except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
        log.warning("rpcs3: could not read the member sizes of %s: %s", archive.name, exc)
        return None


def _extraction_size(archive: Path) -> int:
    """Bytes the finished extraction of `archive` is expected to occupy.

    The archive's own listing is the only honest answer: a `.7z`/`.rar` PS3
    dump expands several-fold, so sizing the extraction from the compressed
    file would let `_require_room` wave through exactly the extractions that
    then fill the disk. EXPANSION_FACTOR only stands in when no listing can
    be read.

    Args:
        archive: The archive about to be extracted.

    Returns:
        The size to budget, or 0 when the archive cannot be stat'd at all.
    """
    listed = _listed_extracted_size(archive)
    if listed is not None:
        return listed
    try:
        compressed = archive.stat().st_size
    except OSError as exc:
        log.warning("rpcs3: could not size %s for the space guard: %s", archive.name, exc)
        return 0
    log.warning(
        "rpcs3: %s has no readable member listing, budgeting %.1fx its compressed size",
        archive.name,
        EXPANSION_FACTOR,
    )
    return int(compressed * EXPANSION_FACTOR)


def _extract_and_cache(archive: Path, emulator: Emulator) -> Path:
    """Extract archive into CACHE_DIR, reusing a cached extraction when possible.

    Reuses a prior extraction keyed by `_cache_key` when one already holds
    a bootable target. The archive is unpacked under `_SCRATCH_DIR_NAME`
    and only renamed to the persistent game_dir once a boot target is
    confirmed, so game_dir either does not exist or holds a complete
    extraction.

    Holds _CACHE_LOCK for the whole call: eviction, extraction, and the
    boot-target lookup all touch the same CACHE_DIR tree, so a second
    launch racing in here must wait rather than potentially evicting the
    directory this one is mid-extracting into or about to boot from.

    Args:
        archive: The archive to extract.
        emulator: The launching emulator; `emulator.extraction_phase` is set
            while this runs so RomM can poll it, and cleared again before
            returning or raising.

    Raises:
        RuntimeError: If the archive cannot be read to key it, the extraction
            cannot fit in the cache or on the disk, extraction fails, the
            extracted archive holds no EBOOT.BIN or decrypted .iso to boot,
            or the finished extraction cannot be moved to its cache key.
        OSError: If CACHE_DIR or a scratch dir cannot be created at all.
    """
    with _CACHE_LOCK:
        key = _cache_key(archive)
        game_dir = CACHE_DIR / key

        if game_dir.is_dir():
            boot = _archive_boot_target(game_dir)
            if boot is not None:
                log.info("rpcs3 cache hit: %s (boot target: %s)", archive.name, boot.name)
                _touch_last_accessed(game_dir)
                return boot
            log.warning("rpcs3 cache: %s has no boot target, re-extracting", archive.name)
            shutil.rmtree(game_dir, ignore_errors=True)

        # Set before eviction, not after: eviction can rmtree tens of GB
        # under the lock, and a caller polling extraction_phase should see
        # that stall rather than an idle-looking None.
        emulator.extraction_phase = "extracting_archive"
        try:
            needed = _extraction_size(archive)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            # Orphaned scratch is un-evictable but still counts toward the
            # cap, so reclaim it before sizing the cache rather than letting
            # it push real entries out.
            _clear_scratch()
            _evict_lru(needed, key)
            _require_room(needed, archive.name)

            # Scratch lives under CACHE_DIR so the staged extraction shares
            # a filesystem with game_dir: the rename below is then atomic
            # rather than a cross-device copy.
            scratch_root = CACHE_DIR / _SCRATCH_DIR_NAME
            scratch_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f"{key}-", dir=str(scratch_root)) as scratch:
                staged = Path(scratch) / "extracted"
                staged.mkdir()
                _extract_archive(archive, staged)
                if _archive_boot_target(staged) is None:
                    raise RuntimeError(
                        f"{archive.name} extracted but held no EBOOT.BIN or decrypted .iso"
                    )
                # The rmtree above uses ignore_errors, so game_dir can still
                # be sitting there non-empty and the rename then fails.
                try:
                    staged.replace(game_dir)
                except OSError as exc:
                    log.error(
                        "rpcs3 cache: could not move the extraction of %s into %s: %s",
                        archive.name, game_dir, exc,
                    )
                    raise RuntimeError(
                        f"could not cache the extraction of {archive.name}: {exc}"
                    ) from exc
        finally:
            emulator.extraction_phase = None

        # Re-looked up under game_dir rather than carried over from staged: a
        # relative symlink resolves against wherever it now sits, so a member
        # contained inside the scratch tree can point outside this one.
        boot = _archive_boot_target(game_dir)
        if boot is None:
            raise RuntimeError(f"{archive.name} extracted but held no EBOOT.BIN or decrypted .iso")
        _touch_last_accessed(game_dir)
        log.info("rpcs3: extracted %s, booting %s", archive.name, boot)
    return boot


def _pkg_title_id(pkg: Path) -> Optional[str]:
    """Title ID from the PKG header.

    The content id at offset 0x30 embeds it as chars 7-15
    (`UP0001-BLUS30443_00-...` -> BLUS30443).
    """
    try:
        with open(pkg, "rb") as f:
            header = f.read(0x60)
    except OSError:
        return None
    if len(header) < 0x60 or header[:4] != b"\x7fPKG":
        return None
    content_id = header[0x30:0x60].split(b"\0", 1)[0].decode("ascii", "replace")
    if len(content_id) >= 16 and content_id[6] == "-":
        return _valid_title_id(content_id[7:16])
    return None


def _rom_siblings(rom: Path) -> list[Path]:
    """Regular files sitting beside `rom` that are really inside the ROM library.

    Containment is tested on the resolved path, so a symlink planted next to
    a ROM cannot pull a file from elsewhere on the host into the emulator's
    data dir.

    Args:
        rom: The ROM whose directory is listed.

    Returns:
        The qualifying files, sorted by name, `rom` itself included.
    """
    root = settings.rom_root()
    found: list[Path] = []
    try:
        entries = sorted(rom.parent.iterdir())
    except OSError as exc:
        log.warning("rpcs3: could not list %s beside %s: %s", rom.parent, rom.name, exc)
        return [rom]
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            real = entry.resolve()
        except OSError as exc:
            log.warning("rpcs3: skipping unreadable %s beside %s: %s", entry, rom.name, exc)
            continue
        if not real.is_relative_to(root):
            log.warning(
                "rpcs3: skipping %s beside %s, it resolves to %s outside %s",
                entry.name, rom.name, real, root,
            )
            continue
        found.append(entry)
    return found


def _fallback_title_id(rom: Path, before: set[str]) -> Optional[str]:
    """Title id of the dir a PKG install created, when the header gave none.

    Args:
        rom: The PKG that was installed, named in the log lines.
        before: Title dir names present under game/ before the install ran.

    Returns:
        The new bootable title dir's name, or None when the install created
        no bootable dir.
    """
    new = sorted(n for n in _installed_title_dirs() - before if _valid_title_id(n))
    bootable = [n for n in new if (GAME_DIR / n / "USRDIR" / "EBOOT.BIN").is_file()]
    if not bootable:
        log.error(
            "rpcs3: installing %s created no bootable title dir (new dirs: %s), see %s",
            rom.name, ", ".join(new) or "none", RPCS3_LOG_PATH,
        )
        return None
    if len(bootable) > 1:
        # A base plus its update or DLC normally lands in one dir; several
        # means the packages were not all for the same title after all.
        log.warning(
            "rpcs3: installing %s created several bootable title dirs (%s), booting %s",
            rom.name, ", ".join(bootable), bootable[0],
        )
    return bootable[0]


def _install_pkgs(rom: Path) -> Path:
    """Install a .pkg rom if it is not already installed.

    A base game, its update and its DLC all carry the same title id in their
    content id, so the packages installed alongside `rom` are the ones whose
    id matches its own, and the licenses copied into exdata are the ones
    naming that id. Selecting by title id rather than by "everything in the
    folder" is what keeps RomM's flat `<library>/<platform>/*.pkg` layout
    from installing the entire platform on one activate; a game with its own
    folder still gets its whole set. Already-installed titles skip straight
    to the boot path.

    Args:
        rom: The .pkg RomM resolved for this session.

    Returns:
        Path to the EBOOT.BIN to boot.

    Raises:
        RuntimeError: If the title id cannot be determined, or the install
            produces no EBOOT.BIN.
    """
    title_id = _pkg_title_id(rom)
    siblings = _rom_siblings(rom)
    if title_id:
        pkgs = [rom] + [
            p
            for p in siblings
            if p != rom and p.suffix.lower() == ".pkg" and _pkg_title_id(p) == title_id
        ]
        licenses = [
            p for p in siblings if p.suffix.lower() in _LICENSE_EXTS and title_id in p.name
        ]
    else:
        # Nothing to match siblings against, and a wrong guess installs or
        # licenses another title's content.
        log.warning(
            "rpcs3: %s carries no readable title id, installing it on its own", rom.name
        )
        pkgs = [rom]
        licenses = []

    if licenses:
        EXDATA_DIR.mkdir(parents=True, exist_ok=True)
        for lic in licenses:
            dest = EXDATA_DIR / lic.name
            if dest.exists():
                continue
            try:
                shutil.copy2(lic, dest)
            except OSError as exc:
                log.warning("rpcs3: could not install license %s for %s: %s", lic.name, title_id, exc)
                continue
            log.info("rpcs3: installed license %s for %s", lic.name, title_id)

    if title_id:
        eboot = GAME_DIR / title_id / "USRDIR" / "EBOOT.BIN"
        if eboot.is_file():
            log.info("rpcs3: %s already installed, booting %s", title_id, eboot)
            return eboot

    before = _installed_title_dirs()
    for pkg in pkgs:
        _run_headless(["--installpkg", str(pkg)], f"pkg install ({pkg.name})")

    if title_id is None:
        title_id = _fallback_title_id(rom, before)
    if title_id is None:
        raise RuntimeError(f"could not determine title id for {rom.name}, see {RPCS3_LOG_PATH}")
    eboot = GAME_DIR / title_id / "USRDIR" / "EBOOT.BIN"
    if not eboot.is_file():
        raise RuntimeError(
            f"pkg install of {rom.name} produced no {eboot}, see {RPCS3_LOG_PATH}"
        )
    log.info("rpcs3: installed %s, booting %s", title_id, eboot)
    return eboot


def _rom_title_id(rom: Path) -> Optional[str]:
    """Title id a boot target carries in its own layout or header.

    An installed title boots from `game/<title id>/USRDIR/EBOOT.BIN`, a PKG
    embeds the id in its content id, and a disc rip carries a PARAM.SFO
    beside USRDIR. A bare .iso and an archive carry none of those, so their
    id only turns up over PINE once the game is confirmed running.

    Args:
        rom: The resolved boot target.

    Returns:
        The title id, or None when the path alone cannot supply one.
    """
    try:
        if rom.suffix.lower() == ".pkg":
            return _pkg_title_id(rom)
        if rom.name.upper().startswith("EBOOT"):
            if rom.is_relative_to(GAME_DIR):
                return _valid_title_id(rom.parent.parent.name)
            return _sfo_title_id(rom.parent.parent / "PARAM.SFO")
    except (OSError, ValueError) as exc:
        log.warning("rpcs3: could not read a title id for %s: %s", rom, exc)
    return None


def _state_dir_for(serial: Optional[str]) -> Optional[Path]:
    return (SSTATE_ROOT / serial) if serial else None


def _state_snapshot(serial: Optional[str]) -> dict:
    d = _state_dir_for(serial)
    if d is None or not d.is_dir():
        return {}
    snap = {}
    for pattern in ("*.SAVESTAT", "*.SAVESTAT.zst", "*.SAVESTAT.gz"):
        for p in d.glob(pattern):
            try:
                st = p.stat()
                snap[p] = (st.st_size, st.st_mtime)
            except OSError:
                pass
    return snap


def _all_state_files() -> dict[Path, tuple[int, float]]:
    """Every savestate currently under SSTATE_ROOT, keyed by path.

    A boot target whose title id only PINE can supply reaches
    `Rpcs3.clear_working_slot` with nothing to scope a clear to, so by the
    time the id is known the title's dir can hold both a previous session's
    leftovers and this session's restored states. A snapshot taken before the
    restore ran is what tells the two apart.

    Returns:
        Path to (size, mtime) for every state file, empty when SSTATE_ROOT
        does not exist or cannot be listed.
    """
    snap: dict[Path, tuple[int, float]] = {}
    if not SSTATE_ROOT.is_dir():
        return snap
    try:
        title_dirs = [d for d in SSTATE_ROOT.iterdir() if d.is_dir()]
    except OSError as exc:
        log.warning("rpcs3: could not list %s to record leftover savestates: %s", SSTATE_ROOT, exc)
        return snap
    for d in title_dirs:
        snap.update(_state_snapshot(d.name))
    return snap


def _newest_state(serial: Optional[str]) -> Optional[Path]:
    """Newest state file for `serial`.

    RPCS3 already isolates every title under its own
    savestates/<title_id>/ dir, so newest-by-mtime in there is always this
    title's own most recent capture.
    """
    best: Optional[tuple[float, Path]] = None
    for p, (_size, mtime) in _state_snapshot(serial).items():
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best is not None else None


def _changed_state(serial: Optional[str], before: dict) -> Optional[Path]:
    best: Optional[tuple[float, Path]] = None
    for p, cur in _state_snapshot(serial).items():
        if before.get(p) != cur:
            mtime = cur[1]
            if best is None or mtime > best[0]:
                best = (mtime, p)
    return best[1] if best is not None else None


def _clear_leftover_states(title_id: str, before: dict) -> None:
    """Drop savestates that predate a deferred clear's pre-restore snapshot.

    clear_working_slot could not scope its clear to `title_id` because the
    id was not readable from the boot target's path, so whatever the
    archive restore just wrote and whatever a stale, never-cleared previous
    session left behind now sit in the same dir. Anything still matching an
    entry in `before` unchanged was never touched by the restore and is
    dropped; anything new or changed is the restore's (or this session's
    own) and stays.

    Args:
        title_id: The title id now known for this session.
        before: The pre-restore snapshot from `_all_state_files`.
    """
    for p, stamp in _state_snapshot(title_id).items():
        if before.get(p) != stamp:
            continue
        try:
            p.unlink()
        except OSError as exc:
            log.warning("rpcs3: could not clear leftover savestate %s: %s", p, exc)
        else:
            log.debug("rpcs3: cleared leftover savestate %s for %s", p, title_id)


def _launch_pids(pid: Optional[int]) -> list[int]:
    """Every pid in the launch `pid` leads, itself included.

    RPCS3_BIN is the AppImage's AppRun wrapper and the emulator runs as a
    child of it, so the handle the broker holds is not the process that
    writes a savestate and its own descriptor list says nothing about the
    write. `_spawn` starts the wrapper in a session of its own, which makes
    the process group the way to name the whole launch.

    Args:
        pid: The process group leader, or None when the broker holds no
            handle on it.

    Returns:
        The pids sharing `pid`'s process group, or just `pid` when /proc
        cannot be scanned.
    """
    if pid is None:
        return []
    try:
        pgid = os.getpgid(pid)
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            candidate = int(entry.name)
            try:
                if os.getpgid(candidate) == pgid:
                    found.append(candidate)
            except OSError:
                continue
    except OSError as exc:
        log.debug("rpcs3: could not scan /proc for the process group of pid %s: %s", pid, exc)
        return [pid]
    return found or [pid]


def _holds_open(pid: Optional[int], path: Path) -> bool:
    """Tell whether the launch `pid` leads still has `path` open.

    A file the writer has not closed yet is a write still in flight, however
    long its size happens to sit still: RPCS3 compresses a state as it writes
    it, and a busy or stalled host can leave the file the same size for a
    full poll window mid-write.

    Args:
        pid: The emulator process, or None when the broker holds no handle on it.
        path: The state file being watched.

    Returns:
        True only when the descriptor is confirmed open. No pid, a `/proc`
        that cannot be read, and a process already gone all read as False, so
        the size test stays the answer where this one cannot contribute.
    """
    target = os.path.realpath(path)
    for candidate in _launch_pids(pid):
        try:
            for fd in Path(f"/proc/{candidate}/fd").iterdir():
                try:
                    if os.path.realpath(fd) == target:
                        return True
                except OSError:
                    continue
        except OSError as exc:
            log.debug(
                "rpcs3: could not read the open files of pid %s for %s: %s", candidate, path, exc
            )
    return False


def _wait_for_state_write(
    serial: Optional[str],
    before: dict,
    deadline: float,
    pid: Optional[int] = None,
) -> Optional[Path]:
    """Poll until the title's savestate write stabilizes, or the deadline passes.

    A write counts as finished once the file is non-empty, rpcs3 has closed
    it, and its size has held steady for STABLE_SECS. RPCS3 exiting is not
    itself proof the write finished: the hotkey ends the process once the
    state is captured, but nothing here can tell whether compression to disk
    still trails that. Size alone is not proof either, since an emulator
    stalled mid-write holds a steady size over a truncated file, and a state
    that has only been created is zero bytes of nothing.

    Args:
        serial: Title id scoping the state dir to watch.
        before: Snapshot from `_state_snapshot` taken before the hotkey was sent.
        deadline: `time.monotonic` value to give up at.
        pid: The running rpcs3 process, whose open descriptors say whether the
            write has finished; None falls back to the size test alone.

    Returns:
        The settled state file, or None on timeout.
    """
    STABLE_SECS = 0.5
    POLL_SECS = 0.2
    target: Optional[Path] = None
    last_size: Optional[int] = None
    stable_since: Optional[float] = None
    while time.monotonic() < deadline:
        p = _changed_state(serial, before)
        if p is None:
            time.sleep(POLL_SECS)
            continue
        try:
            size = p.stat().st_size
        except OSError:
            time.sleep(POLL_SECS)
            continue
        if target != p:
            target, last_size, stable_since = p, size, time.monotonic()
        elif size != last_size:
            last_size, stable_since = size, time.monotonic()
        elif (
            stable_since is not None
            and time.monotonic() - stable_since >= STABLE_SECS
            and last_size
            and not _holds_open(pid, target)
        ):
            log.info("save state write complete: %s (%d bytes)", target.name, last_size)
            return target
        time.sleep(POLL_SECS)
    if target is not None:
        log.warning(
            "save state write never settled before the deadline: %s (%s bytes, pid %s)",
            target,
            last_size,
            pid,
        )
    return None


def _pine_recv_exact(sock: _socket.socket, n: int, deadline: float) -> Optional[bytes]:
    """Read exactly `n` bytes, giving the whole read one shared deadline.

    Args:
        sock: The connected PINE socket.
        n: Bytes to read.
        deadline: `time.monotonic()` value the read must complete by. A
            per-recv timeout alone never expires against a peer that dribbles
            one byte at a time, so the budget is spent, not restarted.

    Returns:
        The bytes read, or None if the peer closed or the deadline passed.
    """
    buf = b""
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("PINE read timed out with %d of %d bytes on %s", len(buf), n, PINE_SOCKET)
            return None
        sock.settimeout(remaining)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _pine_request(opcode: int, payload: bytes = b"", timeout: float = 5.0) -> Optional[bytes]:
    """Send one PINE request and return its reply payload.

    Wire format (LE): u32 total size, u8 opcode, payload; reply is u32
    size, u8 result (0 = OK), payload. Same wire format PCSX2's PINE
    variant uses; RPCS3 just implements a smaller opcode set (no
    save/load-state) on top of it.

    Args:
        opcode: The PINE opcode to send.
        payload: Opcode arguments, if any.
        timeout: Seconds the whole exchange gets, connect through reply.

    Returns:
        The reply payload, or None if the request failed or the socket is
        unreachable.
    """
    packet = struct.pack("<IB", 5 + len(payload), opcode) + payload
    deadline = time.monotonic() + timeout
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(PINE_SOCKET))
            sock.sendall(packet)
            header = _pine_recv_exact(sock, 5, deadline)
            if header is None:
                return None
            size, result = struct.unpack("<IB", header)
            if size < 5 or size > _PINE_MAX_REPLY_BYTES:
                log.warning(
                    "PINE opcode 0x%02X declared an unusable reply of %d bytes on %s",
                    opcode, size, PINE_SOCKET,
                )
                return None
            body = b""
            if size > 5:
                body = _pine_recv_exact(sock, size - 5, deadline) or b""
            if result != 0:
                log.warning("PINE opcode 0x%02X rejected (result %d)", opcode, result)
                return None
            return body
    except OSError as exc:
        log.warning("PINE request failed on %s (opcode 0x%02X): %s", PINE_SOCKET, opcode, exc)
        return None


def _pine_status() -> Optional[int]:
    """0 running, 1 paused, 2 shutdown, None if IPC is down or disabled."""
    body = _pine_request(_PINE_MSG_STATUS, timeout=2.0)
    if body is None or len(body) < 4:
        return None
    return struct.unpack("<I", body[:4])[0]


def _pine_title_id() -> Optional[str]:
    body = _pine_request(_PINE_MSG_ID, timeout=2.0)
    if not body:
        return None
    return _valid_title_id(body.split(b"\0", 1)[0].decode("ascii", "replace"))


class Rpcs3(Emulator):
    """RPCS3 (PS3) launcher: exit-only savestates via PINE, plus archive boot support."""

    name = "rpcs3"
    display_name = "RPCS3"
    save_root = DEV_HDD0
    state_subtrees = ("savestates",)
    """States land under the symlinked `savestates` dir; see `_ensure_sstate_link`."""
    _restoring = False
    _pending_rom: Optional[Path] = None
    """Boot target this activate resolved, or None when none was resolved.

    `resolve_rom_file` is the last thing activate calls before
    `clear_working_slot`, and the launcher is built fresh per activate, so
    recording the target there is what lets the clear name the title it is
    about to boot.
    """
    _session_serial: Optional[str] = None
    _leftover_snapshot: Optional[dict] = None
    """Pre-restore savestate snapshot from clear_working_slot, or None.

    Only set when clear_working_slot ran without a title id to scope its
    clear to (a bare .iso or an archive). Consumed and cleared the moment
    this session's title id becomes known, by _clear_leftover_states.
    """
    _session_start: Optional[float] = None
    """Wall clock at launch, or None when nothing has been launched in this process.

    None is not a baseline of zero: a zero baseline matches every file on
    disk, which would put every unrelated title's saves in the dump.
    """
    log_path = RPCS3_LOG_PATH
    # No SIGTERM handler: the default action ends the process at once, saves
    # are already on disk. The grace window only covers process-group
    # teardown of the AppImage wrapper.
    term_timeout = float(os.environ.get("RPCS3_STOP_WAIT", "2"))

    def __init__(self) -> None:
        """Initialize per-session launch tracking state."""
        super().__init__()
        self._launch_seq = 0

    @property
    def rom_extensions(self) -> tuple[str, ...]:
        """Bootable formats, minus the archives needing the disabled extraction cache.

        An archive only reaches a boot target by way of CACHE_DIR, so with
        the cache off it is not bootable and must not be advertised as
        accepted. `.pkg` is unaffected: it installs through `_install_pkgs`
        into GAME_DIR regardless of the cache flag.
        """
        if CACHE_ENABLED:
            return ROM_EXTENSIONS
        return tuple(e for e in ROM_EXTENSIONS if e not in _ARCHIVE_EXTS)

    def clear_working_slot(self) -> None:
        """Create the savestates symlink and drop this title's stale savestates.

        RPCS3 has no fixed slot to clear, but activate() calls this before
        it ever reads save_subtrees to restore an archive, which makes it the
        one place that is safe to create the savestates symlink: it runs on
        every activate, but never during a bare construction (registry
        sweeps build every emulator against real, unredirected paths).

        It also drops the incoming title's own savestates dir and flips
        _restoring on early. Clearing first means a leftover local state from
        a previous session never outranks (by mtime) whatever this session's
        archive restores. Only that one title goes: every other title's dir
        holds states that may not have been archived yet (a crashed session,
        a title mid-restore for a different game), and none of them can
        outrank this session's states anyway because every lookup is already
        scoped to SSTATE_ROOT/<title id>. A boot target whose title id the
        path alone cannot supply (a bare .iso, an archive) therefore clears
        nothing rather than guessing. Flipping _restoring here, not just in
        prepare_restore(), matters because api.py reads save_subtrees for the
        restore extract before it ever calls prepare_restore().
        """
        _ensure_sstate_link()
        self._restoring = True
        self._leftover_snapshot = None
        rom = self._pending_rom
        title_id = _rom_title_id(rom) if rom is not None else None
        if title_id is None:
            self._leftover_snapshot = _all_state_files()
            log.info(
                "rpcs3: no title id for %s, leaving savestates untouched",
                rom.name if rom is not None else "an unresolved rom",
            )
            return
        state_dir = SSTATE_ROOT / title_id
        if not state_dir.is_dir():
            return
        try:
            shutil.rmtree(state_dir)
        except OSError as exc:
            log.warning(
                "rpcs3: could not clear stale savestates for %s in %s: %s",
                title_id, state_dir, exc,
            )
        else:
            log.debug("rpcs3: cleared stale savestates for %s", title_id)

    @property
    def save_subtrees(self) -> tuple[str, ...]:
        """CellSaveData saves, save states, plus the cellGameData save dirs under game/.

        game/ mixes save data with installed PKG titles and RPCS3's ＄locks
        dir, so the dump enumerates only the dirs without a bootable
        EBOOT.BIN. A restore inverts the problem: the archive holds nothing
        but previously dumped save dirs, and those dirs don't exist on disk
        yet, so the whole game/ prefix is declared to let them through.
        """
        if self._restoring:
            return ("home/00000001/savedata", "game", "savestates")
        subtrees = ["home/00000001/savedata", "savestates"]
        subtrees += [f"game/{d.name}" for d in _gamedata_dirs()]
        return tuple(subtrees)

    def prepare_restore(self) -> None:
        """Stop any running instance and mark the session as archive-restoring."""
        self.stop()
        self._restoring = True

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a RomM path to a single bootable PKG, disc, or ISO.

        A file is taken as is. A directory is searched for the game's
        installer/disc layout via `_ROM_SEARCH_GLOBS`. The chosen target is
        also recorded in `_pending_rom` for `clear_working_slot`, which runs
        next and has no other way to name the title it is clearing for.

        Args:
            path: RomM's resolved rom path, file or directory.

        Returns:
            The path to boot, or None if nothing bootable was found, or path
            is a `.7z`/`.zip`/`.rar` archive with the extraction cache
            disabled (`.pkg` is unaffected: it installs via `_install_pkgs`
            regardless of the cache flag).
        """
        self._pending_rom = None
        if path.is_file():
            if path.suffix.lower() in _ARCHIVE_EXTS and not CACHE_ENABLED:
                log.warning(
                    "rpcs3: refusing %s, %s needs the extraction cache "
                    "(set RPCS3_CACHE_ENABLED=true to boot this format)",
                    path.name,
                    path.suffix.lower(),
                )
                return None
            self._pending_rom = path
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError:
                return None
        picked = _pick_rom_file(candidates, path)
        self._pending_rom = picked
        return picked

    def _xdotool(self, *args: str) -> Optional[str]:
        """Run xdotool, returning its stdout, or None if it failed."""
        try:
            result = subprocess.run(
                [_XDOTOOL, *args],
                env=base_launch_env(),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("xdotool %s failed: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            log.warning("xdotool %s: %s", " ".join(args), result.stderr.strip())
            return None
        return result.stdout

    def _game_window(self) -> Optional[str]:
        out = self._xdotool("search", "--class", _WINDOW_CLASS)
        if not out:
            log.warning("no rpcs3 window found")
            return None
        return out.split()[0]

    def _send_key(self, key: str) -> bool:
        """Focus the render window and send `key` through XTEST.

        Activating first is what makes this survive the player clicking back
        into the page: XTEST delivers to whatever holds focus, so a key sent
        at an unfocused RPCS3 goes to the desktop instead.
        """
        win_id = self._game_window()
        if win_id is None:
            return False
        if self._xdotool("windowactivate", "--sync", win_id) is None:
            return False
        return self._xdotool("key", "--clearmodifiers", key) is not None

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Stop any running instance and boot rpcs3 for the given ROM.

        Installs PKGs and extracts archives as needed, then resolves a
        savestate to boot into when a resume slot is requested and one
        exists for the title.

        Args:
            rom_path: RomM's resolved rom path (file or directory).
            resume_slot: Slot to resume from, or None for a fresh boot.
        """
        self.stop()
        self._restoring = False
        self.boot_failed = False
        self._launch_seq += 1
        seq = self._launch_seq

        _patch_config()
        _patch_ipc()
        ext = rom_path.suffix.lower()
        if ext == ".pkg":
            boot = _install_pkgs(rom_path)
        elif ext in _ARCHIVE_EXTS:
            boot = _extract_and_cache(rom_path, self)
        else:
            boot = rom_path

        # Save dirs (and now states) are named with the title serial as
        # prefix; remember it so the exit dump can be scoped to this title.
        # Installed titles boot from game/<serial>/USRDIR/EBOOT.BIN, disc
        # rips carry a PARAM.SFO next to USRDIR. An .iso has no readable
        # serial from the path alone; the boot watchdog fills it in from
        # PINE once the game is confirmed running.
        if boot.name.upper().startswith("EBOOT"):
            if boot.is_relative_to(GAME_DIR):
                self._session_serial = boot.parent.parent.name
            else:
                self._session_serial = _sfo_title_id(boot.parent.parent / "PARAM.SFO")
        else:
            self._session_serial = None

        if self._session_serial and self._leftover_snapshot is not None:
            _clear_leftover_states(self._session_serial, self._leftover_snapshot)
            self._leftover_snapshot = None

        # RPCS3 has no boot-time state-load flag of its own, but it detects a
        # savestate by magic bytes regardless of what is passed as the boot
        # target, so pointing it at the state file IS the boot-time load.
        target = boot
        if resume_slot is not None:
            if self._session_serial is None:
                log.warning(
                    "resume requested but %s has no known title id, booting fresh",
                    rom_path.name,
                )
            else:
                state = _newest_state(self._session_serial)
                if state is None:
                    log.warning(
                        "resume requested but no savestate in %s",
                        SSTATE_ROOT / self._session_serial,
                    )
                else:
                    target = state

        self._session_start = time.time()
        log.info(
            "launching rpcs3 (rom=%s, boot=%s, serial=%s)",
            rom_path, target, self._session_serial,
        )
        self._spawn([_rpcs3_bin(), "--no-gui", "--fullscreen", str(target)], _launch_env())
        Thread(target=self._boot_watchdog, args=(seq,), daemon=True).start()

    def _boot_watchdog(self, seq: int) -> None:
        """Confirm the launched game reaches a running state via PINE.

        Also opportunistically resolves the title id via MsgID for boots
        that had no serial from the file path alone (a bare .iso).

        A process still alive when the deadline passes without ever
        reporting running is the boot-error-dialog case: RPCS3 does not exit
        on its own, so nothing else in the broker would ever notice.
        """
        deadline = time.monotonic() + BOOT_WAIT
        while time.monotonic() < deadline:
            if self._launch_seq != seq:
                log.info("boot watchdog: launch superseded, abandoning")
                return
            if _pine_status() == 0:
                if not self._session_serial:
                    title = _pine_title_id()
                    if self._launch_seq != seq:
                        log.info(
                            "boot watchdog: launch superseded during title lookup, "
                            "discarding %s",
                            title,
                        )
                        return
                    if title:
                        self._session_serial = title
                        log.info("boot watchdog: resolved title id %s via PINE", title)
                        if self._leftover_snapshot is not None:
                            _clear_leftover_states(title, self._leftover_snapshot)
                            self._leftover_snapshot = None
                return
            time.sleep(1.0)

        if self._launch_seq != seq:
            return
        if self.alive():
            self.boot_failed = True
            log.warning(
                "boot watchdog: rpcs3 never reported a running state and is "
                "still alive, treating as a boot failure"
            )
        else:
            log.warning("boot watchdog: rpcs3 exited before ever reporting running")

    def save_and_exit(self, slot: Optional[int]) -> dict:
        """Send the save-state hotkey, then stop the emulator.

        Args:
            slot: Slot to save to, or None to exit without saving.

        Returns:
            A dict with `state_saved`, `state_slot`, and `state_file` (path,
            size, and mtime of the new state file, or None if none was saved).
        """
        saved = False
        state_file = None
        if slot is not None and self.alive():
            if not self._session_serial:
                log.warning("save-and-exit: no known title id, cannot locate a state to save")
            else:
                before = _state_snapshot(self._session_serial)
                if not self._send_key(_SAVE_STATE_KEY):
                    log.warning("save-and-exit: could not send the save-state hotkey")
                else:
                    p = _wait_for_state_write(
                        self._session_serial,
                        before,
                        time.monotonic() + STATE_WAIT,
                        self._proc.pid if self._proc is not None else None,
                    )
                    if p is None:
                        log.warning("save-and-exit: no new state file appeared")
                    else:
                        try:
                            st = p.stat()
                        except OSError as exc:
                            log.warning("save-and-exit: could not stat saved state %s: %s", p, exc)
                        else:
                            saved = True
                            state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}

        # The dump ships files newer than the session baseline. A save is a
        # directory tree the game rewrites only partially, and sibling dirs
        # belong to other titles, so refresh every mtime in this title's
        # save dirs: they ship whole, the rest stay filtered out.
        now = time.time()
        for d in self._session_save_dirs():
            for p in d.rglob("*"):
                if p.is_file():
                    try:
                        os.utime(p, (now, now))
                    except OSError as exc:
                        log.warning("could not restamp %s, may be dropped from the dump: %s", p, exc)

        self.stop()
        return {"state_saved": saved, "state_slot": slot, "state_file": state_file}

    def stop(self) -> None:
        """Kill the process, invalidating any in-flight boot watchdog first."""
        self._launch_seq += 1
        super().stop()

    def _session_save_dirs(self) -> list[Path]:
        """This title's save dirs.

        A dir qualifies by name (prefixed with the session serial) or by
        containing a file written while the session ran.

        Returns:
            The save dirs belonging to this session. Empty when nothing was
            launched in this process, since with no launch baseline there is
            no way to tell this title's saves from every other title's.
        """
        started = self._session_start
        if started is None:
            log.warning(
                "rpcs3: no launch in this process, refusing to guess which saves are the session's"
            )
            return []
        savedata = USER_HOME / "savedata"
        candidates = sorted(d for d in savedata.iterdir() if d.is_dir()) if savedata.is_dir() else []
        candidates += _gamedata_dirs()
        selected = []
        for d in candidates:
            if self._session_serial and d.name.startswith(self._session_serial):
                selected.append(d)
                continue
            try:
                if any(
                    p.is_file() and p.stat().st_mtime >= started
                    for p in d.rglob("*")
                ):
                    selected.append(d)
            except OSError as exc:
                log.warning("rpcs3: could not scan %s for session saves: %s", d, exc)
                continue
        return selected
