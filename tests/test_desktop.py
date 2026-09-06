"""Desktop launch: the spawned binary and the failure-logging path."""

import logging

import pytest

from webstation_broker.emulators import desktop


def test_launch_spawns_configured_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch spawns whatever DESKTOP_BIN names."""
    calls = []
    monkeypatch.setenv("DESKTOP_BIN", "custom-desktop")
    monkeypatch.setattr(desktop.Desktop, "_spawn", lambda self, cmd, env: calls.append(cmd))
    monkeypatch.setattr(desktop.Desktop, "stop", lambda self: None)

    desktop.Desktop().launch(None, None)

    assert calls == [["custom-desktop"]]


def test_launch_defaults_to_selkies_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch falls back to selkies-desktop when DESKTOP_BIN is unset."""
    calls = []
    monkeypatch.delenv("DESKTOP_BIN", raising=False)
    monkeypatch.setattr(desktop.Desktop, "_spawn", lambda self, cmd, env: calls.append(cmd))
    monkeypatch.setattr(desktop.Desktop, "stop", lambda self: None)

    desktop.Desktop().launch(None, None)

    assert calls == [["selkies-desktop"]]


def test_launch_failure_is_logged_and_reraised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A spawn failure is logged and still propagates, so it does not vanish silently."""

    def _explode(self: desktop.Desktop, cmd: list[str], env: dict[str, str]) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr(desktop.Desktop, "_spawn", _explode)
    monkeypatch.setattr(desktop.Desktop, "stop", lambda self: None)

    with caplog.at_level(logging.ERROR, logger="webstation_broker.emulators.desktop"):
        with pytest.raises(OSError):
            desktop.Desktop().launch(None, None)

    assert any("failed to launch" in r.getMessage() for r in caplog.records)


def test_launch_stops_any_running_session_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch replaces whatever is already running before spawning a new one."""
    order = []
    monkeypatch.setattr(desktop.Desktop, "stop", lambda self: order.append("stop"))
    monkeypatch.setattr(
        desktop.Desktop, "_spawn", lambda self, cmd, env: order.append("spawn")
    )

    desktop.Desktop().launch(None, None)

    assert order == ["stop", "spawn"]
