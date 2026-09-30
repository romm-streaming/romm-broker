#!/usr/bin/env python3
"""Keep the bundled libretro core-info catalog in step with upstream (dev only).

--write   download info.zip and the x86_64 .index, write the bundled zip and its source file
--check   compare the bundle and the platform table with live upstream
--probe   load every default and vetted core and compare library_name
--docs    regenerate the tier table in retroarch-cores.mdx
--tiers   print every catalog core per platform with its tier
"""

import argparse
import ctypes
import datetime
import hashlib
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webstation_broker.emulators import retroarch  # noqa: E402
from webstation_broker.emulators import retroarch_cores as rc  # noqa: E402

INFO_URL = "https://buildbot.libretro.com/assets/frontend/info.zip"
INDEX_URL = "https://buildbot.libretro.com/nightly/linux/x86_64/latest/.index"
COMMITS_URL = "https://api.github.com/repos/libretro/libretro-core-info/commits/master"

DOCS_PATH = ROOT / "docs/content/docs/emulators/retroarch-cores.mdx"
"""Where `docs()` rewrites the tier table."""
DOCS_START = "{/* core-tiers:start */}"
DOCS_END = "{/* core-tiers:end */}"


def fetch(url: str) -> bytes:
    """Download `url` under the same size cap and deadline the broker's refresh uses.

    Args:
        url: What to fetch.

    Returns:
        The body.
    """
    return rc._http_fetch(url, rc.ZIP_CAP)


def write() -> int:
    """Download upstream and rewrite the bundled zip and source file.

    Returns:
        The process exit code.
    """
    data = fetch(INFO_URL)
    index = fetch(INDEX_URL).decode()
    commit = json.loads(fetch(COMMITS_URL))["sha"]
    rc.parse_info_zip(data)  # raises on a bad zip before anything is written
    rc.BUNDLED_ZIP.write_bytes(data)
    source = {
        "url": INFO_URL,
        "index_url": INDEX_URL,
        "sha256": hashlib.sha256(data).hexdigest(),
        "commit": commit,
        "date": datetime.date.today().isoformat(),
        "x86_64_cores": sorted(rc.parse_index(index)),
    }
    rc.BUNDLED_SOURCE.write_text(json.dumps(source, indent=2) + "\n")
    print(f"wrote {rc.BUNDLED_ZIP.name} ({len(data)} bytes, {len(source['x86_64_cores'])} x86_64 cores)")
    return 0


def render_tier_table(
    platforms: Mapping[str, Mapping[str, Any]], catalog: rc.Catalog, tiers: Mapping[str, rc.TierEntry]
) -> str:
    """The per-platform tier table for retroarch-cores.mdx.

    Args:
        platforms: The platform table.
        catalog: The bundled catalog.
        tiers: The tiers table.

    Returns:
        A Markdown table: platform, default (tagged `(untested)` on a flagged
        platform), vetted, blocked, count of untested.
    """
    lines = ["| Platform | Default (best tested) | Vetted | Blocked | Untested |", "|---|---|---|---|---|"]
    for slug in sorted(platforms):
        rows = rc.cores_for_platform(platforms, slug, catalog, tiers)
        by = {
            t: [r["core"] for r in rows if r["tier"] == t]
            for t in ("default", "vetted", "blocked", "untested")
        }
        default = f"`{by['default'][0]}`" + (" (untested)" if platforms[slug].get("untested") else "")
        lines.append(
            f"| `{slug}` | {default} | {', '.join(f'`{c}`' for c in by['vetted']) or '-'} "
            f"| {', '.join(f'`{c}`' for c in by['blocked']) or '-'} | {len(by['untested'])} |"
        )
    return "\n".join(lines)


def docs() -> int:
    """Rewrite the tier table between the markers in retroarch-cores.mdx.

    Returns:
        The process exit code.
    """
    text = DOCS_PATH.read_text()
    head, rest = text.split(DOCS_START, 1)
    _, tail = rest.split(DOCS_END, 1)
    table = render_tier_table(retroarch.PLATFORMS, rc.load_bundled_catalog(), rc.TIERS)
    DOCS_PATH.write_text(f"{head}{DOCS_START}\n\n{table}\n\n{DOCS_END}{tail}")
    return 0


