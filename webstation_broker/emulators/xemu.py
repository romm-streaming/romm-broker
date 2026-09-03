"""xemu launcher (original Xbox): FATX-level save sync on a raw HDD image.

xemu keeps game saves inside the one hard-disk image its xemu.toml points at,
and QEMU exposes that image only as an opaque block device, so the previous
design shipped the entire qcow2 as the save artifact. This version syncs at
the filesystem level instead: the image is kept in raw format so pyfatx
(userspace libfatx bindings: no FUSE, no NBD, container-safe) can read and
write the FATX E partition directly, and the save archive carries only the
launched title's save data.

How a session moves saves in and out of the image:

- image: the first launch after deploy finds the configured qcow2 and
  converts it in place with qemu-img (same filename, raw content, sparse on
  disk), keeping the original alongside as `<name>.backup`. A raw image
  cannot hold QEMU internal snapshots, so the save-state interface is gone;
  save data is the whole artifact now.
- launch: when the activate restored an archive, its files are written into
  E:/UDATA and E:/TDATA before xemu boots (pre-launch hook).
- exit: once xemu has stopped and flushed, the launched title's UDATA and
  TDATA trees are extracted from the image into the staging dir (post-close
  hook), where the standard dump zips them.
- title: the title id (the UDATA directory name) is read from the disc's
  default.xbe certificate; if the disc cannot be parsed, extraction falls
  back to every title on the disk rather than losing the session's saves.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any, Optional

from pyfatx import Fatx

from .. import settings
from .base import Emulator, _cmdline, base_launch_env

log = logging.getLogger(__name__)

XEMU_BIN = os.environ.get("XEMU_BIN", "/opt/xemu/AppRun")
"""The xemu executable to spawn (env `XEMU_BIN`, default `/opt/xemu/AppRun`)."""
XEMU_LOG_PATH = Path(os.environ.get("XEMU_LOG_PATH", "/config/xemu.log"))
"""The emulator log file (env `XEMU_LOG_PATH`, default `/config/xemu.log`)."""

XEMU_STRAY_TERM_WAIT = float(os.environ.get("XEMU_STRAY_TERM_WAIT", "5"))
"""Grace a stray xemu gets to exit before SIGKILL (env `XEMU_STRAY_TERM_WAIT`, default 5).

QEMU flushes the HDD image on a clean shutdown and not on SIGKILL, and the save
hooks read that image seconds later, so a stray is asked to leave first.
"""

XEMU_RENDERER = os.environ.get("XEMU_RENDERER", "OPENGL").strip().upper()
"""Renderer pinned into xemu.toml before each launch (env `XEMU_RENDERER`, default `OPENGL`).

Vulkan aborts xemu on the AMD Renoir/RADV stack these containers run on, and
the choice persists in xemu.toml, so one session spent switching renderers
leaves every later launch broken. Pinned before each launch; set `XEMU_RENDERER`
to `VULKAN` where the driver is known good, or to `KEEP` to leave the file alone.
"""


def _truthy(value: str) -> bool:
    """Whether an environment flag reads as enabled.

    Args:
        value: The raw environment value.

    Returns:
        True for `1`, `true`, `yes` or `on`, case-insensitive and whitespace-trimmed.
    """
    return value.strip().lower() in ("1", "true", "yes", "on")


XEMU_SOFTWARE_GL = _truthy(os.environ.get("XEMU_SOFTWARE_GL", ""))
"""Whether xemu renders on the CPU via `LIBGL_ALWAYS_SOFTWARE` (env `XEMU_SOFTWARE_GL`, default off).

Which renderer xemu asks for and whether the driver can answer are separate
problems: on the AMD Renoir stack these containers run on, xemu aborts in
gl_fence on the OpenGL path and in RADV on the Vulkan one. Set
`XEMU_SOFTWARE_GL` to render xemu on the CPU there, which the container-wide
`LIBGL_ALWAYS_SOFTWARE` cannot do without dragging every other emulator down
with it. Slow, so it stays off unless the host needs it.
"""


def _default_toml_path() -> Path:
    """Where xemu.toml lives when `XEMU_TOML` is not set.

    xemu.toml lives in SDL's pref dir: `$XDG_DATA_HOME/xemu/xemu`, or
    `~/.local/share/xemu/xemu` when `XDG_DATA_HOME` is unset.

    Returns:
        The default xemu.toml path.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        base = Path(os.environ.get("HOME", "/config")) / ".local/share"
    return base / "xemu" / "xemu" / "xemu.toml"


XEMU_TOML = Path(os.environ.get("XEMU_TOML", str(_default_toml_path())))
"""The xemu.toml the HDD path is read from and display settings are pinned into (env `XEMU_TOML`).

Defaults to the SDL pref dir location `_default_toml_path` computes.
"""
FALLBACK_HDD_IMAGE = Path(
    os.environ.get("XEMU_HDD_IMAGE", "/config/xemu/xbox_hdd.qcow2")
)
"""HDD image assumed when xemu.toml cannot say (env `XEMU_HDD_IMAGE`).

Defaults to `/config/xemu/xbox_hdd.qcow2`, and is used only when xemu.toml
cannot tell us: a fresh container where xemu has never run, or a config with
no usable hdd_path.
"""

