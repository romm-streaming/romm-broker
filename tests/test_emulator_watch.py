"""The emulator watch: one ERROR line when a session's emulator exits on its own, and nothing else."""

import logging
from typing import Any, Optional

import anyio
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


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The ERROR messages the watch logged.

    Args:
        caplog: The pytest log capture fixture.

    Returns:
        Each ERROR record's message, in order.
    """
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


def test_a_dead_emulator_is_logged_once_per_session(caplog: pytest.LogCaptureFixture) -> None:
    """A crash is one ERROR naming the session, emulator and rom, however many checks see it."""
    _activate().running = False

    reported = api.check_emulator_alive(None)
    reported = api.check_emulator_alive(reported)

    assert reported == "sess-1"
    [line] = _errors(caplog)
    assert "session sess-1: fake exited" in line
    assert "rom Game" in line


def test_the_next_session_is_watched_afresh(caplog: pytest.LogCaptureFixture) -> None:
    """Having reported one session's crash does not silence the next session's."""
    _activate("sess-1").running = False
    reported = api.check_emulator_alive(None)
    _activate("sess-2").running = False

    assert api.check_emulator_alive(reported) == "sess-2"
    assert len(_errors(caplog)) == 2


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
    assert _errors(caplog) == []


def test_a_session_operation_in_flight_is_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    """An exit stops the emulator on purpose, so the watch stays quiet while one holds the lock."""
    _activate().running = False

    with api._session_operation("exit"):
        assert api.check_emulator_alive(None) is None
    assert _errors(caplog) == []


def test_exit_code_reports_how_the_process_ended() -> None:
    """The exit code is the process's own, and None before anything was spawned."""
    emulator = FakeEmulator()
    assert emulator.exit_code is None

    class Finished:
        """A spawned process that died of a segfault."""

        returncode = -11

    emulator._proc = Finished()

    assert emulator.exit_code == -11


async def test_the_watch_keeps_going_after_a_failed_check(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One check raising is logged, and the loop carries on checking."""
    calls: list[Any] = []

    def flaky(reported: Optional[str]) -> Optional[str]:
        calls.append(reported)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return reported

    monkeypatch.setattr(api, "check_emulator_alive", flaky)

    with anyio.move_on_after(0.2):
        await api.watch_emulator_forever(0.01)

    assert len(calls) > 1
    assert "emulator watch: check failed" in _errors(caplog)
