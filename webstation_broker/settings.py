"""Environment-driven configuration.

Every setting is read once, at import time, from the process environment. Each
attribute below names the variable it reads and the default that applies when
the variable is unset.
"""

import os
from pathlib import Path
from typing import Optional


def truthy(value: Optional[str]) -> bool:
    """Read a boolean env var.

    Args:
        value: The raw value, or None when unset.

    Returns:
        True for `1`, `true`, `yes` or `on`, in any case and with whitespace around.
    """
    return value is not None and value.strip().lower() in ("1", "true", "yes", "on")


def _prefix() -> str:
    """Return the normalized URL prefix the broker is served under, e.g. `/streaming`.

    Read from `SUBFOLDER`, which also drives the nginx templating and the vite
    base. A missing leading slash is added and any trailing slash is dropped.

    Returns:
        The prefix with a leading slash and no trailing slash, or an empty string
        when `SUBFOLDER` is just `/`.
    """
    raw = os.environ.get("SUBFOLDER", "/streaming/").strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/")


PREFIX = _prefix()
"""URL prefix the broker is mounted under, from `SUBFOLDER` (default `/streaming/`), normalized."""

HOST = os.environ.get("BROKER_HOST", "127.0.0.1")
"""Address uvicorn binds to, from `BROKER_HOST` (default `127.0.0.1`)."""
PORT = int(os.environ.get("BROKER_PORT", "8000"))
"""Port uvicorn listens on, from `BROKER_PORT` (default `8000`)."""

BROKER_SECRET = os.environ.get("BROKER_SECRET", "")
"""Shared secret for the session lifecycle endpoints, from `BROKER_SECRET`.

Leaving it unset refuses to start unless `BROKER_DEV_MODE` is set, which
starts the broker unauthenticated instead.
"""

_control_url = os.environ.get("SELKIES_CONTROL_URL", "").rstrip("/")
"""Base of the token control endpoint used by selkies, from `SELKIES_CONTROL_URL` (default empty)."""
if _control_url:
    SELKIES_TOKEN_URLS = [f"{_control_url}/api/tokens", f"{_control_url}/tokens"]
    """Candidate selkies token endpoints, both paths under `SELKIES_CONTROL_URL`."""
else:
    SELKIES_TOKEN_URLS = [
        "http://127.0.0.1:8082/api/tokens",
        "http://127.0.0.1:8083/tokens",
    ]
    """Candidate selkies token endpoints when `SELKIES_CONTROL_URL` is unset.

    One entry per known selkies image, each on the port and path that image
    answers on.
    """
SELKIES_MASTER_TOKEN = os.environ.get("SELKIES_MASTER_TOKEN", "")
"""Bearer token for the selkies token endpoint, from `SELKIES_MASTER_TOKEN` (default empty)."""

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""ROM library root, from `ROM_ROOT` (default `/romm`); activate rejects paths outside it."""


def rom_root() -> Path:
    """Return `ROM_ROOT` with symlinks resolved, for containment checks.

    Callers test a candidate with `candidate.resolve().is_relative_to(...)`, so
    the root has to be resolved on the same terms. Bind and NFS mount layouts
    routinely make `/romm` itself a symlink, and comparing a resolved candidate
    against an unresolved root rejects every ROM in the library. This resolves
    per call rather than at import so that a test or a caller reassigning
    `ROM_ROOT` still gets a correct answer.

    Returns:
        The resolved ROM library root.
    """
    return ROM_ROOT.resolve()

EXPORT_DIR = Path(os.environ.get("BROKER_EXPORT_DIR", "/config/broker-exports"))
"""Where exit writes its save archives, from `BROKER_EXPORT_DIR` (default `/config/broker-exports`).

Written always in dev mode, otherwise only when the upload to the callback
origin fails.
"""

IMPORT_DIR = Path(os.environ.get("BROKER_IMPORT_DIR", "/config/broker-imports"))
"""Where the parent uploads archives to restore, from `BROKER_IMPORT_DIR`.

Defaults to `/config/broker-imports`; activate's `save.archive` path points
into here.
"""

SAVE_UPLOAD_PATH = os.environ.get("BROKER_SAVE_UPLOAD_PATH", "/api/webstation/saves")
"""Exit upload target path, from `BROKER_SAVE_UPLOAD_PATH` (default `/api/webstation/saves`).

Appended to the callback base URL, which is the parent origin derived at
activate unless the payload supplies one.
"""
SAVE_UPLOAD_TIMEOUT = float(os.environ.get("BROKER_SAVE_UPLOAD_TIMEOUT", "30"))
"""Seconds allowed for the exit upload, from `BROKER_SAVE_UPLOAD_TIMEOUT` (default `30`)."""

EMULATOR_WATCH_INTERVAL = float(os.environ.get("BROKER_EMULATOR_WATCH_INTERVAL", "15"))
"""Seconds between checks that the session's emulator is still running, from
`BROKER_EMULATOR_WATCH_INTERVAL` (default `15`); `0` turns the watch off.
"""

FRONTEND_DIST = Path(
    os.environ.get("BROKER_FRONTEND_DIST", "/usr/share/webstation-broker/www")
)
"""Built frontend served in non-dev mode, from `BROKER_FRONTEND_DIST`.

