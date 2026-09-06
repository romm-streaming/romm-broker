"""Administration session that launches the full webstation desktop.

Starts selkies-desktop so the user can configure emulators through the GUI.
Managed like any emulator session, just with no ROM and no save sync.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)


class Desktop(Emulator):
    """The webstation desktop, run as a session with no ROM and no save sync.

    Attributes:
        name: Registry key, `desktop`.
        display_name: Shown as "Webstation Desktop".
        requires_rom: Off; a desktop session boots nothing.
        save_root: Left at `/config`.
        save_subtrees: Empty, so the save routes have nothing to dump or restore.
        log_path: `/config/selkies-desktop.log`.
    """

    name = "desktop"
    """Registry key for the desktop session."""
    display_name = "Webstation Desktop"
    """Name the UI shows for the desktop session."""
    requires_rom = False
    """A desktop session boots nothing, so no ROM is needed."""
    save_root = Path("/config")
    """Root of the writable data; nothing under it is synced."""
    save_subtrees = ()
    """Empty: there is no save data to dump or restore."""
    log_path = Path("/config/selkies-desktop.log")
    """Where selkies-desktop's output is appended."""

    def launch(self, rom_path: Optional[Path], resume_slot: Optional[int]) -> None:
        """Start selkies-desktop, replacing any session already running.

        The binary comes from `DESKTOP_BIN` (default `selkies-desktop`).

        Args:
            rom_path: Ignored; the desktop has no content to boot.
            resume_slot: Ignored; the desktop has no state.

        Raises:
            OSError: When the binary cannot be started or its pid cannot be
                recorded; logged here since this launch failure otherwise
                surfaced nowhere.
        """
        self.stop()
        binary = os.environ.get("DESKTOP_BIN", "selkies-desktop")
        try:
            self._spawn([binary], base_launch_env())
        except OSError:
            log.exception("desktop: failed to launch %s", binary)
            raise