SAVE_STAGING_DIRNAME = "saves"
"""Name of the staging directory next to the HDD image.

The generic dump/restore reads and writes host files here, and the
launch/exit hooks move them in and out of the FATX filesystem.
"""

QCOW2_MAGIC = b"QFI\xfb"
"""The four-byte header that marks a qcow2 image; its absence means raw content."""

ROM_EXTENSIONS = (".iso",)
"""Bootable disc formats: only XISO, always named `.iso`, including the `.xiso.iso` double extension."""
_ROM_SEARCH_GLOBS = ("*", "*/*")
"""Glob patterns a ROM folder is searched with, one level of wrapper folder deep."""
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)
"""Matches a disc number in a file name, for ranking multi-disc dumps."""


def _hdd_image_path() -> Path:
    """The disk image xemu will actually mount, read from the user's config.

    The user configures xemu themselves, so their xemu.toml `[sys.files]`
    hdd_path is the authority; a broker-side path would drift from it the
    moment they repoint one of them.

    Returns:
        The configured image path, or `FALLBACK_HDD_IMAGE` when xemu.toml is
        missing, unparsable, or carries no usable absolute hdd_path.
    """
    try:
        with XEMU_TOML.open("rb") as fh:
            cfg = tomllib.load(fh)
        raw = str(cfg.get("sys", {}).get("files", {}).get("hdd_path", "") or "")
    except OSError as exc:
        log.warning("could not read %s (%s); assuming hdd at %s",
                    XEMU_TOML, exc, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    except tomllib.TOMLDecodeError as exc:
        log.error("could not parse %s (%s); assuming hdd at %s",
                  XEMU_TOML, exc, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    if not raw:
        log.warning("%s has no sys.files.hdd_path; assuming hdd at %s",
                    XEMU_TOML, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    p = Path(raw).expanduser()
    # A relative hdd_path or one sitting in / gives the save staging dir no
    # sane directory to live in.
    if not p.is_absolute() or not p.parent.name:
        log.error("xemu.toml hdd_path %r is unusable; assuming hdd at %s",
                  raw, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    return p


def _launch_env() -> dict[str, str]:
    """The environment xemu is spawned with.

    Returns:
        The base launch environment, plus `LIBGL_ALWAYS_SOFTWARE=1` when
        `XEMU_SOFTWARE_GL` is set.
    """
    env = base_launch_env()
    if XEMU_SOFTWARE_GL:
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    return env


_NEXT_SECTION_RE = re.compile(r"^\[", re.MULTILINE)
"""Matches the start of the next TOML table header, bounding a section body."""


def _pin_toml_key(text: str, section: str, key: str, value: str) -> str:
    """Set `key = value` under `[section]` in TOML text, adding either if missing.

    Edited as text rather than reparsed and dumped: tomllib only reads, and a
    round trip through a writer would flatten the comments and key order xemu
    maintains in the file it owns.

    Args:
        text: The full xemu.toml content.
        section: The table name without brackets, for example `display.window`.
        key: The key to set within that table.
        value: The value as TOML source text, quotes included where needed.

    Returns:
        The updated TOML text.
    """
    line = f"{key} = {value}"
    header = re.search(rf"^\[{re.escape(section)}\][ \t]*$", text, re.MULTILINE)
    if header is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{line}\n"

    body_start = header.end()
    next_section = _NEXT_SECTION_RE.search(text, body_start)
    body_end = next_section.start() if next_section else len(text)
    body = text[body_start:body_end]
    key_re = re.compile(rf"^{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
    if key_re.search(body):
        # A replacement function, so a backslash in the value stays literal.
        body = key_re.sub(lambda _: line, body, count=1)
    else:
        body = f"\n{line}" + body
    return text[:body_start] + body + text[body_end:]


def _write_toml(text: str) -> bool:
    """Replace xemu.toml with `text`, atomically and only when it still parses.

    xemu owns this file and every setting the user has ever changed lives in
    it, so a truncated write or a bad edit costs them all of it. The candidate
    is parsed before anything is written, then swapped in from a sibling temp
    file so the config is never the half-written one.

    Args:
        text: The full replacement config.

    Returns:
        True when xemu.toml now holds `text`, False (logged) when it does not,
        in which case the original file is untouched.
    """
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        log.error("xemu: refusing to write %s, the pinned content does not parse (%s)",
                  XEMU_TOML, exc)
        return False
    try:
        mode = XEMU_TOML.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(XEMU_TOML.parent),
                                   prefix=f".{XEMU_TOML.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, XEMU_TOML)
    except OSError as exc:
        log.error("could not pin display settings in %s: %s", XEMU_TOML, exc)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError as cleanup_exc:
                log.debug("could not remove %s: %s", tmp, cleanup_exc)
        return False
    return True


def _pin_display_settings() -> None:
    """Force the display settings a streamed session needs into xemu.toml.

    xemu owns this file and rewrites it on exit, dropping every key that still
    matches its own default, so neither setting survives a session on its own.
    """
    try:
        text = XEMU_TOML.read_text(encoding="utf-8")
    except OSError as exc:
        # Nothing to pin on a container where xemu has never run; it writes the
        # file on first exit, and starts out on OpenGL anyway.
        log.debug("could not read %s to pin display settings (%s)", XEMU_TOML, exc)
        return

    updated = text
    if XEMU_RENDERER not in ("", "KEEP"):
        updated = _pin_toml_key(updated, "display", "renderer", f"'{XEMU_RENDERER}'")
    # Every other emulator here is launched fullscreen with a command line flag.
    # xemu has none, so its window size is a config key like anything else.
    updated = _pin_toml_key(updated, "display.window", "fullscreen_on_startup", "true")

    if updated == text:
        return
    if not _write_toml(updated):
        return
    log.info("pinned xemu display settings in %s", XEMU_TOML)


def _disc_number(rel: Path) -> int:
    """Disc number parsed from a candidate's relative path, for multi-disc ranking.

    Args:
        rel: The candidate's path relative to the search base.

    Returns:
        The disc number found in the name, or 1 when there is none.
    """
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Optional[Path]:
    """Pick the best bootable disc image among `candidates`.

    Hidden files, non-files and anything resolving outside the ROM library
    root are skipped. Ranking prefers disc 1, then the `ROM_EXTENSIONS` order,
    then the shallowest path, then the lowercased name.

    Args:
        candidates: Paths found under `base` by the search globs.
        base: The directory the candidates were searched from.

    Returns:
        The resolved path of the best candidate, or None when nothing qualifies.
    """
    ranked = []
    rom_root = settings.rom_root()
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
        if not real.is_relative_to(rom_root):
            log.warning("xemu: skipping %s, it resolves outside %s", p, rom_root)
            continue
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


# ── One-time image conversion ────────────────────────────────────────────────


def _ensure_raw_image(image: Path) -> bool:
    """Make sure `image` holds raw disk content, converting a qcow2 once.

    Detection is the qcow2 magic, so this runs as a cheap no-op on every
    launch and converts exactly once. The raw content replaces the file under
    its original name: xemu format-probes the content, so the .qcow2 name
    keeps working and the user's xemu.toml never needs to change. The
    original qcow2 (including any internal snapshots) is kept alongside as a
    backup. On any failure the qcow2 is left in place so the session can
    still play; only save sync is lost.

    Args:
        image: The HDD image xemu mounts.

    Returns:
        True when FATX access is possible afterwards, False when the image
        could not be read or the conversion failed.
    """
    try:
        with image.open("rb") as fh:
            magic = fh.read(len(QCOW2_MAGIC))
    except OSError as exc:
        log.error("could not read HDD image %s: %s", image, exc)
        return False
    if magic != QCOW2_MAGIC:
        return True

    backup = image.with_name(image.name + ".backup")
    tmp = image.with_name(f".{image.name}.raw-tmp")
    log.info("one-time HDD conversion: %s qcow2 -> raw", image)
    try:
        subprocess.run(
            ["qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(image), str(tmp)],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        log.error("qemu-img not found; cannot convert %s", image)
        return False
    except subprocess.CalledProcessError as exc:
        log.error("qemu-img convert failed for %s: %s", image, exc.stderr.strip())
        tmp.unlink(missing_ok=True)
        return False
    try:
        if backup.exists():
            log.warning("overwriting previous HDD backup %s", backup)
        os.replace(image, backup)
        os.replace(tmp, image)
    except OSError as exc:
        log.error("could not swap converted HDD image into place: %s", exc)
        if not image.exists() and backup.exists():
            os.replace(backup, image)
        tmp.unlink(missing_ok=True)
        return False
    log.info("HDD image now raw; original qcow2 kept at %s", backup)
    return True


# ── Title id from the disc image ─────────────────────────────────────────────

_XISO_SECTOR = 2048
"""Sector size of an XISO, in bytes."""
_XISO_MAGIC = b"MICROSOFT*XBOX*MEDIA"
"""The volume descriptor signature at sector 32 of the game partition."""
XISO_MAX_DIR_BYTES = 8 * 1024 * 1024
"""Ceiling on one XISO directory table read, in bytes.

The root table's size is a 32-bit field taken straight from an untrusted disc
image, so a corrupt or hostile ISO can ask for a 4 GiB allocation. A real Xbox
root table is a few kilobytes; megabytes of it is already past anything a disc
carries.
"""
_XISO_BASES = (0, 0x18300000)
"""Game-partition offsets to probe.

0 for an extracted XISO, 0x18300000 for a full dump that still carries the
video partition up front.
"""


def _xiso_root_dir(fh: IO[bytes]) -> Optional[tuple[int, int, int]]:
    """Locate the root directory of the disc's game partition.

    Args:
        fh: The disc image, open for binary reading.

    Returns:
        A `(partition base, root dir file offset, root dir size)` tuple, or
        None when no XISO volume descriptor sits at any probed base.
    """
    for base in _XISO_BASES:
        fh.seek(base + 32 * _XISO_SECTOR)
        vd = fh.read(_XISO_SECTOR)
        if len(vd) == _XISO_SECTOR and vd[: len(_XISO_MAGIC)] == _XISO_MAGIC:
            sector = int.from_bytes(vd[20:24], "little")
            size = int.from_bytes(vd[24:28], "little")
            return base, base + sector * _XISO_SECTOR, size
    return None


def _xiso_find_entry(table: bytes, name: bytes) -> Optional[tuple[int, int]]:
    """Find `name` in one XISO directory table.

    Entries form a binary tree; left/right links are dword offsets into the
    table. The visited set and bounds checks make a corrupt or 0xFF-padded
    table terminate instead of looping.

    Args:
        table: The raw directory table bytes.
        name: The lowercased entry name to find.

    Returns:
        A `(start sector, file size)` tuple, or None when the entry is absent.
    """
    stack = [0]
    seen: set[int] = set()
    while stack:
        off = stack.pop() * 4
        if off in seen or off + 14 > len(table):
            continue
        seen.add(off)
        left = int.from_bytes(table[off:off + 2], "little")
        right = int.from_bytes(table[off + 2:off + 4], "little")
        name_len = table[off + 13]
        entry_name = table[off + 14:off + 14 + name_len]
        if 0 < name_len <= 42 and len(entry_name) == name_len:
            if entry_name.lower() == name:
                start = int.from_bytes(table[off + 4:off + 8], "little")
                size = int.from_bytes(table[off + 8:off + 12], "little")
                return start, size
            for branch in (left, right):
                if branch not in (0, 0xFFFF):
                    stack.append(branch)
    return None


def _disc_title_id(rom_path: Path) -> Optional[str]:
    """Title id from the disc's default.xbe certificate.

    Formatted the way the Xbox names save dirs (`E:/UDATA/<id>`): eight
    uppercase hex digits.

    Args:
        rom_path: The XISO to read.

    Returns:
        The title id, or None when the disc, its default.xbe or the XBE
        certificate cannot be read.
    """
    try:
        with rom_path.open("rb") as fh:
            root = _xiso_root_dir(fh)
            if root is None:
                log.warning("%s: no XISO volume descriptor found", rom_path)
                return None
            base, dir_off, dir_size = root
            if not 0 < dir_size <= XISO_MAX_DIR_BYTES:
                log.warning("%s: root directory table claims %d bytes; not reading it",
                            rom_path, dir_size)
                return None
            fh.seek(dir_off)
            entry = _xiso_find_entry(fh.read(dir_size), b"default.xbe")
            if entry is None:
                log.warning("%s: no default.xbe in the disc root", rom_path)
                return None
            xbe_off, xbe_size = entry
            xbe_off = base + xbe_off * _XISO_SECTOR
            fh.seek(xbe_off)
            header = fh.read(0x11C)
            if len(header) < 0x11C or header[:4] != b"XBEH":
                log.warning("%s: default.xbe has no XBE header", rom_path)
                return None
            base_addr = int.from_bytes(header[0x104:0x108], "little")
            cert_addr = int.from_bytes(header[0x118:0x11C], "little")
            cert_off = cert_addr - base_addr
            if cert_off < 0 or cert_off + 12 > xbe_size:
                log.warning("%s: XBE certificate out of bounds", rom_path)
                return None
            fh.seek(xbe_off + cert_off)
            cert = fh.read(12)
            if len(cert) < 12:
                return None
            # Uppercase because that is how the dashboard names the directory
            # it creates, and a directory this code creates has to match.
            return f"{int.from_bytes(cert[8:12], 'little'):08X}"
    except OSError as exc:
        log.warning("could not read %s for a title id: %s", rom_path, exc)
        return None


# ── FATX access (pyfatx surfaces libfatx errors as bare AssertionError) ──────


def _open_fatx_e(image: Path) -> Optional[Fatx]:
    """Open the FATX E partition of a raw HDD image.

    pyfatx surfaces libfatx errors as bare AssertionError, hence the catch.

    Args:
        image: The raw HDD image.

    Returns:
        The open filesystem handle, or None (logged) when it cannot be opened.
    """
    try:
        return Fatx(str(image), drive="e")
    except (AssertionError, OSError):
        log.error("could not open the FATX E partition on %s", image)
        return None


def _fatx_isdir(fs: Fatx, path: str) -> bool:
    """Whether `path` exists on the FATX filesystem and is a directory.

    Args:
        fs: The open FATX filesystem.
        path: The absolute path on the partition.

    Returns:
        True for an existing directory, False for anything else or a lookup error.
    """
    try:
        return bool(fs.get_attr(path).is_directory)
    except AssertionError:
        return False


def _fatx_find_dir(fs: Fatx, parent: str, name: str) -> Optional[str]:
    """Path of `parent`'s child directory matching `name` whatever its case.

    libfatx compares names byte for byte, so a lookup has to use the case the
    disk actually holds. Titles vary in how they case their save directories,
    and the miss would be silent: nothing resolves, nothing is extracted, and
    the session's saves stay behind in the image.

    Args:
        fs: The open FATX filesystem.
        parent: The directory to search, as an absolute path on the partition.
        name: The child directory name, compared case-insensitively.

    Returns:
        The child's path in the case the disk holds, or None when there is no
        such directory or `parent` cannot be listed.
    """
    wanted = name.lower()
    try:
        entries = list(fs.listdir(parent))
    except (AssertionError, OSError):
        return None
    for attr in entries:
        if attr.is_directory and attr.filename.lower() == wanted:
            return f"{parent.rstrip('/')}/{attr.filename}"
    return None


def _fatx_discard(fs: Fatx, path: str) -> None:
    """Remove a file a failed write left in an unknown state.

    Args:
        fs: The open FATX filesystem.
        path: Absolute path of the file on the partition.
    """
    try:
        fs.get_attr(path)
    except (AssertionError, OSError):
        return
    try:
        fs.unlink(path)
    except (AssertionError, OSError) as exc:
        log.error("could not remove the partially written %s from the HDD image: %s",
                  path, exc)


def _fatx_write_file(fs: Fatx, path: str, data: bytes) -> None:
    """Write `data` to `path` on a FATX filesystem, all of it or none of it.

    pyfatx's write() never shortens an existing file, so a save that shrank
    keeps the previous one's tail unless the file is truncated. The truncate
    runs first: done afterwards, every failure between the two leaves the two
    saves spliced together and readable by the game as one. Anything that goes
    wrong takes the file with it for the same reason.

    Args:
        fs: The open FATX filesystem.
        path: Absolute path of the file on the partition.
        data: The full file contents.

    Raises:
        AssertionError: Raised by pyfatx for any libfatx level failure.
        OSError: If the image itself cannot be written.
        RuntimeError: If the file ends up on the image at the wrong size.
    """
    try:
        existing = fs.get_attr(path).file_size
    except (AssertionError, OSError):
        existing = 0
    try:
        if existing > len(data):
            fs.truncate(path, len(data))
        fs.write(path, data)
        landed = fs.get_attr(path).file_size
    except (AssertionError, OSError):
        _fatx_discard(fs, path)
        raise
    if landed != len(data):
        _fatx_discard(fs, path)
        raise RuntimeError(f"{path} landed as {landed} bytes, expected {len(data)}")


def _remove_tree(path: Path) -> None:
    """Delete a directory tree, logging rather than raising when it will not go.

    Args:
        path: The directory to remove.
    """
    if not path.is_dir():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        log.warning("could not fully remove %s: %s", path, exc)


# ── Provider ─────────────────────────────────────────────────────────────────


def _proc_pids() -> list[int]:
    """Every pid currently visible in `/proc`.

    Returns:
        The pids, or an empty list (logged) when `/proc` cannot be listed.
    """
    try:
        return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError as exc:
        log.warning("could not list /proc to find stray xemu processes: %s", exc)
        return []


def _is_xemu(pid: int) -> bool:
    """Whether a pid is running the configured xemu binary.

    Args:
        pid: The process to check.

    Returns:
        True when the process's argv carries `XEMU_BIN` as a whole element.
    """
    return XEMU_BIN in _cmdline(pid)


def _stray_xemu_pids() -> list[int]:
    """The pids running xemu right now.

    Matched on whole argv elements rather than a substring of the command
    line: a substring match also hits every process that merely names the
    binary, a shell, a log tail or a grep among them, and those would be
    killed too. Re-checking argv also means a pid recycled between the scan
    and the signal is left alone.

    Returns:
        The matching pids, this process excluded.
    """
    own = os.getpid()
    return [pid for pid in _proc_pids() if pid != own and _is_xemu(pid)]


def _reap_strays() -> None:
    """Stop any xemu the broker does not own, SIGTERM first.

    An orphan busy-loops CPU cores and, worse, keeps writing the HDD image the
    save hooks are about to read. QEMU flushes that image on SIGTERM and not on
    SIGKILL, so killing a stray outright tears its writes apart moments before
    the extraction reads them; SIGKILL is only the escalation once the stray
    has had `XEMU_STRAY_TERM_WAIT` seconds to go on its own.
    """
    pids = _stray_xemu_pids()
    if not pids:
        return
    log.warning("stray xemu process(es) %s hold the HDD image; asking them to exit",
                ", ".join(str(pid) for pid in pids))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            log.warning("could not SIGTERM stray xemu pid %d: %s", pid, exc)
    deadline = time.monotonic() + XEMU_STRAY_TERM_WAIT
    while True:
        pids = [pid for pid in pids if _is_xemu(pid)]
        if not pids:
            log.info("stray xemu process(es) exited on SIGTERM")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    for pid in pids:
        log.warning("stray xemu pid %d ignored SIGTERM for %.0fs; killing it",
                    pid, XEMU_STRAY_TERM_WAIT)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as exc:
            log.warning("could not SIGKILL stray xemu pid %d: %s", pid, exc)
    # The image is opened right after this returns, and a kill only queues the
    # exit.
    time.sleep(0.5)


class Xemu(Emulator):
    """Original Xbox via xemu, with saves synced at the FATX level.

    xemu is a QEMU derivative with no control channel the broker can reach,
    so the session is command line in (`-dvd_path`) and SIGTERM out. SIGTERM
    gives QEMU a clean shutdown that flushes the HDD image the post-close
    extraction then reads, so the grace window is long enough for the flush
    to land before the SIGKILL escalation tears it. Any xemu the broker does
    not own is stopped before every hook, SIGTERM first, because an orphan
    keeps writing the image the hooks are about to touch. Display settings
    (renderer and fullscreen) are pinned into xemu.toml before each launch,
    since xemu rewrites that file on exit and drops both.

    Save data lives inside the raw HDD image's FATX E partition. An archive
    the activate restored lands in a staging directory next to the image and
    is injected into E:/UDATA and E:/TDATA before boot; after exit the
    launched title's trees are extracted back into the staging directory for
    the standard dump. There are no save states: the image is kept raw so
    pyfatx can read it, and a raw image cannot hold QEMU internal snapshots,
    so `supports_states` stays off and a resume slot is logged and ignored.

    Attributes:
        name: Provider key, `xemu`.
        display_name: Human-readable name.
        rom_extensions: Bootable disc formats, `.iso` only.
        log_path: The emulator log file.
        term_timeout: SIGTERM grace before SIGKILL (env `XEMU_STOP_WAIT`, default 15).
        hdd_image: The HDD image xemu mounts, resolved once per session.
        staging_dir: Host-side directory the dump and restore read and write.
        save_root: The image's parent directory, which the save subtrees hang off.
        save_subtrees: The staging directory name, scoping dump and restore to it.
    """

    name = "xemu"
    display_name = "xemu"
    rom_extensions = ROM_EXTENSIONS
    log_path = XEMU_LOG_PATH
    term_timeout = float(os.environ.get("XEMU_STOP_WAIT", "15"))
    """SIGTERM grace before SIGKILL (env `XEMU_STOP_WAIT`, default 15).

    SIGTERM gives QEMU a clean shutdown that flushes the HDD image the
    post-close extraction is about to read; give the flush time to land
    before the SIGKILL escalation tears it.
    """

    def __init__(self) -> None:
        """Resolve the HDD image and staging directory for this session.

        An image parked under `.<name>.prev` by a previous broker version is
        moved back into place when the configured image is missing.
        """
        super().__init__()
        # Resolved once per session so the whole activate/exit round trip
        # sees one image, even if xemu rewrites its config mid-session.
        self.hdd_image = _hdd_image_path()
        self.staging_dir = self.hdd_image.parent / SAVE_STAGING_DIRNAME
        self.save_root = self.hdd_image.parent
        self.save_subtrees = (SAVE_STAGING_DIRNAME,)
        self._restore_pending = False
        self._title_id: Optional[str] = None
        parked = self.hdd_image.with_name(f".{self.hdd_image.name}.prev")
        if not self.hdd_image.exists() and parked.is_file():
            log.info("recovering HDD image parked by a previous version")
            try:
                os.replace(parked, self.hdd_image)
            except OSError as exc:
                log.error("could not recover parked image %s: %s", parked, exc)
        log.info("xemu hdd image: %s (save staging %s)", self.hdd_image, self.staging_dir)

    def _clear_staging(self) -> None:
        """Remove the staging directory and everything under it, logging a failure."""
        _remove_tree(self.staging_dir)

    def _inject_saves(self) -> int:
        """Pre-launch hook: write every staged file into the FATX E partition.

        Hidden entries and symlinks in the staging dir are skipped. Directory
        components are matched case-insensitively against the disk so an
        archive whose case differs lands in the existing directory.

        Returns:
            The number of files that landed on the image.
        """
        if not self.staging_dir.is_dir():
            return 0
        files = [
            p for p in sorted(self.staging_dir.rglob("*"))
            if p.is_file() and not p.is_symlink()
            and not any(part.startswith(".") for part in p.relative_to(self.staging_dir).parts)
        ]
        if not files:
            return 0
        fs = _open_fatx_e(self.hdd_image)
        if fs is None:
            return 0
        written = 0
        # Directory paths as they exist on the image, keyed by the staged
        # (case-bearing) components, so an archive whose case differs from the
        # disk's lands in the existing directory instead of a twin beside it.
        resolved: dict[tuple[str, ...], str] = {(): ""}
        try:
            for p in files:
                parts = p.relative_to(self.staging_dir).parts
                try:
                    parent = ""
                    for i in range(1, len(parts)):
                        key = parts[:i]
                        if key in resolved:
                            parent = resolved[key]
                            continue
                        found = _fatx_find_dir(fs, parent or "/", parts[i - 1])
                        if found is None:
                            found = f"{parent}/{parts[i - 1]}"
                            fs.mkdir(found)
                        resolved[key] = found
                        parent = found
                    _fatx_write_file(fs, f"{parent}/{parts[-1]}", p.read_bytes())
                    written += 1
                except (AssertionError, OSError, RuntimeError) as exc:
                    log.warning("could not inject %s into the HDD image %s: %s",
                                "/".join(parts), self.hdd_image, exc)
        finally:
            # pyfatx has no close()/flush(); dropping the handle is the only
            # way to trigger fatx_close_device and commit these writes, so an
            # exception on the way out must not skip it.
            del fs
        return written

    def _save_roots(self, fs: Fatx) -> Optional[list[str]]:
        """The partition directories this session's saves live under.

        Args:
            fs: The open FATX filesystem.

        Returns:
            The absolute partition paths to extract, empty when the title has
            simply never saved or its title id could not be determined, or
            None when neither top level save directory can be read, which is
            the image failing to answer rather than an empty save set.
        """
        tops = [top for top in ("UDATA", "TDATA") if _fatx_isdir(fs, f"/{top}")]
        if not tops:
            log.error("neither /UDATA nor /TDATA could be read on %s; treating this "
                      "as a failed read, not as a title with no saves", self.hdd_image)
            return None
        if not self._title_id:
            # A disc whose title id could not be parsed must not fall back to
            # scoping every title on the drive: that would archive and later
            # restore every installed title's save data into this session,
            # not just the one that is actually running.
            log.warning("no disc title id for %s; refusing to scope saves to every "
                        "title on the drive, nothing will be archived or restored "
                        "this session", self.hdd_image)
            return []
        roots = []
        for top in tops:
            src = _fatx_find_dir(fs, f"/{top}", self._title_id)
            if src is None:
                log.info("no /%s/%s on the HDD image", top, self._title_id)
            else:
                roots.append(src)
        if not roots:
            log.info("title %s has no save directory on %s; nothing to extract",
                     self._title_id, self.hdd_image)
        return roots

    def _extract_into(self, fs: Fatx, roots: Iterable[str], dest: Path) -> Optional[int]:
        """Copy every file under `roots` out of the image into `dest`.

        Args:
            fs: The open FATX filesystem.
            roots: Absolute partition paths to walk.
            dest: Host directory the files are written under.

        Returns:
            The number of files written, or None when a directory listing
            failed, which leaves the save set only partly known.
        """
        extracted = 0
        dest_real = dest.resolve()
        for src in roots:
            try:
                tree = list(fs.walk(src))
            except (AssertionError, OSError) as exc:
                log.error("could not list %s on %s: %s", src, self.hdd_image, exc)
                return None
            for root, _dirs, filenames in tree:
                for name in filenames:
                    fatx_path = root.rstrip("/") + "/" + name
                    try:
                        data = bytes(fs.read(fatx_path))
                    except (AssertionError, OSError) as exc:
                        log.warning("could not read %s from the HDD image: %s",
                                    fatx_path, exc)
                        continue
                    target = dest / fatx_path.lstrip("/")
                    # Defense in depth: fs.walk()/read() surface whatever the
                    # emulated guest wrote to its save partition, so a `..`
                    # component (however unlikely from libfatx) is rejected
                    # here rather than trusted to stay under dest.
                    if not target.resolve().is_relative_to(dest_real):
                        log.warning("save path escapes staging dir: %s", fatx_path)
                        continue
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                    except OSError as exc:
                        log.warning("could not stage %s: %s", target, exc)
                        continue
                    extracted += 1
        return extracted

    def _swap_staging(self, scratch: Path) -> bool:
        """Put a finished extraction in place as the staging directory.

        Args:
            scratch: The finished extraction, a sibling of the staging dir.

        Returns:
            True when the staging directory now holds the extraction, False
            (logged) when it does not.
        """
        self._clear_staging()
        if self.staging_dir.exists():
            log.error("could not clear %s, so the extracted saves cannot be moved in",
                      self.staging_dir)
            return False
        try:
            os.replace(scratch, self.staging_dir)
        except OSError as exc:
            log.error("could not move the extracted saves into %s: %s", self.staging_dir, exc)
            return False
        return True

    def _extract_saves(self) -> Optional[int]:
        """Post-close hook: copy the title's save data out of the FATX E partition.

        The extraction runs into a scratch directory and is swapped over the
        staging one only once it has finished. The dump ships whatever staging
        holds, so clearing it up front would turn a failed read into an empty
        archive uploaded over the title's real saves; leaving the session's
        restored files in place instead means the dump finds nothing newer than
        the launch baseline and uploads nothing at all.

        Files land carrying fresh mtimes, so that baseline filter ships all of
        them: the archive is the title's complete save set, not a delta.
        Without a title id nothing is extracted; see `_save_roots`.

        Returns:
            The number of files staged, or None when the image could not be
            read, in which case the staging directory is left as it was.
        """
        fs = _open_fatx_e(self.hdd_image)
        if fs is None:
            return None
        scratch = self.staging_dir.with_name(f".{self.staging_dir.name}.new")
        try:
            roots = self._save_roots(fs)
            if roots is None:
                return None
            _remove_tree(scratch)
            try:
                scratch.mkdir(parents=True)
            except OSError as exc:
                log.error("could not prepare %s for the extraction: %s", scratch, exc)
                return None
            extracted = self._extract_into(fs, roots, scratch)
        finally:
            # pyfatx has no close()/flush(); dropping the handle is the only
            # way to trigger fatx_close_device and release the image, so an
            # exception on the way out must not skip it.
            del fs
        if extracted is None or not self._swap_staging(scratch):
            _remove_tree(scratch)
            return None
        return extracted

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The disc image to boot for `path`.

        Args:
            path: A ROM file, or a folder searched up to two levels deep.

        Returns:
            The file itself, the best-ranked `.iso` in the folder, or None.
        """
        rom_root = settings.rom_root()
        if path.is_file():
            # Defense in depth: api.py already validates path is under the ROM
            # root before calling in, but this checks it independently rather
            # than trusting every future caller to do the same.
            try:
                if not path.resolve().is_relative_to(rom_root):
                    log.warning("xemu: refusing %s, it resolves outside %s", path, rom_root)
                    return None
            except OSError as exc:
                log.warning("xemu: could not resolve %s (%s)", path, exc)
                return None
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError as exc:
                log.warning("xemu: could not search %s for a disc image (%s)", path, exc)
                return None
        return _pick_rom_file(candidates, path)

    def prepare_restore(self) -> None:
        """Get the image and staging dir ready before the archive is extracted.

        Stops anything holding the image, makes sure it is raw, and empties
        the staging dir: leftovers from the previous session's dump would
        otherwise mix into the injection, and the newer-file guard could skip
        archive members over them. Marks the restore pending so the next
        launch injects what lands.
        """
        self.stop()
        _reap_strays()
        _ensure_raw_image(self.hdd_image)
        self._clear_staging()
        self._restore_pending = True

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Inject any restored saves, pin display settings and boot the disc.

        Args:
            rom_path: The XISO to boot.
            resume_slot: Ignored with a warning; a raw image holds no states.
        """
        self.stop()
        _reap_strays()
        raw_ok = _ensure_raw_image(self.hdd_image)

        self._title_id = _disc_title_id(rom_path)
        if self._title_id:
            log.info("disc title id: %s", self._title_id)
        else:
            log.warning("no title id for %s; this session will not archive or "
                        "restore any save data", rom_path)

        if self._restore_pending:
            self._restore_pending = False
            if raw_ok:
                injected = self._inject_saves()
                log.info("pre-launch: injected %d save file(s) into the HDD image", injected)
            else:
                log.error("pre-launch: HDD image is not raw; restored saves were NOT injected")

        if resume_slot is not None:
            log.warning("resume: save states are not supported on a raw HDD "
                        "image; slot %d ignored, booting fresh", resume_slot)

        _pin_display_settings()

        log.info("launching xemu (rom=%s)", rom_path)
        self._spawn([XEMU_BIN, "-dvd_path", str(rom_path)], _launch_env())

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Stop xemu and stage the launched title's saves for the dump.

        Args:
            slot: Ignored; there are no save states.

        A failed extraction leaves the staging directory alone rather than
        emptying it, so the dump uploads nothing instead of an empty archive
        over the title's real saves.

        Returns:
            The state fields all None (`state_saved`, `state_slot`,
            `state_file`), plus `title_id` and `saves_extracted`, the number
            of files staged.
        """
        self.stop()
        _reap_strays()
        extracted = 0
        if not self.hdd_image.is_file():
            log.error("post-close: HDD image %s is missing; nothing to extract",
                      self.hdd_image)
        else:
            staged = self._extract_saves()
            if staged is None:
                log.error("post-close: could not read saves out of %s for title %s; "
                          "the staging dir keeps what this session restored so the "
                          "dump does not ship an empty archive",
                          self.hdd_image, self._title_id or "<unknown>")
            else:
                extracted = staged
                log.info("post-close: staged %d save file(s) for title %s",
                         extracted, self._title_id or "<unknown>")
        return {
            "state_saved": None,
            "state_slot": None,
            "state_file": None,
            "title_id": self._title_id,
            "saves_extracted": extracted,
        }
