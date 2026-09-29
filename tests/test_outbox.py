"""Tests for the per-connection room outbox."""

import asyncio
import logging
from typing import Any, Callable, Optional, Union, cast

import pytest
from starlette.websockets import WebSocket, WebSocketState

from webstation_broker import outbox, session
from webstation_broker.outbox import Outbox


class _Socket:
    """A room socket stand-in that records sends and can be held mid-send.

    Attributes:
        client_state: The client's side of the connection, flipped by a test to disconnect it.
        application_state: The server's side, which `close` moves as starlette's does.
        events: Every send and close, in order, as `(kind, value)` pairs.
        gate: Cleared to park every send until it is set again.
    """

    def __init__(self) -> None:
        """Build a connected socket that sends straight away."""
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
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
        self.application_state = WebSocketState.DISCONNECTED
        self.events.append(("close", code))


def _outbox(
    label: str = "viewer",
    held: bool = False,
    on_video_gap: Optional[Callable[[bytes], None]] = None,
) -> tuple[_Socket, Outbox]:
    """Build an outbox over a recording socket.

    Args:
        label: The member name the outbox logs under.
        held: Park every send until the socket's gate is set.
        on_video_gap: Passed through to the outbox.

    Returns:
        The socket and the outbox wrapping it.
    """
    sock = _Socket()
    if held:
        sock.gate.clear()
    return sock, Outbox(cast(WebSocket, sock), label, on_video_gap=on_video_gap)


async def _settle() -> None:
    """Give the drain task a few loop turns, for checks that nothing (more) went out.

    Anything expected to be sent is waited for with `Outbox.flush` instead: how
    many turns one send takes differs between Python versions.
    """
    for _ in range(20):
        await asyncio.sleep(0)


def _video(sender: bytes, body: bytes, key: bool = False) -> bytes:
    """Build a stamped video frame.

    Args:
        sender: The 8-byte media id of the member it came from.
        body: The encoded payload.
        key: Whether it is a keyframe.

    Returns:
        The frame in the room's binary wire format.
    """
    return sender + bytes([outbox.VIDEO_FRAME, outbox.KEYFRAME_FLAG if key else 0]) + body


def _audio(sender: bytes, body: bytes) -> bytes:
    """Build a stamped audio frame.

    Args:
        sender: The 8-byte media id of the member it came from.
        body: The encoded payload.

    Returns:
        The frame in the room's binary wire format.
    """
    return sender + bytes([outbox.AUDIO_FRAME, 0]) + body


def _config(sender: bytes) -> bytes:
    """Build a stamped video config message.

    Args:
        sender: The 8-byte media id of the member it came from.

    Returns:
        The message in the room's binary wire format.
    """
    return sender + bytes([outbox.VIDEO_CONFIG]) + b"cfg"


_A = b"AAAAAAAA"
_B = b"BBBBBBBB"


def _sent_frames(sock: _Socket) -> list[bytes]:
    """List the media frames a socket was sent, in order.

    Args:
        sock: The recording socket.

    Returns:
        Every binary payload it received.
    """
    return [value for _, value in sock.events if isinstance(value, bytes)]


async def test_json_then_audio_then_video_and_each_lane_keeps_its_order() -> None:
    """Chat and control never wait behind media, audio never waits behind video."""
    sock, box = _outbox()

    box.send_media(_video(_A, b"v1", key=True))
    box.send_media(_audio(_A, b"a1"))
    box.send_media(_video(_A, b"v2"))
    box.send_media(_audio(_A, b"a2"))
    box.send_json({"n": 1})
    box.send_json({"n": 2})
    assert await box.flush(1.0)

    assert sock.events == [
        ("json", {"n": 1}),
        ("json", {"n": 2}),
        ("bytes", _audio(_A, b"a1")),
        ("bytes", _audio(_A, b"a2")),
        ("bytes", _video(_A, b"v1", key=True)),
        ("bytes", _video(_A, b"v2")),
    ]
    await box.aclose()


async def test_a_brief_hiccup_drops_nothing_however_much_queued_up() -> None:
    """Frames younger than `MAX_MEDIA_AGE` all go out once the recipient catches up."""
    sock, box = _outbox(held=True)
    frames = [_video(_A, b"k", key=True)] + [_video(_A, f"d{i}".encode()) for i in range(300)]
    for frame in frames:
        box.send_media(frame)
    await _settle()

    sock.gate.set()
    assert await box.flush(1.0)

    assert _sent_frames(sock) == frames
    assert box.dropped_video == 0
    await box.aclose()


