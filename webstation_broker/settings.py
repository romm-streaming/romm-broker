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

GAMEPAD_SLOTS = int(os.environ.get("BROKER_GAMEPAD_SLOTS", "4"))
"""Number of virtual gamepad slots, from `BROKER_GAMEPAD_SLOTS` (default `4`)."""

MAX_ROOM_VIEWERS = int(os.environ.get("BROKER_MAX_ROOM_VIEWERS", "32"))
"""Ceiling on concurrent viewer seats in one session, from `BROKER_MAX_ROOM_VIEWERS` (default 32).

An invite link is reusable and unauthenticated past the token itself, and each
anonymous arrival on one mints a new seat with nothing to de-duplicate
against. At the ceiling, a disconnected anonymous seat is reclaimed for the
new arrival; a named user's seat is never reclaimed.
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
