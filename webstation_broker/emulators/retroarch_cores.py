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
import functools
import hashlib
import io
import json
import logging
import os
import re
import secrets
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Optional

import anyio.to_thread
import httpx

log = logging.getLogger(__name__)

CORE_NAME_RE = re.compile(r"^[a-z0-9_]+$")
"""A libretro core name as the buildbot spells it, which is also what `core:` accepts."""

_INFO_SUFFIX = "_libretro.info"
_INDEX_SUFFIX = "_libretro.so.zip"
_INFO_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*$')

_HERE = Path(__file__).parent
BUNDLED_ZIP = _HERE / "retroarch_core_info.zip"
"""libretro's info.zip, byte for byte, bundled so the catalog works offline."""
BUNDLED_SOURCE = _HERE / "retroarch_core_info.source.json"
"""Where `BUNDLED_ZIP` came from, its sha256 and the x86_64 build list at that time."""
INFO_MEMBER_CAP = 64 * 1024
"""Largest decompressed `.info` member read from a core-info zip, 64 KiB.

The zip cap (`ZIP_CAP`) bounds the compressed download only; a hostile member
can inflate far past it. The largest bundled `.info` is under 14 KiB, so a
member over this cap is not a real core-info file and is skipped.
"""


def truthy(value: Optional[str]) -> bool:
    """Read a boolean env var the way the broker's other ones are read.

    Args:
        value: The raw value, or None when unset.

    Returns:
        True for `1`, `true`, `yes` or `on`, in any case and with whitespace around.
    """
    return value is not None and value.strip().lower() in ("1", "true", "yes", "on")


def safe_dir_name(value: object) -> Optional[str]:
    r"""A value usable as a single path component under a sorted save or state dir, or None.

    Shared by every place a name that came from outside the broker (a restored
    archive's manifest, a core-info `corename`) is joined under `saves/` or
    `states/`: none of them may name a path outside the dir it is joined under.

    Args:
        value: The candidate name.

    Returns:
        `value` when it is a non-empty string, not "." or "..", and contains
        none of "/", "\\" or NUL; otherwise None.
    """
    if not isinstance(value, str) or not value or value in (".", ".."):
        return None
    if "/" in value or "\\" in value or "\0" in value:
        return None
    return value


