"""The emulator watch: one ERROR line when a session's emulator exits on its own, and nothing else."""

import logging
from typing import Any, Optional

import pytest

from webstation_broker import api, session

from .conftest import FakeEmulator


def _activate(session_id: str = "sess-1") -> FakeEmulator:
    """Start a session on a FakeEmulator.

    Args:
        session_id: The id RomM hands over.

    Returns:
        The emulator the session holds.
    """
    emulator = FakeEmulator()
    session.new_session(
        {"session_id": session_id, "emulator": "fake", "rom": {"name": "Game"}},
        emulator,
        "/romm/roms/ps2/Game.iso",
    )
    return emulator


class _Exited:
    """A spawned process that has exited with `returncode`."""

    def __init__(self, returncode: int) -> None:
        """Record the exit code.

        Args:
            returncode: The code `subprocess` would report.
        """
        self.returncode = returncode


class _StopWatch(BaseException):
    """Ends the watch loop from a stubbed check; the loop only catches `Exception`."""


def _logged(caplog: pytest.LogCaptureFixture, level: int = logging.ERROR) -> list[str]:
    """The messages the watch logged at one level.

    Args:
        caplog: The pytest log capture fixture.
        level: The level to collect.

    Returns:
        Each matching record's message, in order.
    """
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_a_dead_emulator_is_logged_once_per_session(caplog: pytest.LogCaptureFixture) -> None:
    """A crash is one ERROR naming the session, emulator and rom, however many checks see it."""
    emulator = _activate()
    emulator.running = False
    emulator._proc = _Exited(-11)

    reported = api.check_emulator_alive(None)
    reported = api.check_emulator_alive(reported)

    assert reported == "sess-1"
    [line] = _logged(caplog)
    assert "session sess-1: fake exited" in line
    assert "rom Game" in line
    assert "exit code -11" in line


def test_a_clean_quit_is_a_warning_not_an_error(caplog: pytest.LogCaptureFixture) -> None:
    """Exit code 0 is the player quitting from the emulator's own menu, not a crash."""
    emulator = _activate()
    emulator.running = False
    emulator._proc = _Exited(0)

    api.check_emulator_alive(None)

    assert _logged(caplog) == []
    [line] = _logged(caplog, logging.WARNING)
    assert "exit code 0" in line


def test_the_next_session_is_watched_afresh(caplog: pytest.LogCaptureFixture) -> None:
    """Having reported one session's crash does not silence the next session's."""
    _activate("sess-1").running = False
    reported = api.check_emulator_alive(None)
    _activate("sess-2").running = False

    assert api.check_emulator_alive(reported) == "sess-2"
    assert len(_logged(caplog)) == 2


@pytest.mark.parametrize("running", [True, None])
def test_a_running_emulator_or_no_session_logs_nothing(
    caplog: pytest.LogCaptureFixture, running: Optional[bool]
) -> None:
    """Nothing is logged while the emulator runs, or when there is no session to watch.

    Args:
        caplog: The pytest log capture fixture.
        running: The emulator's running flag, or None for no session at all.
    """
    if running is not None:
        _activate().running = running

    assert api.check_emulator_alive(None) is None
    assert _logged(caplog) == []


def test_a_session_operation_in_flight_is_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    """An exit stops the emulator on purpose, so the watch stays quiet while one holds the lock."""
    _activate().running = False

    with api._session_operation("exit"):
        assert api.check_emulator_alive(None) is None
    assert _logged(caplog) == []


def test_exit_code_reports_how_the_process_ended() -> None:
    """The exit code is the process's own, and None before anything was spawned."""
    emulator = FakeEmulator()
    assert emulator.exit_code is None

    emulator._proc = _Exited(-11)

    assert emulator.exit_code == -11


async def test_a_failing_check_is_logged_once_until_it_recovers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Two failures in a row are one ERROR; the next good check logs the recovery.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        caplog: The pytest log capture fixture.
    """
    outcomes: list[Any] = [RuntimeError("boom"), RuntimeError("boom"), None, _StopWatch()]
    calls: list[Optional[str]] = []

    def scripted(reported: Optional[str]) -> Optional[str]:
        calls.append(reported)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return reported

    monkeypatch.setattr(api, "check_emulator_alive", scripted)
    caplog.set_level(logging.INFO)

    with pytest.raises(_StopWatch):
        await api.watch_emulator_forever(0)

    assert len(calls) == 4
    assert _logged(caplog) == ["emulator watch: check failed"]
    assert "emulator watch: check recovered" in _logged(caplog, logging.INFO)
