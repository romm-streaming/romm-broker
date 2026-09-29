"""Tests for the per-connection room outbox."""

import asyncio
from typing import Any, Union

import pytest
from starlette.websockets import WebSocketState

from webstation_broker import outbox, session
from webstation_broker.outbox import Outbox


class _Socket:
    """A room socket stand-in that records sends and can be held mid-send.

    Attributes:
        client_state: What the outbox checks before each send.
        events: Every send and close, in order, as `(kind, value)` pairs.
        gate: Cleared to park every send until it is set again.
    """

    def __init__(self) -> None:
        """Build a connected socket that sends straight away."""
        self.client_state = WebSocketState.CONNECTED
        self.events: list[tuple[str, Union[dict[str, Any], bytes, int]]] = []
        self.gate = asyncio.Event()
        self.gate.set()

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Record a JSON send once the gate is open.

        Args:
            payload: The message the outbox sent.
        """
        await self.gate.wait()
        self.events.append(("json", payload))

    async def send_bytes(self, frame: bytes) -> None:
        """Record a media send once the gate is open.

        Args:
            frame: The frame the outbox sent.
        """
        await self.gate.wait()
        self.events.append(("bytes", frame))

    async def close(self, code: int = 1000) -> None:
        """Record a close.

        Args:
            code: The close code the outbox used.
        """
        self.events.append(("close", code))


async def _settle() -> None:
    """Let the drain task run everything it can without a real wait."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_json_goes_out_before_queued_media_and_each_kind_keeps_its_order() -> None:
    """Chat and control never wait behind a media backlog, and neither kind is reordered."""
    sock = _Socket()
    box = Outbox(sock, "viewer")  # type: ignore[arg-type]

    box.send_media(b"f1")
    box.send_media(b"f2")
    box.send_json({"n": 1})
    box.send_json({"n": 2})
    await _settle()

    assert sock.events == [
        ("json", {"n": 1}),
        ("json", {"n": 2}),
        ("bytes", b"f1"),
        ("bytes", b"f2"),
    ]
    await box.aclose()


async def test_a_recipient_that_falls_behind_loses_its_oldest_frames_but_no_json() -> None:
    """Past the media backlog the oldest frame goes; every JSON message still arrives."""
    sock = _Socket()
    sock.gate.clear()
    box = Outbox(sock, "viewer")  # type: ignore[arg-type]
    box.send_media(b"in-flight")
    await _settle()

    frames = [f"f{i}".encode() for i in range(outbox.MAX_MEDIA_BACKLOG + 5)]
    for frame in frames:
        box.send_media(frame)
    box.send_json({"type": "chat_message"})
    sock.gate.set()
    await _settle()

    sent_frames = [value for kind, value in sock.events if kind == "bytes"]
    assert sent_frames == [b"in-flight", *frames[5:]]
    assert box.dropped_frames == 5
    assert ("json", {"type": "chat_message"}) in sock.events
    await box.aclose()


async def test_a_send_that_outlasts_the_stall_limit_closes_the_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recipient that stops reading is closed with 1013 rather than queued for forever."""
    monkeypatch.setattr(outbox, "SEND_STALL_LIMIT", 0.05)
    sock = _Socket()
    sock.gate.clear()
    box = Outbox(sock, "viewer")  # type: ignore[arg-type]

    box.send_json({"n": 1})
    await asyncio.sleep(0.2)

    assert sock.events == [("close", outbox.LAGGARD_CLOSE_CODE)]
    box.send_json({"n": 2})
    sock.gate.set()
    await _settle()
    assert sock.events == [("close", outbox.LAGGARD_CLOSE_CODE)]
    await box.aclose()


async def test_a_json_backlog_past_its_cap_closes_the_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON is never dropped, so a recipient that lets it pile up past the cap is closed instead."""
    monkeypatch.setattr(outbox, "MAX_JSON_BACKLOG", 3)
    sock = _Socket()
    sock.gate.clear()
    box = Outbox(sock, "viewer")  # type: ignore[arg-type]
    box.send_json({"n": 0})
    await _settle()

    for n in range(1, 5):
        box.send_json({"n": n})
    await _settle()

    assert sock.events == [("close", outbox.LAGGARD_CLOSE_CODE)]
    await box.aclose()


async def test_a_failed_send_is_logged_and_the_next_message_still_goes_out() -> None:
    """One send raising does not stop the drain."""
    sock = _Socket()
    calls = 0
    real_send = sock.send_json

    async def flaky(payload: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        await real_send(payload)

    sock.send_json = flaky  # type: ignore[method-assign]
    box = Outbox(sock, "viewer")  # type: ignore[arg-type]

    box.send_json({"n": 1})
    box.send_json({"n": 2})
    await _settle()

    assert sock.events == [("json", {"n": 2})]
    await box.aclose()


async def test_nothing_is_sent_to_a_socket_that_is_no_longer_connected() -> None:
    """A queued message for a socket that has since disconnected is skipped, not sent."""
    sock = _Socket()
    sock.client_state = WebSocketState.DISCONNECTED
    box = Outbox(sock, "viewer")  # type: ignore[arg-type]

    box.send_json({"n": 1})
    box.send_media(b"f")
    await _settle()

    assert sock.events == []
    await box.aclose()


async def test_flush_waits_for_the_queue_and_times_out_on_a_stuck_one() -> None:
    """`flush` reports whether everything queued so far went out in time."""
    sock = _Socket()
    box = Outbox(sock, "viewer")  # type: ignore[arg-type]
    assert await box.flush(0.1)

    box.send_json({"n": 1})
    assert await box.flush(1.0)
    assert sock.events == [("json", {"n": 1})]

    sock.gate.clear()
    box.send_json({"n": 2})
    assert not await box.flush(0.05)
    await box.aclose()


async def test_aclose_stops_the_drain_and_drops_what_was_queued() -> None:
    """After `aclose` nothing more goes out and later sends are ignored."""
    sock = _Socket()
    sock.gate.clear()
    box = Outbox(sock, "viewer")  # type: ignore[arg-type]
    box.send_json({"n": 1})
    box.send_media(b"f")
    await _settle()

    await box.aclose()
    box.send_json({"n": 2})
    sock.gate.set()
    await _settle()

    assert sock.events == []
    assert box._task is None


async def test_session_end_delivers_its_notice_before_closing_each_socket() -> None:
    """`session_ended` reaches every member before their socket closes under it."""
    host, guest = _Socket(), _Socket()
    boxes = [Outbox(host, "host"), Outbox(guest, "guest")]  # type: ignore[arg-type]
    session.ROOM["controller"] = {"websocket": host, "outbox": boxes[0]}
    session.ROOM["viewers"] = {"g": {"websocket": guest, "outbox": boxes[1]}}

    await session.notify_session_ended()

    for sock in (host, guest):
        assert sock.events == [("json", {"type": "session_ended"}), ("close", 1000)]
    for box in boxes:
        await box.aclose()
