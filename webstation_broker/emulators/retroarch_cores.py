"""The libretro core catalog, core tiers and per-platform core profiles.

RetroArch boots one core per platform, and the platform table in
`retroarch_platforms.json` names the best-tested one. This module answers the
question an operator's `core:` override raises: is that core known, is it
built for this machine, how well tested is it, and what profile (library
name, save handling, extensions) should the launch use.

It never imports `retroarch.py`; the platform table is passed in, so the
launcher can import this module without a cycle.
"""

import dataclasses
import io
import logging
import re
import zipfile
from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Optional

log = logging.getLogger(__name__)

CORE_NAME_RE = re.compile(r"^[a-z0-9_]+$")
"""A libretro core name as the buildbot spells it, which is also what `core:` accepts."""

_INFO_SUFFIX = "_libretro.info"
_INDEX_SUFFIX = "_libretro.so.zip"
_INFO_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*$')


def truthy(value: Optional[str]) -> bool:
    """Read a boolean env var the way the broker's other ones are read.

    Args:
        value: The raw value, or None when unset.

    Returns:
        True for `1`, `true`, `yes` or `on`, in any case and with whitespace around.
    """
    return value is not None and value.strip().lower() in ("1", "true", "yes", "on")


def normalize_extensions(raw: str) -> tuple[str, ...]:
    """Turn a core-info `supported_extensions` value into the platform table's form.

    Args:
        raw: Pipe-separated extensions, dotless and in any case (`sfc|SMC`).

    Returns:
        Lowercase, dotted extensions in their original order, without repeats.
    """
    seen: dict[str, None] = {}
    for part in raw.split("|"):
        ext = part.strip().lower().lstrip(".")
        if ext:
            seen.setdefault(f".{ext}", None)
    return tuple(seen)


@dataclasses.dataclass(frozen=True)
class CoreInfo:
    """What core-info says about one core.

    Attributes:
        core: The core's file name without `_libretro.so`.
        display_name: The human name core-info gives it.
        corename: The name core-info records, usually the core's `library_name`.
        extensions: Normalized supported extensions.
    """

    core: str
    display_name: str
    corename: str
    extensions: tuple[str, ...]


def _parse_info_text(text: str) -> dict[str, str]:
    """Read the `key = "value"` lines of one `.info` file.

    Args:
        text: The file's contents.

    Returns:
        Its keys and values; lines that are not assignments are skipped.
    """
    keys: dict[str, str] = {}
    for line in text.splitlines():
        match = _INFO_LINE_RE.match(line)
        if match:
            keys[match.group(1)] = match.group(2)
    return keys


def parse_info_zip(data: bytes) -> dict[str, CoreInfo]:
    """Parse libretro's `info.zip` into one CoreInfo per core.

    Args:
        data: The zip's bytes.

    Returns:
        Core name to its info. Members that are not `<core>_libretro.info` with
        a valid core name are skipped.

    Raises:
        zipfile.BadZipFile: When `data` is not a zip.
    """
    cores: dict[str, CoreInfo] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            leaf = PurePosixPath(member.filename).name
            if member.is_dir() or not leaf.endswith(_INFO_SUFFIX):
                continue
            core = leaf[: -len(_INFO_SUFFIX)]
            if not CORE_NAME_RE.match(core):
                continue
            keys = _parse_info_text(zf.read(member).decode("utf-8", "replace"))
            cores[core] = CoreInfo(
                core=core,
                display_name=keys.get("display_name", core),
                corename=keys.get("corename", core),
                extensions=normalize_extensions(keys.get("supported_extensions", "")),
            )
    return cores


def parse_index(text: str) -> frozenset[str]:
    """Read the core names out of a buildbot `.index` or `.index-extended`.

    Args:
        text: The index body; each line ends with `<core>_libretro.so.zip`.

    Returns:
        The core names with a valid spelling.
    """
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if not parts or not parts[-1].endswith(_INDEX_SUFFIX):
            continue
        core = parts[-1][: -len(_INDEX_SUFFIX)]
        if CORE_NAME_RE.match(core):
            names.add(core)
    return frozenset(names)


@dataclasses.dataclass(frozen=True)
class Catalog:
    """The cores an operator may pick: in core-info and built for x86_64.

    Attributes:
        cores: Core name to its info, read-only.
        info_zip: The zip the catalog was built from, for installing `.info` files.
    """

    cores: Mapping[str, CoreInfo]
    info_zip: bytes

    def info_file(self, core: str) -> Optional[bytes]:
        """The core's `.info` file from `info_zip`, or None when the catalog lacks it.

        Args:
            core: The core name.

        Returns:
            The file's bytes, or None.
        """
        if core not in self.cores:
            return None
        with zipfile.ZipFile(io.BytesIO(self.info_zip)) as zf:
            for member in zf.infolist():
                if PurePosixPath(member.filename).name == f"{core}{_INFO_SUFFIX}":
                    return zf.read(member)
        return None


def build_catalog(zip_bytes: bytes, index_text: str, installed: frozenset[str] = frozenset()) -> Catalog:
    """Build the catalog: cores in the zip that the index builds, or already installed.

    Args:
        zip_bytes: libretro's `info.zip`.
        index_text: The buildbot x86_64 `.index`.
        installed: Cores whose `.so` is already in `CORES_DIR`; a refresh
            dropping one from the index must not lock players out of it (§7).

    Returns:
        The catalog.
    """
    built = parse_index(index_text) | installed
    cores = {name: info for name, info in parse_info_zip(zip_bytes).items() if name in built}
    return Catalog(MappingProxyType(cores), zip_bytes)
