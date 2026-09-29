"""Per-connection outbound queue for room sockets.

A broadcast only queues its message and returns; one drain task per
connection does the sends, so a slow recipient delays nobody but itself.

Traffic goes out in three lanes, each in queue order: JSON (chat, presence,
control) first, then audio, then video. JSON is never dropped. Media is
live, so a recipient that falls behind skips frames instead of building up
delay, and video absorbs the lag before audio does.

A frame is dropped only once it has waited `MAX_MEDIA_AGE`. Webcam deltas
each depend on the frame before, so after one of a sender's video frames is
dropped for a recipient, that sender's later deltas are skipped for it too
until a keyframe, and `on_video_gap` asks the sender for one. A sender's
video config is never dropped.

A recipient that stops draining (one send outlasting `SEND_STALL_LIMIT`, or
more than `MAX_JSON_BACKLOG` JSON messages queued) is closed; the client
reconnects on the same seat.
"""

import asyncio
import collections
import logging
import time
from typing import Any, Callable, Optional, Union

from starlette.websockets import WebSocket, WebSocketState

from .session import PUBLIC_ID_HEX_CHARS

log = logging.getLogger(__name__)

MAX_MEDIA_AGE = 0.5
"""Seconds a media frame may wait for its recipient before it is dropped."""

MAX_MEDIA_BACKLOG_BYTES = 4 * 1024 * 1024
"""Bytes of media queued per recipient before the oldest frames are dropped.

A memory backstop for a recipient whose send is blocked outright, so nothing
is dequeued to be aged out.
"""

MAX_JSON_BACKLOG = 512
"""JSON messages allowed to queue for one recipient before it is closed."""

SEND_STALL_LIMIT = 10.0
"""Seconds one send may take before the recipient is treated as gone and closed."""

CLOSE_WAIT = 2.0
"""Seconds allowed for the close handshake on a recipient already known to be stuck."""

LAGGARD_CLOSE_CODE = 1013
"""Close code for a recipient that stopped draining: 1013, try again later."""

VIDEO_FRAME = 0x01
"""Frame-type byte of an encoded webcam video frame."""

AUDIO_FRAME = 0x02
"""Frame-type byte of an encoded mic audio frame."""

VIDEO_CONFIG = 0x03
"""Frame-type byte of a sender's video decoder config, which is never dropped."""

KEYFRAME_FLAG = 0x01
"""Value of the byte after a video frame's type byte that marks it a keyframe."""

_TYPE_AT = PUBLIC_ID_HEX_CHARS
"""Offset of the frame-type byte: straight after the sender's media id."""

_Queued = tuple[float, bytes]