Defaults to `/usr/share/webstation-broker/www`; ignored when vite serves the
page.
"""

STATE_FILE_MAX_BYTES = int(os.environ.get("BROKER_STATE_FILE_MAX_BYTES", str(256 * 1024 * 1024)))
"""Ceiling on a single state file moving either way over the state-file routes.

From `BROKER_STATE_FILE_MAX_BYTES` (default 256 MiB). RomM caps its side of
the same transfer, so raising one without the other just moves which end
refuses.
"""

SAVE_FILE_MAX_ENTRIES = int(os.environ.get("BROKER_SAVE_FILE_MAX_ENTRIES", "10000"))
"""Ceiling on the number of members a save/memory-card archive may contain.

Independent of `SAVE_FILE_MAX_BYTES`: a byte-size cap alone doesn't stop an
archive of huge numbers of near-zero-byte deeply-nested entries from
exhausting inodes or hanging the restore walk.
"""

STATE_SCREENSHOT_SIZE = int(os.environ.get("BROKER_STATE_SCREENSHOT_SIZE", "640"))
"""Longest side, in pixels, of the frame captured with a state.

From `BROKER_STATE_SCREENSHOT_SIZE` (default 640). The capture is the whole
streamed desktop, so it is scaled down to this before it is served as the
state's thumbnail.
"""

DEV_MODE = os.environ.get("BROKER_DEV_MODE", "").lower() == "true"
"""Whether dev mode is on, from `BROKER_DEV_MODE` (default off; only the string `true` enables it)."""

FRAME_ANCESTORS = os.environ.get("BROKER_FRAME_ANCESTORS", "").strip()
"""Who may embed the room, from `BROKER_FRAME_ANCESTORS` (default unset: anyone).

A CSP `frame-ancestors` source list, e.g. `'self'` when RomM serves the room
under its own `SUBFOLDER`, or `https://romm.example.com` when it embeds the
room cross-origin. Unset by default, since the right value depends on how
RomM is deployed and a wrong one blanks the player.
"""

CSP_ENFORCE = truthy(os.environ.get("BROKER_CSP_ENFORCE"))
"""Whether the room page's Content-Security-Policy is enforced, from `BROKER_CSP_ENFORCE`.

Default off: the policy is sent as `Content-Security-Policy-Report-Only`, so a
page it would break still works and the violation shows in the browser console.
"""

GAMEPAD_SLOTS = int(os.environ.get("BROKER_GAMEPAD_SLOTS", "4"))
"""Number of virtual gamepad slots, from `BROKER_GAMEPAD_SLOTS` (default `4`)."""

MAX_ROOM_VIEWERS = int(os.environ.get("BROKER_MAX_ROOM_VIEWERS", "32"))
"""Ceiling on concurrent viewer seats in one session, from `BROKER_MAX_ROOM_VIEWERS` (default 32).

An invite link is reusable and unauthenticated past the token itself, and each
anonymous arrival on one mints a new seat with nothing to de-duplicate
against. At the ceiling, a disconnected anonymous seat is reclaimed for the
new arrival; a named user's seat is never reclaimed.
"""

RPCS3_CACHE_ENABLED = truthy(os.environ.get("RPCS3_CACHE_ENABLED"))
"""Whether RPCS3 extracts archived ROMs into its cache, from `RPCS3_CACHE_ENABLED` (default off).

Off, an archived PS3 ROM is refused rather than re-extracted on every launch.
"""

SHADPS4_CACHE_ENABLED = truthy(os.environ.get("SHADPS4_CACHE_ENABLED"))
"""Whether shadPS4 extracts .pkg and archived ROMs into its cache, from `SHADPS4_CACHE_ENABLED` (default off).

Off, those formats are refused, since a PS4 title only boots once extracted.
"""

SCUMMVM_CACHE_ENABLED = truthy(os.environ.get("SCUMMVM_CACHE_ENABLED", "true"))
"""Whether ScummVM extracts archived games into its cache, from `SCUMMVM_CACHE_ENABLED` (default on).

A ScummVM game is a folder, so a `.zip`, `.7z` or `.rar` only boots once
extracted. Off, an archived game is refused.
"""

PPSSPP_CACHE_ENABLED = truthy(os.environ.get("PPSSPP_CACHE_ENABLED"))
"""Whether PPSSPP extracts archived ROMs into its cache, from `PPSSPP_CACHE_ENABLED` (default off).

PPSSPP does not boot from inside an archive, so a zipped image only boots once
extracted. Off, an archived ROM is refused.
"""

RETROARCH_MSU1_CACHE_ENABLED = truthy(os.environ.get("RETROARCH_MSU1_CACHE_ENABLED", "true"))
"""Whether a zipped MSU-1 game is extracted to boot, from `RETROARCH_MSU1_CACHE_ENABLED` (default on).