def tiers(platform: Optional[str]) -> int:
    """Print every catalog core per platform with its tier.

    Args:
        platform: One slug, or None for all.

    Returns:
        The process exit code.
    """
    cat = rc.load_bundled_catalog()
    for slug in [platform] if platform else sorted(retroarch.PLATFORMS):
        for row in rc.cores_for_platform(retroarch.PLATFORMS, slug, cat, rc.TIERS):
            print(f"{slug}\t{row['tier']}\t{row['core']}\t{row['display_name']}")
    return 0


def check() -> int:
    """Compare the bundle and table with live upstream.

    Returns:
        1 when a default or vetted core is renamed, removed or not built for
        x86_64 (core_source cores skipped), or the bundle is stale; else 0.
        Extension drift is printed only.
    """
    live_zip, live_index = fetch(INFO_URL), fetch(INDEX_URL).decode()
    live = rc.build_catalog(live_zip, live_index)
    bundled = rc.load_bundled_catalog()
    failed = False
    for slug, info in retroarch.PLATFORMS.items():
        cores = [] if "core_source" in info else [info["core"]]
        cores += list(info.get("alternates", {}))
        for core in cores:
            if core not in live.cores:
                print(f"FAIL {slug}: {core} is gone from upstream or not built for x86_64")
                failed = True
            elif live.cores[core].corename != bundled.cores[core].corename:
                print(
                    f"FAIL {slug}: {core} corename "
                    f"{bundled.cores[core].corename} -> {live.cores[core].corename}"
                )
                failed = True
            elif live.cores[core].extensions != bundled.cores[core].extensions:
                print(f"note {slug}: {core} extensions changed upstream")
    if hashlib.sha256(live_zip).hexdigest() != json.loads(rc.BUNDLED_SOURCE.read_text())["sha256"]:
        print("FAIL the bundled info.zip is stale; run --write")
        failed = True
    return 1 if failed else 0


class _SystemInfo(ctypes.Structure):
    """The libretro `retro_system_info` struct, only the fields `probe()` reads."""

    _fields_ = [
        ("library_name", ctypes.c_char_p),
        ("library_version", ctypes.c_char_p),
        ("valid_extensions", ctypes.c_char_p),
        ("need_fullpath", ctypes.c_bool),
        ("block_extract", ctypes.c_bool),
    ]


def probe() -> int:
    """Load every default and vetted core and compare its library_name (dev container only).

    This downloads and runs native code. Run it only in the dev container.

    Returns:
        1 on any mismatch, else 0.
    """
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        for slug, info in retroarch.PLATFORMS.items():
            pairs = [(info["core"], info["library_name"], info.get("core_source"))]
            pairs += [
                (c, a["library_name"], a.get("core_source")) for c, a in info.get("alternates", {}).items()
            ]
            for core, expected, source in pairs:
                so = Path(tmp) / f"{core}_libretro.so"
                if not so.exists():
                    so.write_bytes(retroarch._download_core_bytes(core, retroarch._core_url(core, source)))
                lib = ctypes.CDLL(str(so))
                sysinfo = _SystemInfo()
                lib.retro_get_system_info(ctypes.byref(sysinfo))
                got = sysinfo.library_name.decode()
                status = "ok  " if got == expected else "FAIL"
                failed |= got != expected
                print(f"{status} {slug}/{core}: table {expected!r}, core reports {got!r}")
    return 1 if failed else 0


def main() -> int:
    """Parse arguments and run one mode.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--docs", action="store_true")
    mode.add_argument("--tiers", action="store_true")
    parser.add_argument("--platform", default=None, help="With --tiers, limit to one platform slug.")
    args = parser.parse_args()
    if args.platform and not args.tiers:
        parser.error("--platform only applies to --tiers")
    if args.write:
        return write()
    if args.check:
        return check()
    if args.probe:
        return probe()
    if args.docs:
        return docs()
    if args.tiers:
        return tiers(args.platform)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