class Outbox:
    """Queue a room connection's outbound messages and send them from one task.

    The drain task starts on the first queued message, so an outbox can be
    built before the event loop that serves the connection is known.

    Attributes:
        dropped_video: Video frames discarded for this recipient.
        dropped_audio: Audio frames discarded for this recipient.
    """

    def __init__(
        self,
        websocket: WebSocket,
        label: str,
        on_video_gap: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        """Wrap a room connection's socket.

        Args:
            websocket: The accepted room socket the messages go out on.
            label: The member's display name, for log lines only.
            on_video_gap: Called with a sender's media id when this recipient
                has lost one of its video frames and now waits for a keyframe.
        """
        self._websocket = websocket
        self._label = label
        self._on_video_gap = on_video_gap
        self._json: collections.deque[dict[str, Any]] = collections.deque()
        self._audio: collections.deque[_Queued] = collections.deque()
        self._video: collections.deque[_Queued] = collections.deque()
        self._media_bytes = 0
        self._gaps: set[bytes] = set()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: Optional[asyncio.Task[None]] = None
        self._closer: Optional[asyncio.Task[None]] = None
        self._closed = False
        self.dropped_video = 0
        self.dropped_audio = 0

    def send_json(self, payload: dict[str, Any]) -> None:
        """Queue a JSON message; it is never dropped while the connection lives.

        Args:
            payload: The JSON-serializable message, normally carrying a `type` key.
        """
        if self._closed:
            return
        if len(self._json) >= MAX_JSON_BACKLOG:
            log.warning(
                "room outbox: %s has %d unsent messages, closing it",
                self._label,
                len(self._json),
            )
            self._shut()
            return
        self._json.append(payload)
        self._kick()

    def send_media(self, frame: bytes) -> None:
        """Queue a media frame in its lane; it goes out unless it grows stale first.

        Args:
            frame: The stamped frame in the room's binary wire format.
        """
        if self._closed:
            return
        lane = self._video if _is_video(frame) else self._audio
        lane.append((time.monotonic(), frame))
        self._media_bytes += len(frame)
        while self._media_bytes > MAX_MEDIA_BACKLOG_BYTES and self._evict_oldest():
            pass
        self._kick()

    async def flush(self, timeout: float) -> bool:
        """Wait for everything queued so far to be sent or dropped.

        Args:
            timeout: Seconds to wait at most.

        Returns:
            True if the queue emptied in time, False if it did not or the
            outbox closed first.
        """
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return not self._closed

    async def aclose(self) -> None:
        """Stop the drain task and discard anything still queued.

        Called once the connection is over; it does not close the socket.
        Logs how many frames this recipient lost, if any.
        """
        if self.dropped_video or self.dropped_audio:
            log.info(
                "room outbox: %s missed %d video and %d audio frame(s) while connected",
                self._label,
                self.dropped_video,
                self.dropped_audio,
            )
        self._closed = True
        self._clear()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _clear(self) -> None:
        """Drop everything queued and mark the outbox idle."""
        self._json.clear()
        self._audio.clear()
        self._video.clear()
        self._media_bytes = 0
        self._idle.set()

    def _kick(self) -> None:
        """Mark the outbox busy and make sure its drain task is running."""
        self._idle.clear()
        self._wake.set()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._drain())

    def _shut(self) -> None:
        """Give up on a stuck recipient: drop its queue and close it in the background."""
        self._closed = True
        self._clear()
        self._closer = asyncio.get_running_loop().create_task(self._close_socket())

    async def _close_socket(self) -> None:
        """Close the socket with `LAGGARD_CLOSE_CODE`, giving up after `CLOSE_WAIT`."""
        try:
            await asyncio.wait_for(self._websocket.close(code=LAGGARD_CLOSE_CODE), CLOSE_WAIT)
        except Exception as exc:
            log.debug("room outbox: closing %s failed: %s", self._label, exc)

    def _drop_video(self, frame: bytes) -> None:
        """Count a dropped video frame and start waiting for its sender's next keyframe.

        Args:
            frame: The video frame being dropped.
        """
        self.dropped_video += 1
        sender = frame[:_TYPE_AT]
        if sender not in self._gaps:
            self._gaps.add(sender)
            if self.dropped_video == 1:
                log.info("room outbox: %s is behind, skipping video until a keyframe", self._label)
            if self._on_video_gap is not None:
                self._on_video_gap(sender)

    def _evict_oldest(self) -> bool:
        """Drop the oldest droppable frame to get back under the byte backstop.

        Video goes first, since audio is the smaller and the more useful to
        keep; a video config message is never evicted.

        Returns:
            True if a frame was dropped, False if nothing droppable was queued.
        """
        for i, (_, frame) in enumerate(self._video):
            if frame[_TYPE_AT] != VIDEO_CONFIG:
                del self._video[i]
                self._media_bytes -= len(frame)
                self._drop_video(frame)
                return True
        if self._audio:
            _, frame = self._audio.popleft()
            self._media_bytes -= len(frame)
            self.dropped_audio += 1
            return True
        return False

    def _next_media(self) -> Optional[bytes]:
        """Take the next media frame worth sending, dropping stale and undecodable ones.

        Returns:
            The frame to send, or None once both media lanes are empty.
        """
        now = time.monotonic()
        while self._audio:
            queued_at, frame = self._audio.popleft()
            self._media_bytes -= len(frame)
            if now - queued_at > MAX_MEDIA_AGE:
                self.dropped_audio += 1
                continue
            return frame
        while self._video:
            queued_at, frame = self._video.popleft()
            self._media_bytes -= len(frame)
            if frame[_TYPE_AT] == VIDEO_CONFIG:
                return frame
            sender = frame[:_TYPE_AT]
            if sender in self._gaps:
                if _is_keyframe(frame) and now - queued_at <= MAX_MEDIA_AGE:
                    self._gaps.discard(sender)
                    return frame
                self.dropped_video += 1
                continue
            if now - queued_at > MAX_MEDIA_AGE:
                self._drop_video(frame)
                continue
            return frame
        return None

    async def _drain(self) -> None:
        """Send queued messages, JSON then audio then video, until the outbox is closed."""
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            while not self._closed:
                item: Union[dict[str, Any], bytes, None]
                item = self._json.popleft() if self._json else self._next_media()
                if item is None:
                    break
                if self._websocket.client_state != WebSocketState.CONNECTED:
                    continue
                send = (
                    self._websocket.send_bytes(item)
                    if isinstance(item, bytes)
                    else self._websocket.send_json(item)
                )
                try:
                    # timeout() runs the send in this task; wait_for() would
                    # wrap it in a new task per frame on Python 3.11.
                    async with asyncio.timeout(SEND_STALL_LIMIT):
                        await send
                except TimeoutError:
                    log.warning(
                        "room outbox: a send to %s took over %.0fs, closing it",
                        self._label,
                        SEND_STALL_LIMIT,
                    )
                    self._shut()
                    return
                except Exception as exc:
                    log.warning("room send to %s failed: %s", self._label, exc)
            if not self._closed:
                self._idle.set()


def _is_video(frame: bytes) -> bool:
    """Tell whether a frame belongs in the video lane.

    Args:
        frame: A stamped frame in the room's binary wire format.

    Returns:
        True for video frames and video config, False for audio of either kind.
    """
    return frame[_TYPE_AT] in (VIDEO_FRAME, VIDEO_CONFIG)


def _is_keyframe(frame: bytes) -> bool:
    """Tell whether a video frame is a keyframe, which decodes without the frames before it.

    Args:
        frame: A stamped video frame.

    Returns:
        True if its keyframe flag is set.
    """
    return len(frame) > _TYPE_AT + 1 and frame[_TYPE_AT + 1] == KEYFRAME_FLAG