def _oversized(member: zipfile.ZipInfo) -> bool:
    """Whether a zip member inflates past `INFO_MEMBER_CAP`, logging when it does.

    Args:
        member: The member.

    Returns:
        True when its declared decompressed size is over the cap.
    """
    if member.file_size <= INFO_MEMBER_CAP:
        return False
    log.warning(
        "retroarch: core info member %s is %d bytes decompressed, over the %d byte cap; skipped",
        member.filename,
        member.file_size,
        INFO_MEMBER_CAP,
    )
    return True


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
        a valid core name, or that inflate past `INFO_MEMBER_CAP`, are skipped;
        an unsafe `corename` is replaced by the core name.

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
            if not CORE_NAME_RE.match(core) or _oversized(member):
                continue
            keys = _parse_info_text(zf.read(member).decode("utf-8", "replace"))
            corename = keys.get("corename", core)
            if safe_dir_name(corename) is None:
                # An untested core's library_name defaults to its corename, which
                # names a dir under saves/ and states/; a hostile catalog must not
                # steer that outside them. The core name already matched
                # CORE_NAME_RE, so it is a safe stand-in.
                log.warning(
                    "retroarch: core info for %s has an unsafe corename %r, using the core name",
                    core,
                    corename,
                )
                corename = core
            cores[core] = CoreInfo(
                core=core,
                display_name=keys.get("display_name", core),
                corename=corename,
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
        cache_zip: The zip a background refresh wrote, or None before any refresh has
            landed (§7).
        protected: Cores a refresh may never change: every default, every alternate
            and every core in `TIERS`. `info_file` never serves one of these from
            `cache_zip`, even when the merged catalog holds one.
    """

    cores: Mapping[str, CoreInfo]
    info_zip: bytes
    cache_zip: Optional[bytes] = None
    protected: frozenset[str] = frozenset()

    def info_file(self, core: str) -> Optional[bytes]:
        """The core's `.info` file, preferring the refreshed zip for an unprotected core.

        Args:
            core: The core name.

        Returns:
            The file's bytes, or None when the catalog lacks it. A protected core
            is always read from `info_zip`; an unprotected one is looked up in
            `cache_zip` first and falls back to `info_zip`, so a core the cache no
            longer lists still resolves to the bundled entry. A member over
            `INFO_MEMBER_CAP` is never read.
        """
        if core not in self.cores:
            return None
        zips = (
            (self.info_zip,)
            if self.cache_zip is None or core in self.protected
            else (self.cache_zip, self.info_zip)
        )
        for zip_bytes in zips:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for member in zf.infolist():
                    if PurePosixPath(member.filename).name != f"{core}{_INFO_SUFFIX}":
                        continue
                    if not _oversized(member):
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


@functools.cache
def load_bundled_catalog(installed: frozenset[str] = frozenset()) -> Catalog:
    """Build the catalog from the bundled zip and the build list in its source file.

    Args:
        installed: Cores already in `CORES_DIR`.

    Returns:
        The catalog.

    Raises:
        ValueError: When the zip does not match the recorded sha256; a
            half-updated bundle must stop the broker, not boot a wrong catalog.
    """
    data = BUNDLED_ZIP.read_bytes()
    source = json.loads(BUNDLED_SOURCE.read_text())
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ValueError(f"{BUNDLED_ZIP.name} does not match the sha256 in {BUNDLED_SOURCE.name}")
    index = "\n".join(f"{core}_libretro.so.zip" for core in source["x86_64_cores"])
    return build_catalog(data, index, installed)


LIBRARY_NAME_FIXES: Mapping[str, str] = MappingProxyType(
    {"dolphin": "dolphin-emu", "vecx": "VecX", "freeintv": "freeintv"}
)
"""Cores whose real `library_name` (from their own source) differs from core-info's `corename`."""

CORE_OWNED_FIELDS = frozenset({
    "save_subtrees", "core_options", "core_option_seeds", "core_source", "assets",
    "save_links", "resume_settle", "state_confirm_wait", "savestate", "extra_extensions",
})
"""Profile fields that belong to a core, so an alternate never inherits them from the default."""

TIERS_FILE = _HERE / "retroarch_core_tiers.json"
"""Tier of every non-default core that is not `untested`."""

_TIER_KEYS = {"tier", "reason", "reports", "platforms"}


@dataclasses.dataclass(frozen=True)
class TierEntry:
    """One core's row in the tiers file.

    Attributes:
        tier: `vetted` or `blocked`.
        reason: Why a blocked core is blocked; None for vetted.
        reports: Issue references backing the tier.
        platforms: Platforms a block is narrowed to, or None for all.
    """

    tier: str
    reason: Optional[str]
    reports: tuple[str, ...]
    platforms: Optional[frozenset[str]]


def load_tiers(raw: Mapping[str, Any]) -> dict[str, TierEntry]:
    """Validate the tiers file (§5.2).

    Args:
        raw: The parsed JSON.

    Returns:
        Core name to its entry.

    Raises:
        ValueError: On a bad core name, an unknown key or tier, a blocked core
            with no reason, or `platforms` on anything but a block.
    """
    tiers: dict[str, TierEntry] = {}
    for core, entry in raw.items():
        where = f"{TIERS_FILE.name}: {core}"
        if not CORE_NAME_RE.match(core):
            raise ValueError(f"{where}: not a core name")
        unknown = set(entry) - _TIER_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown key {sorted(unknown)}")
        tier = entry.get("tier")
        if tier not in ("vetted", "blocked"):
            raise ValueError(f"{where}: tier must be vetted or blocked, not {tier!r}")
        reason = entry.get("reason")
        if tier == "blocked" and not reason:
            raise ValueError(f"{where}: a blocked core needs a reason")
        platforms = entry.get("platforms")
        if platforms is not None and tier != "blocked":
            raise ValueError(f"{where}: platforms only narrows a block")
        tiers[core] = TierEntry(
            tier,
            reason,
            tuple(entry.get("reports", ())),
            frozenset(platforms) if platforms is not None else None,
        )
    return tiers


TIERS: Mapping[str, TierEntry] = MappingProxyType(load_tiers(json.loads(TIERS_FILE.read_text())))
"""The loaded tiers file."""

Profile = Mapping[str, Any]
"""A resolved, read-only platform profile: the platform entry's keys plus `tier` and `display_name`."""

REPORT_URL = "https://github.com/romm-streaming/romm-broker/issues/new?template=core-report.yml"
"""Where players report how a core works (§9)."""


class CoreRejectedError(ValueError):
    """A `core:` the broker will not launch on this platform; `detail` is shown to the player."""

    def __init__(self, detail: str) -> None:
        """Keep the detail for the 422.

        Args:
            detail: The message, naming the options.
        """
        super().__init__(detail)
        self.detail = detail


def _blocking_entry(tiers: Mapping[str, TierEntry], core: str, platform: str) -> Optional[TierEntry]:
    """The tiers entry blocking `core` on `platform`, or None.

    Args:
        tiers: The tiers table.
        core: The core name.
        platform: The platform slug.

    Returns:
        The entry when it is a block that covers the platform.
    """
    entry = tiers.get(core)
    if entry is None or entry.tier != "blocked":
        return None
    if entry.platforms is not None and platform not in entry.platforms:
        return None
    return entry


def _untested_extensions(platform_exts: tuple[str, ...], info: CoreInfo) -> tuple[str, ...]:
    """The platform's extensions the core also supports, in platform order (§5.4).

    Args:
        platform_exts: The platform entry's extensions.
        info: The core's catalog entry.

    Returns:
        The intersection.
    """
    supported = set(info.extensions)
    return tuple(ext for ext in platform_exts if ext in supported)


def tier_of(
    platforms: Mapping[str, Mapping[str, Any]],
    platform: str,
    core: str,
    catalog: Catalog,
    tiers: Mapping[str, TierEntry],
) -> Optional[str]:
    """The tier `core` has on `platform`, or None when it is not offered there.

    Args:
        platforms: The platform table.
        platform: The platform slug.
        core: The core name.
        catalog: The current catalog.
        tiers: The tiers table.

    Returns:
        `default`, `vetted`, `blocked`, `untested`, or None.
    """
    entry = platforms.get(platform)
    if entry is None:
        return None
    if core == entry["core"]:
        return "default"
    if core in entry.get("alternates", {}):
        return "vetted"
    info = catalog.cores.get(core)
    if info is None or not _untested_extensions(entry["extensions"], info):
        return None
    return "blocked" if _blocking_entry(tiers, core, platform) else "untested"


_TIER_ORDER = {"default": 0, "vetted": 1, "untested": 2, "blocked": 3}
"""Sort order for tiers in the cores route."""


def cores_for_platform(
    platforms: Mapping[str, Mapping[str, Any]],
    platform: str,
    catalog: Catalog,
    tiers: Mapping[str, TierEntry],
) -> list[dict[str, Any]]:
    """Every core offered on `platform`, for the cores route and the docs table (§9).

    Args:
        platforms: The platform table.
        platform: The platform slug.
        catalog: The current catalog.
        tiers: The tiers table.

    Returns:
        One row per core, default first.
    """
    entry = platforms[platform]
    names = {entry["core"], *entry.get("alternates", {}), *catalog.cores}
    rows = []
    for core in names:
        tier = tier_of(platforms, platform, core, catalog, tiers)
        if tier is None:
            continue
        info = catalog.cores.get(core)
        tier_entry = tiers.get(core)
        rows.append({
            "core": core,
            "display_name": info.display_name if info else core,
            "tier": tier,
            "reason": tier_entry.reason if tier_entry and tier == "blocked" else None,
            "reports": list(tier_entry.reports) if tier_entry else [],
            "verified": tier in ("default", "vetted"),
            "report_url": f"{REPORT_URL}&core={core}&platform={platform}",
        })
    rows.sort(key=lambda r: (_TIER_ORDER[r["tier"]], r["core"]))
    return rows


def _options_detail(
    platforms: Mapping[str, Mapping[str, Any]],
    platform: str,
    catalog: Catalog,
    tiers: Mapping[str, TierEntry],
) -> str:
    """Name what a platform does offer, for a rejected core.

    Args:
        platforms: The platform table.
        platform: The platform slug.
        catalog: The current catalog.
        tiers: The tiers table.

    Returns:
        One sentence listing the default, the vetted cores and a count of the rest.
    """
    entry = platforms[platform]
    vetted = sorted(entry.get("alternates", {}))
    untested = sum(
        1
        for core in catalog.cores
        if core != entry["core"]
        and core not in vetted
        and tier_of(platforms, platform, core, catalog, tiers) == "untested"
    )
    listed = ", ".join(vetted) if vetted else "none"
    return (
        f"default {entry['core']}; vetted: {listed}; {untested} untested; "
        f"see GET /api/retroarch/cores?platform={platform}"
    )


def resolve_profile(
    platforms: Mapping[str, Mapping[str, Any]],
    platform: str,
    core: Optional[str],
    *,
    experimental: bool,
    catalog: Catalog,
    tiers: Mapping[str, TierEntry],
) -> Profile:
    """Resolve the profile a launch uses (§6.1).

    Args:
        platforms: The platform table.
        platform: The platform slug, lowercase.
        core: The requested core, or None for the platform's default.
        experimental: Whether a known-broken core may run (§6.4).
        catalog: The current catalog.
        tiers: The tiers table.

    Returns:
        The read-only profile.

    Raises:
        CoreRejectedError: For a core that is not a string, an unmapped
            platform with a core, a blocked core without the opt-in, or a core
            not offered on the platform.
    """
    if core is not None and not isinstance(core, str):
        raise CoreRejectedError(f"core must be a core name, not a {type(core).__name__}")
    entry = platforms.get(platform)
    if entry is None:
        raise CoreRejectedError(
            f"RetroArch has no core for platform {platform!r}; see GET /api/retroarch/cores"
        )
    default_info = catalog.cores.get(entry["core"])
    if core is None or core == entry["core"]:
        base = {k: v for k, v in entry.items() if k != "alternates"}
        name = default_info.display_name if default_info else entry["core"]
        return MappingProxyType({**base, "tier": "default", "display_name": name})
    alternate = entry.get("alternates", {}).get(core)
    if alternate is not None:
        info = catalog.cores.get(core)
        return MappingProxyType({
            **alternate,
            "core": core,
            "extensions": entry["extensions"],
            "tier": "vetted",
            "display_name": info.display_name if info else core,
        })
    info = catalog.cores.get(core)
    extensions = _untested_extensions(entry["extensions"], info) if info else ()
    if not extensions:
        raise CoreRejectedError(
            f"core {core} is not offered on {platform}: "
            f"{_options_detail(platforms, platform, catalog, tiers)}"
        )
    block = _blocking_entry(tiers, core, platform)
    if block is not None and not experimental:
        raise CoreRejectedError(
            f"core {core} is known broken on {platform}: {block.reason}; set "
            f"experimental_cores: true in RomM's config.yml or "
            f"RETROARCH_EXPERIMENTAL_CORES=true on the container to run it "
            f"anyway, or remove core: to use {entry['core']}"
        )
    return MappingProxyType({
        "core": core,
        "library_name": LIBRARY_NAME_FIXES.get(core, info.corename),
        "save_ram": None,
        "extensions": extensions,
        "tier": "blocked" if block is not None else "untested",
        "display_name": info.display_name,
    })


_catalog: Optional[Catalog] = None


def catalog() -> Catalog:
    """The current catalog, loading the bundled one on first use.

    Returns:
        The catalog; a refresh replaces it with `set_catalog`.
    """
    global _catalog
    if _catalog is None:
        _catalog = load_bundled_catalog()
    return _catalog


def set_catalog(new: Catalog) -> None:
    """Swap in a new catalog with one assignment (§7), so no reader sees half of one.

    Args:
        new: The catalog to use from now on.
    """
    global _catalog
    _catalog = new


ZIP_CAP = 5 * 1024 * 1024
"""Largest core-info zip a refresh accepts, 5 MiB (§7)."""
INDEX_CAP = 256 * 1024
"""Largest buildbot index a refresh accepts, 256 KiB (§7)."""
REFRESH_EVERY = 7 * 24 * 3600
"""Seconds between background refreshes, 7 days (§7)."""
CACHE_ZIP = "core_info_cache.zip"
"""File name the refreshed core-info zip is cached under, in `RA_DATA_DIR`."""
CACHE_INDEX = "core_info_cache.index"
"""File name the refreshed buildbot index is cached under, in `RA_DATA_DIR`."""
FETCH_DEADLINE = 120
"""Total seconds a single fetch may take (§7). `timeout=60` below is a per-read
timeout, so without this a server that trickles a byte every 59 seconds would
otherwise never finish."""


def protected_cores(platforms: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """Cores a refresh may never change: every default, every alternate, every tiered core.

    Args:
        platforms: The platform table.

    Returns:
        Their names.
    """
    names = {info["core"] for info in platforms.values()}
    names |= {core for info in platforms.values() for core in info.get("alternates", {})}
    return frozenset(names | set(TIERS))


def merge_catalogs(bundled: Catalog, cache: Catalog, protected: frozenset[str]) -> Catalog:
    """Bundled first, then the cache, never overriding a protected core (§7).

    Args:
        bundled: The catalog from the package.
        cache: The catalog from a refresh.
        protected: Cores the cache may not change.

    Returns:
        The merged catalog.
    """
    cores = dict(bundled.cores)
    for name, info in cache.cores.items():
        if name not in protected:
            cores[name] = info
    return Catalog(MappingProxyType(cores), bundled.info_zip, cache.info_zip, protected)


def installed_cores(cores_dir: Path) -> frozenset[str]:
    """Cores whose `.so` is already in `cores_dir`.

    Args:
        cores_dir: RetroArch's core dir.

    Returns:
        Their names, or an empty set when `cores_dir` cannot be listed.
    """
    try:
        return frozenset(p.name[: -len("_libretro.so")] for p in cores_dir.glob("*_libretro.so"))
    except OSError:
        return frozenset()


def _http_fetch(url: str, cap: int) -> bytes:
    """GET `url`, refusing a body over `cap` bytes or a fetch over `FETCH_DEADLINE`.

    Args:
        url: What to fetch.
        cap: The largest body accepted.

    Returns:
        The body.

    Raises:
        ValueError: When the body is over `cap`, or the fetch runs past `FETCH_DEADLINE`.
        httpx.HTTPError: When the request fails.
    """
    buf = bytearray()
    deadline = time.monotonic() + FETCH_DEADLINE
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > cap:
                raise ValueError(f"{url} is over {cap} bytes")
            if time.monotonic() > deadline:
                raise ValueError(f"{url} took over {FETCH_DEADLINE}s")
    return bytes(buf)


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` through a temp file and a rename.

    Args:
        path: The destination.
        data: The bytes.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def refresh_once(
    cache_dir: Path,
    cores_dir: Path,
    *,
    fetch: Callable[[str, int], bytes],
    platforms: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Fetch, validate, cache and swap in a fresh catalog; keep the current one on any failure.

    Args:
        cache_dir: Where the cache files go (`RA_DATA_DIR`).
        cores_dir: RetroArch's core dir, for sticky removal.
        fetch: `(url, cap) -> bytes`; `_http_fetch` outside tests.
        platforms: The platform table.

    Returns:
        Whether a new catalog was swapped in.
    """
    zip_url = (
        os.environ.get("RETROARCH_CORE_INFO_URL", "").strip()
        or "https://buildbot.libretro.com/assets/frontend/info.zip"
    )
    index_url = (
        os.environ.get("RETROARCH_CORE_INDEX_URL", "").strip()
        or "https://buildbot.libretro.com/nightly/linux/x86_64/latest/.index"
    )
    try:
        zip_bytes = fetch(zip_url, ZIP_CAP)
        index_text = fetch(index_url, INDEX_CAP).decode("utf-8", "replace")
        if not parse_info_zip(zip_bytes):
            raise ValueError("the zip holds no .info members")
    except Exception as exc:  # noqa: BLE001 - a hostile or corrupt zip can raise
        # far more than httpx.HTTPError/OSError/ValueError/zipfile.BadZipFile
        # (zlib.error, RuntimeError for an encrypted member, NotImplementedError,
        # EOFError, ...); §7 says any failure here keeps the current catalog.
        log.warning("retroarch: core info refresh failed, keeping the current catalog: %s", exc)
        return False
    lines = [line for line in index_text.splitlines() if line.strip()]
    dropped = len(lines) - len(parse_index(index_text))
    if dropped:
        log.info(
            "retroarch: core info refresh dropped %d index line(s) that are not valid core names",
            dropped,
        )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Index first: refresh_forever schedules off the zip's mtime, so a
        # failed index write must never leave a fresh zip behind on its own.
        _write_atomic(cache_dir / CACHE_INDEX, index_text.encode())
        _write_atomic(cache_dir / CACHE_ZIP, zip_bytes)
    except OSError as exc:
        log.warning("retroarch: core info refresh could not write its cache: %s", exc)
        return False
    try:
        installed = installed_cores(cores_dir)
        cache = build_catalog(zip_bytes, index_text, installed)
        set_catalog(merge_catalogs(load_bundled_catalog(installed), cache, protected_cores(platforms)))
    except Exception as exc:  # noqa: BLE001 - building the catalog re-parses the
        # same fetched bytes, so the same open-ended set of errors applies; keep
        # the current catalog rather than let one escape.
        log.warning(
            "retroarch: core info refresh could not build the catalog, keeping the current one: %s", exc
        )
        return False
    log.info("retroarch: core info refreshed, %d cores", len(catalog().cores))
    return True


def load_startup_catalog(
    cache_dir: Path, cores_dir: Path, platforms: Mapping[str, Mapping[str, Any]]
) -> None:
    """Load the bundled catalog, merged with a cache from a previous refresh when one is valid.

    Runs once at lifespan start, whether or not `RETROARCH_CORE_INFO_REFRESH` is
    set, so a cache a previous broker process wrote is used immediately instead
    of waiting out a full refresh interval.

    Args:
        cache_dir: Where the cache files are (`RA_DATA_DIR`).
        cores_dir: RetroArch's core dir, for sticky removal.
        platforms: The platform table.
    """
    installed = installed_cores(cores_dir)
    bundled = load_bundled_catalog(installed)
    try:
        zip_bytes = (cache_dir / CACHE_ZIP).read_bytes()
        index_text = (cache_dir / CACHE_INDEX).read_text()
    except FileNotFoundError:
        # No cache yet is the default configuration (a fresh install, or a
        # refresh that has never run): not a failure, so no WARNING.
        log.debug("retroarch: no cached core catalog yet, using the bundled one")
        set_catalog(bundled)
        return
    try:
        if not parse_info_zip(zip_bytes):
            raise ValueError("the cached zip holds no .info members")
        cache = build_catalog(zip_bytes, index_text, installed)
        set_catalog(merge_catalogs(bundled, cache, protected_cores(platforms)))
    except Exception as exc:  # noqa: BLE001 - a corrupt on-disk cache can raise
        # far more than OSError/ValueError/zipfile.BadZipFile (zlib.error,
        # RuntimeError for an encrypted member, NotImplementedError, EOFError,
        # ...); a bad cache must never stop the broker from booting.
        log.warning(
            "retroarch: could not load the cached core catalog, using the bundled one: %s", exc
        )
        set_catalog(bundled)


async def refresh_forever(
    cache_dir: Path, cores_dir: Path, platforms: Mapping[str, Mapping[str, Any]]
) -> None:
    """Refresh at startup when the cache is stale, then every 7 days (§7).

    Args:
        cache_dir: Where the cache files go.
        cores_dir: RetroArch's core dir.
        platforms: The platform table.
    """
    while True:
        try:
            age = time.time() - (cache_dir / CACHE_ZIP).stat().st_mtime
        except OSError:
            age = REFRESH_EVERY
        if age >= REFRESH_EVERY:
            try:
                await anyio.to_thread.run_sync(
                    functools.partial(
                        refresh_once, cache_dir, cores_dir, fetch=_http_fetch, platforms=platforms
                    ),
                    abandon_on_cancel=True,
                )
            except Exception:  # noqa: BLE001 - refresh_once already turns its own
                # known failures into a returned bool; this only guards against
                # something unexpected escaping it, and a refresh must never end
                # the loop for the life of the process (§7).
                log.exception("retroarch: core info refresh crashed, keeping the current catalog")
            age = 0
        await anyio.sleep(REFRESH_EVERY - age)
