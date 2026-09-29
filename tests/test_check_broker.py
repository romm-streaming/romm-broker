"""Offline checks on scripts/check_broker.py's thresholds; no broker is contacted."""

import importlib.util
import types
import urllib.error
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
    return check.Limits(
        max_session_hours=12.0, max_exit_seconds=120.0, max_kept_archives=0, max_last_exit_hours=24.0
    )


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
        ({"upload": "failed", "archive_path": "/config/broker-exports/gone.zip"}, None),
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
    exports = [{"name": "s1.zip", "size": 10, "mtime": NOW}]
    limits = _limits(check)._replace(max_kept_archives=1)

    problems = check.evaluate({"active": False, "last_exit": _exit(**last_exit)}, exports, NOW, limits)

    assert (check._LABELS[problems[0][0]] if problems else None) == expected


@pytest.mark.parametrize(
    "last_exit",
    [
        {"dump_error": "input/output error", "upload": "failed"},
        {"upload": "failed", "archive_path": None},
        {"duration_s": 300.0},
    ],
)
def test_last_exit_alerts_with_nothing_on_disk_expire(
    check: types.ModuleType, last_exit: dict[str, Any]
) -> None:
    """Alerts the exports listing cannot clear drop once the exit is older than the window.

    Args:
        check: The loaded check module.
        last_exit: Fields to set on the last exit.
    """
    status = {"active": False, "last_exit": _exit(ended_at=NOW - 25 * 3600, **last_exit)}

    assert check.evaluate(status, [], NOW, _limits(check)) == []


def test_a_kept_archive_alert_outlives_the_window_until_the_archive_is_gone(check: types.ModuleType) -> None:
    """A failed upload's archive still on disk stays a warning however old the exit is."""
    last = _exit(ended_at=NOW - 72 * 3600, upload="failed", archive_path="/config/broker-exports/s1.zip")
    exports = [{"name": "s1.zip", "size": 10, "mtime": NOW}]
    limits = _limits(check)._replace(max_kept_archives=1)

    problems = check.evaluate({"active": False, "last_exit": last}, exports, NOW, limits)

    assert [s for s, _ in problems] == [check.WARNING]


def test_kept_archives_over_the_limit_warn(check: types.ModuleType) -> None:
    """Any archive sitting in EXPORT_DIR warns at the default limit of zero."""
    exports = [{"name": "s1-1.zip", "size": 10, "mtime": NOW}]

    problems = check.evaluate({"active": False, "last_exit": None}, exports, NOW, _limits(check))

    assert [s for s, _ in problems] == [check.WARNING]


def _raise(exc: Exception) -> Any:
    """A `_get` stand-in that fails every request with `exc`.

    Args:
        exc: The exception to raise.

    Returns:
        A function with `_get`'s signature.
    """

    def fake_get(url: str, *_args: Any) -> Any:
        if isinstance(exc, urllib.error.HTTPError) and url.endswith("/health"):
            return {"status": "ok"}
        raise exc

    return fake_get


def test_an_unreachable_broker_is_critical(
    check: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broker that refuses the connection is CRITICAL, not UNKNOWN.

    Args:
        check: The loaded check module.
        monkeypatch: The pytest monkeypatch fixture.
        capsys: The pytest stdout capture fixture.
    """
    monkeypatch.setattr(check, "_get", _raise(urllib.error.URLError("connection refused")))

    assert check.main(["--url", "http://broker.invalid"]) == check.CRITICAL
    assert capsys.readouterr().out.startswith("CRITICAL: broker health")


@pytest.mark.parametrize(
    ("code", "blames_secret"),
    [(401, True), (403, True), (404, False), (500, False), (502, False)],
)
def test_only_a_refused_secret_is_blamed_on_the_secret(
    check: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
    blames_secret: bool,
) -> None:
    """A 404 or 5xx on a secret route is reported as itself, not as a wrong secret.

    Args:
        check: The loaded check module.
        monkeypatch: The pytest monkeypatch fixture.
        capsys: The pytest stdout capture fixture.
        code: The HTTP status the status route answers with.
        blames_secret: Whether the message should point at BROKER_SECRET.
    """
    url = "http://broker.invalid/api/session/status"
    monkeypatch.setattr(check, "_get", _raise(urllib.error.HTTPError(url, code, "x", {}, None)))  # type: ignore[arg-type]

    assert check.main(["--url", "http://broker.invalid"]) == check.UNKNOWN
    out = capsys.readouterr().out
    assert f"HTTP {code}" in out
    assert ("BROKER_SECRET" in out) is blames_secret


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
    for name in ("BROKER_URL", "BROKER_HOST", "BROKER_PORT"):
        monkeypatch.delenv(name, raising=False)
    if subfolder is None:
        monkeypatch.delenv("SUBFOLDER", raising=False)
    else:
        monkeypatch.setenv("SUBFOLDER", subfolder)

    assert check._default_url() == expected


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        (None, "9000", "http://127.0.0.1:9000/streaming"),
        ("0.0.0.0", None, "http://127.0.0.1:8000/streaming"),
        ("10.0.0.5", "8100", "http://10.0.0.5:8100/streaming"),
        ("::1", None, "http://[::1]:8000/streaming"),
    ],
)
def test_the_default_url_follows_the_brokers_bind_address(
    check: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    host: Optional[str],
    port: Optional[str],
    expected: str,
) -> None:
    """A container that moves the broker off 127.0.0.1:8000 is still found without `--url`.

    Args:
        check: The loaded check module.
        monkeypatch: The pytest monkeypatch fixture.
        host: The container's `BROKER_HOST`, or None when unset.
        port: The container's `BROKER_PORT`, or None when unset.
        expected: The URL the check should default to.
    """
    for name in ("BROKER_URL", "SUBFOLDER", "BROKER_HOST", "BROKER_PORT"):
        monkeypatch.delenv(name, raising=False)
    if host is not None:
        monkeypatch.setenv("BROKER_HOST", host)
    if port is not None:
        monkeypatch.setenv("BROKER_PORT", port)

    assert check._default_url() == expected