async def test_stale_video_is_skipped_until_its_senders_next_keyframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a dropped frame the sender's deltas are undecodable, so they wait on a keyframe.

    The other sender's chain is untouched, and the gap is reported once so the
    sender can be asked for a keyframe.
    """
    monkeypatch.setattr(outbox, "MAX_MEDIA_AGE", 0.05)
    gaps: list[bytes] = []
    sock, box = _outbox(held=True, on_video_gap=gaps.append)
    box.send_media(_video(_A, b"in-flight", key=True))
    await _settle()
    box.send_media(_video(_A, b"stale"))
    await asyncio.sleep(0.1)

    box.send_media(_video(_A, b"after-gap"))
    box.send_media(_video(_B, b"b-fresh"))
    box.send_media(_video(_A, b"still-broken"))
    box.send_media(_video(_A, b"fresh-key", key=True))
    box.send_media(_video(_A, b"decodable"))
    sock.gate.set()
    assert await box.flush(1.0)

    assert _sent_frames(sock) == [
        _video(_A, b"in-flight", key=True),
        _video(_B, b"b-fresh"),
        _video(_A, b"fresh-key", key=True),
        _video(_A, b"decodable"),
    ]
    assert gaps == [_A, _A, _A]
    assert box.dropped_video == 3
    await box.aclose()


async def test_a_keyframe_that_arrives_stale_mid_gap_asks_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the requested keyframe too must not leave the tile frozen until the scheduled one."""
    monkeypatch.setattr(outbox, "MAX_MEDIA_AGE", 0.05)
    gaps: list[bytes] = []
    sock, box = _outbox(held=True, on_video_gap=gaps.append)
    box.send_media(_video(_A, b"in-flight", key=True))
    await _settle()
    box.send_media(_video(_A, b"stale"))
    box.send_media(_video(_A, b"stale-key", key=True))
    await asyncio.sleep(0.1)

    sock.gate.set()
    assert await box.flush(1.0)

    assert _sent_frames(sock) == [_video(_A, b"in-flight", key=True)]
    assert gaps == [_A, _A]
    await box.aclose()


async def test_audio_keeps_flowing_while_video_absorbs_the_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale backlog loses its video; fresh audio queued behind it still goes out first."""
    monkeypatch.setattr(outbox, "MAX_MEDIA_AGE", 0.05)
    sock, box = _outbox(held=True)
    box.send_media(_audio(_A, b"in-flight"))
    await _settle()
    box.send_media(_video(_A, b"old", key=True))
    box.send_media(_audio(_A, b"old"))
    await asyncio.sleep(0.1)

    box.send_media(_audio(_A, b"fresh"))
    sock.gate.set()
    assert await box.flush(1.0)

    assert _sent_frames(sock) == [_audio(_A, b"in-flight"), _audio(_A, b"fresh")]
    assert (box.dropped_audio, box.dropped_video) == (1, 1)
    await box.aclose()


async def test_a_video_config_is_never_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A decoder config goes out however stale, even mid-gap: without it nothing decodes."""
    monkeypatch.setattr(outbox, "MAX_MEDIA_AGE", 0.05)
    sock, box = _outbox(held=True)
    box.send_media(_audio(_A, b"in-flight"))
    await _settle()
    box.send_media(_video(_A, b"stale"))
    box.send_media(_config(_A))
    await asyncio.sleep(0.1)

    sock.gate.set()
    assert await box.flush(1.0)

    assert _sent_frames(sock) == [_audio(_A, b"in-flight"), _config(_A)]
    await box.aclose()


async def test_the_byte_backstop_evicts_video_before_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recipient blocked outright is capped in memory, losing video first."""
    frame_bytes = len(_video(_A, b"x" * 90))
    monkeypatch.setattr(outbox, "MAX_MEDIA_BACKLOG_BYTES", frame_bytes * 3)
    gaps: list[bytes] = []
    sock, box = _outbox(held=True, on_video_gap=gaps.append)
    box.send_media(_audio(_A, b"in-flight"))
    await _settle()

    box.send_media(_video(_A, b"x" * 90, key=True))
    box.send_media(_audio(_A, b"y" * 90))
    box.send_media(_audio(_A, b"z" * 90))
    box.send_media(_audio(_A, b"w" * 90))
    sock.gate.set()
    assert await box.flush(1.0)

    assert _sent_frames(sock) == [
        _audio(_A, b"in-flight"),
        _audio(_A, b"y" * 90),
        _audio(_A, b"z" * 90),
        _audio(_A, b"w" * 90),
    ]
    assert gaps == [_A]
    await box.aclose()


async def test_leaving_logs_how_many_frames_the_recipient_missed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The drop count is reported when the connection closes, and only if it lost any."""
    monkeypatch.setattr(outbox, "MAX_MEDIA_AGE", 0.05)
    sock, box = _outbox(held=True)
    box.send_media(_audio(_A, b"in-flight"))
    await _settle()
    box.send_media(_audio(_A, b"stale"))
    await asyncio.sleep(0.1)
    sock.gate.set()
    assert await box.flush(1.0)

    with caplog.at_level(logging.INFO, logger="webstation_broker.outbox"):
        await box.aclose()
        await _outbox("healthy")[1].aclose()

    assert [r.getMessage() for r in caplog.records] == [
        "room outbox: viewer missed 0 video and 1 audio frame(s) while connected"
    ]


