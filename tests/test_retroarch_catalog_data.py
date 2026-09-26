"""Offline checks on the bundled RetroArch catalog data (§10.2); no network."""

import hashlib
import json

from webstation_broker.emulators import retroarch_cores as rc

_SOURCE_KEYS = {"url", "index_url", "sha256", "commit", "date", "x86_64_cores"}


def test_source_file_has_exactly_the_known_keys() -> None:
    """Unknown keys are rejected so a typo cannot hide a missing field."""
    assert set(json.loads(rc.BUNDLED_SOURCE.read_text())) == _SOURCE_KEYS


def test_bundled_zip_matches_its_recorded_sha256() -> None:
    """A zip replaced without `--write` is caught here, not at startup in a container."""
    source = json.loads(rc.BUNDLED_SOURCE.read_text())
    assert hashlib.sha256(rc.BUNDLED_ZIP.read_bytes()).hexdigest() == source["sha256"]


def test_bundled_catalog_loads_and_is_not_empty() -> None:
    """The bundle parses and intersects to a real catalog."""
    catalog = rc.load_bundled_catalog()
    assert "snes9x" in catalog.cores
    assert catalog.cores["snes9x"].extensions