Snes9x reads a game's `.msu` data and `-N.pcm` tracks from beside the ROM on
disk, so a zip handed to RetroArch boots without them. Off, the zip boots as
is, without its MSU-1 audio and video.
"""

ROM_CACHE_ENABLED = truthy(os.environ.get("ROM_CACHE_ENABLED"))
"""Whether ROMs are copied to local disk and booted from there, from `ROM_CACHE_ENABLED` (default off).

For libraries on a slow network mount. Off, every launch reads the ROM from
`ROM_ROOT` exactly as it always has, and nothing is written for the cache.
"""

ROM_CACHE_DIR = Path(os.environ.get("ROM_CACHE_DIR", "/config/rom-cache"))
"""Where cached ROM copies live, from `ROM_CACHE_DIR` (default `/config/rom-cache`).

Has to be local disk: a cache on the same network mount as the library only
adds a copy without saving a single read.
"""

ROM_CACHE_MODE = (
    "blocking" if os.environ.get("ROM_CACHE_MODE", "").strip().lower() == "blocking" else "background"
)
"""When an uncached ROM is copied, from `ROM_CACHE_MODE` (default `background`).

`background` boots the first launch from `ROM_ROOT` straight away and copies
alongside it, so the copy serves the next launch. `blocking` copies before the
boot, so even the first launch plays from local disk, at the cost of waiting
for the copy. Any other value reads as `background`.
"""

ROM_CACHE_MAX_GB = float(os.environ.get("ROM_CACHE_MAX_GB", "100"))
"""Cap on the ROM cache's total size in GB, from `ROM_CACHE_MAX_GB` (default 100; `0` is no cap).

Least recently launched games are evicted first.
"""

ROM_CACHE_MAX_COUNT = int(os.environ.get("ROM_CACHE_MAX_COUNT", "50"))
"""Cap on how many games the ROM cache keeps, from `ROM_CACHE_MAX_COUNT` (default 50; `0` is no cap).

Least recently launched games are evicted first.
"""

ROM_CACHE_MAX_AGE_DAYS = float(os.environ.get("ROM_CACHE_MAX_AGE_DAYS", "30"))
"""Days a cached game is kept without a launch, from `ROM_CACHE_MAX_AGE_DAYS`.

Defaults to 30; `0` keeps a game until the count or size limit evicts it.
"""

ROM_CACHE_COPY_MBPS = float(os.environ.get("ROM_CACHE_COPY_MBPS", "20"))
"""Speed cap on a background copy in MB/s, from `ROM_CACHE_COPY_MBPS` (default 20; `0` is uncapped).

A background copy shares the link with the game the player is running from
`ROM_ROOT`, so an uncapped one can starve the emulator's own reads and make
that session stutter. A blocking copy is never capped: the player is waiting
on it.
"""

ROM_CACHE_COPY_TIMEOUT = float(os.environ.get("ROM_CACHE_COPY_TIMEOUT", "300"))
"""Seconds a blocking copy may take before the launch boots from `ROM_ROOT` instead.

From `ROM_CACHE_COPY_TIMEOUT` (default 300; `0` or less uses the default). It has to stay under RomM's
`STREAMING_LAUNCH_TIMEOUT` (default 600), or RomM gives up on the activate
while the copy is still running.
"""

XEMU_SOFTWARE_GL = truthy(os.environ.get("XEMU_SOFTWARE_GL"))
"""Whether xemu renders on the CPU via `LIBGL_ALWAYS_SOFTWARE`, from `XEMU_SOFTWARE_GL` (default off).

Which renderer xemu asks for and whether the driver can answer are separate
problems: on the AMD Renoir stack these containers run on, xemu aborts in
gl_fence on the OpenGL path and in RADV on the Vulkan one. Set
`XEMU_SOFTWARE_GL` to render xemu on the CPU there, which the container-wide
`LIBGL_ALWAYS_SOFTWARE` cannot do without dragging every other emulator down
with it. Slow, so it stays off unless the host needs it.
"""

RETROARCH_EXPERIMENTAL_CORES = truthy(os.environ.get("RETROARCH_EXPERIMENTAL_CORES"))
"""Whether a blocked RetroArch core may launch, from `RETROARCH_EXPERIMENTAL_CORES` (default off).

RomM's per-ROM `experimental_cores` flag lifts the same block for one launch.
"""

RETROARCH_CORE_INFO_REFRESH = truthy(os.environ.get("RETROARCH_CORE_INFO_REFRESH"))
"""Whether the RetroArch core catalog refreshes in the background.

From `RETROARCH_CORE_INFO_REFRESH` (default off).
"""

RETROARCH_CORE_INFO_URL = (
    os.environ.get("RETROARCH_CORE_INFO_URL", "").strip()
    or "https://buildbot.libretro.com/assets/frontend/info.zip"
)
"""Where a catalog refresh downloads the core-info zip from, from `RETROARCH_CORE_INFO_URL`."""

RETROARCH_CORE_INDEX_URL = (
    os.environ.get("RETROARCH_CORE_INDEX_URL", "").strip()
    or "https://buildbot.libretro.com/nightly/linux/x86_64/latest/.index"
)
"""Where a catalog refresh downloads the buildbot core index from, from `RETROARCH_CORE_INDEX_URL`."""
