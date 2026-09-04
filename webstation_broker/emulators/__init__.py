"""Emulator launchers and the registry the broker picks them from.

Every launcher subclasses `Emulator` from `base` and is keyed by its `name` in
`REGISTRY`; `get_emulator` is how the rest of the broker turns a name from the
activate payload into a fresh instance.
"""

from typing import Optional

from .azahar import Azahar
from .base import Emulator
from .cemu import Cemu
from .desktop import Desktop
from .dolphin import Dolphin
from .duckstation import Duckstation
from .eden import Eden
from .flycast import Flycast
from .pcsx2 import Pcsx2
from .ppsspp import Ppsspp
from .retroarch import Retroarch
from .rpcs3 import Rpcs3
from .scummvm import Scummvm
from .shadps4 import Shadps4
from .xemu import Xemu
from .xenia import Xenia

REGISTRY: dict[str, type[Emulator]] = {
    "pcsx2": Pcsx2,
    "duckstation": Duckstation,
    "dolphin": Dolphin,
    "flycast": Flycast,
    "cemu": Cemu,
    "azahar": Azahar,
    "eden": Eden,
    "shadps4": Shadps4,
    "retroarch": Retroarch,
    "rpcs3": Rpcs3,
    "xemu": Xemu,
    "xenia": Xenia,
    "ppsspp": Ppsspp,
    "scummvm": Scummvm,
    "desktop": Desktop,
}
"""Emulator name to launcher class, the names the activate payload may ask for."""


def get_emulator(name: str) -> Optional[Emulator]:
    """Instantiate the launcher registered under `name`.

    Args:
        name: An emulator name as it appears in `REGISTRY`.

    Returns:
        A new launcher instance, or None when the name is not registered.
    """
    cls = REGISTRY.get(name)
    return cls() if cls else None