async def test_keyframe_requests_reach_only_the_sender_and_are_spaced_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member streaming under the media id is asked, at most once per gap window."""
    (host, host_box), (guest, guest_box) = _outbox("host"), _outbox("guest")
    boxes = [host_box, guest_box]
    session.ROOM["controller"] = {"websocket": host, "public_id": _A.decode(), "outbox": boxes[0]}
    session.ROOM["viewers"] = {"g": {"websocket": guest, "public_id": _B.decode(), "outbox": boxes[1]}}

    session.request_keyframe(_B)
    session.request_keyframe(_B)
    session.request_keyframe(b"ZZZZZZZZ")
    for box in boxes:
        assert await box.flush(1.0)
    assert guest.events == [("json", {"type": "keyframe_request"})]
    assert host.events == []

    monkeypatch.setattr(session, "KEYFRAME_REQUEST_GAP", 0.0)
    session.request_keyframe(_B)
    assert await boxes[1].flush(1.0)
    assert len(guest.events) == 2
    for box in boxes:
        await box.aclose()


async def test_a_send_that_outlasts_the_stall_limit_closes_the_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recipient that stops reading is closed with 1013 rather than queued for forever."""
    monkeypatch.setattr(outbox, "SEND_STALL_LIMIT", 0.05)
    sock, box = _outbox(held=True)

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
    """JSON is never dropped, so a recipient that lets it pile up past the cap is closed instead.

    The send already in flight is abandoned with it, so it neither completes
    nor times out into a second close.
    """
    monkeypatch.setattr(outbox, "MAX_JSON_BACKLOG", 3)
    monkeypatch.setattr(outbox, "SEND_STALL_LIMIT", 0.05)
    sock, box = _outbox(held=True)
    box.send_json({"n": 0})
    await _settle()

    for n in range(1, 5):
        box.send_json({"n": n})
    await asyncio.sleep(0.1)
    sock.gate.set()
    await _settle()

    assert sock.events == [("close", outbox.LAGGARD_CLOSE_CODE)]
    assert box.abandoned.is_set()
    await box.aclose()


async def test_aclose_waits_for_a_laggard_close_already_under_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The close started on giving up is finished, not left running past `aclose`."""
    monkeypatch.setattr(outbox, "MAX_JSON_BACKLOG", 1)
    sock, box = _outbox(held=True)
    real_close = sock.close

    async def slow_close(code: int = 1000) -> None:
        await asyncio.sleep(0.05)
        await real_close(code)

    sock.close = slow_close  # type: ignore[method-assign]
    box.send_json({"n": 0})
    box.send_json({"n": 1})

    await box.aclose()

    assert sock.events == [("close", outbox.LAGGARD_CLOSE_CODE)]


async def test_a_failed_send_is_logged_and_the_next_message_still_goes_out() -> None:
    """One send raising does not stop the drain."""
    sock, box = _outbox()
    calls = 0
    real_send = sock.send_json

    async def flaky(payload: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        await real_send(payload)

    sock.send_json = flaky  # type: ignore[method-assign]

    box.send_json({"n": 1})
    box.send_json({"n": 2})
    assert await box.flush(1.0)

    assert sock.events == [("json", {"n": 2})]
    await box.aclose()


@pytest.mark.parametrize("side", ["client_state", "application_state"])
async def test_nothing_is_sent_to_a_socket_that_is_no_longer_connected(side: str) -> None:
    """A queued message for a socket either end has since closed is skipped, not sent."""
    sock, box = _outbox()
    setattr(sock, side, WebSocketState.DISCONNECTED)

    box.send_json({"n": 1})
    box.send_media(_video(_A, b"f"))
    assert await box.flush(1.0)

    assert sock.events == []
    await box.aclose()


async def test_flush_waits_for_the_queue_and_times_out_on_a_stuck_one() -> None:
    """`flush` reports whether everything queued so far went out in time."""
    sock, box = _outbox()
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
    sock, box = _outbox(held=True)
    box.send_json({"n": 1})
    box.send_media(_video(_A, b"f"))
    await _settle()

    await box.aclose()
    box.send_json({"n": 2})
    sock.gate.set()
    await _settle()

    assert sock.events == []
    assert box._task is None


async def test_session_end_delivers_its_notice_before_closing_each_socket() -> None:
    """`session_ended` reaches every member before their socket closes under it."""
    (host, host_box), (guest, guest_box) = _outbox("host"), _outbox("guest")
    boxes = [host_box, guest_box]
    session.ROOM["controller"] = {"websocket": host, "outbox": boxes[0]}
    session.ROOM["viewers"] = {"g": {"websocket": guest, "outbox": boxes[1]}}

    await session.notify_session_ended()

    for sock in (host, guest):
        assert sock.events == [("json", {"type": "session_ended"}), ("close", 1000)]
    for box in boxes:
        await box.aclose()
