#!/usr/bin/env python3
"""Keep the bundled libretro core-info catalog in step with upstream (dev only, stdlib only).

--write   download info.zip and the x86_64 .index, write the bundled zip and its source file
--check   compare the bundle and the platform table with live upstream (Task 13)
--probe   load every default and vetted core and compare library_name (Task 13)
--docs    regenerate the tier table in retroarch-cores.mdx (Task 13)
--tiers   print every catalog core per platform with its tier (Task 13)
"""

import argparse
import datetime
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webstation_broker.emulators import retroarch_cores as rc  # noqa: E402

INFO_URL = "https://buildbot.libretro.com/assets/frontend/info.zip"
INDEX_URL = "https://buildbot.libretro.com/nightly/linux/x86_64/latest/.index"
COMMITS_URL = "https://api.github.com/repos/libretro/libretro-core-info/commits/master"


def fetch(url: str) -> bytes:
    """Download `url`.

    Args:
        url: What to fetch.

    Returns:
        The body.
    """
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed https URLs
        return resp.read()


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
    args = parser.parse_args()
    if args.write:
        return write()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
