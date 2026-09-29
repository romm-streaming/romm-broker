"""Offline checks on scripts/check_broker.py's thresholds; no broker is contacted."""

import importlib.util
import types
from pathlib import Path
from typing import Any, Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def check() -> types.ModuleType:
    """Load scripts/check_broker.py as a module.

    Returns:
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location("check_broker", _ROOT / "scripts" / "check_broker.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _limits(check: types.ModuleType) -> Any:
    """The script's default thresholds.

    Args:
        check: The loaded check module.

    Returns:
        A `Limits` carrying the defaults `main` applies.
    """
    return check.Limits(max_session_hours=12.0, max_exit_seconds=120.0, max_kept_archives=0)


def _exit(**overrides: Any) -> dict[str, Any]:
    """A clean `last_exit` block with the given fields replaced.

    Args:
        **overrides: Fields to set on the block.

    Returns:
        The block.
    """
    block = {
        "session_id": "s1",
        "ended_at": NOW,
        "duration_s": 12.0,
        "state_saved": True,
        "dump_error": None,
        "upload": "uploaded",
        "archive_path": None,
    }
    block.update(overrides)
    return block


def test_an_idle_broker_with_a_clean_last_exit_is_healthy(check: types.ModuleType) -> None:
    """No session, a clean last exit and no kept archives report nothing."""
    assert check.evaluate({"active": False, "last_exit": _exit()}, [], NOW, _limits(check)) == []


def test_a_session_whose_emulator_died_is_critical(check: types.ModuleType) -> None:
    """An active session with no emulator behind it is the worst state the check reports."""
    status = {"active": True, "session_id": "s2", "emulator": "pcsx2", "emulator_alive": False}

    assert [s for s, _ in check.evaluate(status, [], NOW, _limits(check))] == [check.CRITICAL]


def test_a_session_left_running_past_the_limit_warns(check: types.ModuleType) -> None:
    """A session older than the limit is flagged as probably abandoned."""
    status = {"active": True, "session_id": "s3", "emulator_alive": True, "started_at": NOW - 13 * 3600}

    problems = check.evaluate(status, [], NOW, _limits(check))

    assert [s for s, _ in problems] == [check.WARNING]
    assert "13.0h" in problems[0][1]


@pytest.mark.parametrize(
    ("last_exit", "expected"),
    [
        ({"dump_error": "input/output error", "upload": "failed"}, "CRITICAL"),
        ({"upload": "failed", "archive_path": None}, "CRITICAL"),
        ({"upload": "failed", "archive_path": "/config/broker-exports/s1.zip"}, "WARNING"),
        ({"duration_s": 300.0}, "WARNING"),
        ({"upload": "skipped"}, None),
    ],
)
def test_the_last_exit_is_judged_by_where_its_save_data_ended_up(
    check: types.ModuleType, last_exit: dict[str, Any], expected: str
) -> None:
    """Save data that never left the container outranks save data kept on disk.

    Args:
        check: The loaded check module.
        last_exit: Fields to set on the last exit.
        expected: The state label expected first, or None for healthy.
    """
    problems = check.evaluate({"active": False, "last_exit": _exit(**last_exit)}, [], NOW, _limits(check))

    assert (check._LABELS[problems[0][0]] if problems else None) == expected


def test_kept_archives_over_the_limit_warn(check: types.ModuleType) -> None:
    """Any archive sitting in EXPORT_DIR warns at the default limit of zero."""
    exports = [{"name": "s1-1.zip", "size": 10, "mtime": NOW}]

    problems = check.evaluate({"active": False, "last_exit": None}, exports, NOW, _limits(check))

    assert [s for s, _ in problems] == [check.WARNING]


def test_an_unreachable_broker_is_critical(
    check: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broker that refuses the connection is CRITICAL, not UNKNOWN.

    Args:
        check: The loaded check module.
        capsys: The pytest stdout capture fixture.
    """
    assert check.main(["--url", "http://127.0.0.1:9", "--timeout", "1"]) == check.CRITICAL
    assert capsys.readouterr().out.startswith("CRITICAL: broker health")


@pytest.mark.parametrize(
    ("subfolder", "expected"),
    [
        (None, "http://127.0.0.1:8000/streaming"),
        ("/games/", "http://127.0.0.1:8000/games"),
        ("games", "http://127.0.0.1:8000/games"),
        ("/", "http://127.0.0.1:8000"),
    ],
)
def test_the_default_url_follows_the_containers_subfolder(
    check: types.ModuleType, monkeypatch: pytest.MonkeyPatch, subfolder: Optional[str], expected: str
) -> None:
    """Run through `docker exec`, the check finds the broker under whatever prefix the container uses.

    Args:
        check: The loaded check module.
        monkeypatch: The pytest monkeypatch fixture.
        subfolder: The container's `SUBFOLDER`, or None when unset.
        expected: The URL the check should default to.
    """
    monkeypatch.delenv("BROKER_URL", raising=False)
    if subfolder is None:
        monkeypatch.delenv("SUBFOLDER", raising=False)
    else:
        monkeypatch.setenv("SUBFOLDER", subfolder)

    assert check._default_url() == expected
