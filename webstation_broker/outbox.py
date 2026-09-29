"""Per-connection outbound queue for room sockets.

A room broadcast used to await every recipient's send before returning, and
the media relay awaits the broadcast inline in the sender's receive loop. A
single viewer whose connection backed up (a slow uplink, a suspended tab)
therefore stalled the sender, and with it every other member's frames, chat
and control messages. Each room connection now owns an `Outbox`: a broadcast
only queues the message and returns, and one drain task per connection does
the actual sends, so a slow recipient only ever delays itself.

The two kinds of traffic are queued apart. JSON (chat, presence, control) is
never dropped and is sent before any queued media, in the order it was
queued. Media frames are live video and audio, where a late frame is worth
less than the next one, so a recipient that falls behind loses its oldest
queued frames instead of building up delay. A recipient that stops draining
altogether, one send outlasting `SEND_STALL_LIMIT` or more JSON backed up
than `MAX_JSON_BACKLOG`, is closed: its handler then runs the normal
departure cleanup, and the client can reconnect on the same seat.
"""

import asyncio
import collections
import logging
from typing import Any, Optional, Union

from starlette.websockets import WebSocket, WebSocketState

log = logging.getLogger(__name__)

MAX_MEDIA_BACKLOG = 64
"""Media frames kept per recipient before the oldest is dropped.

About a second of 60 fps video plus its audio: enough to ride out a brief
hiccup without adding noticeable delay once it clears.
"""

MAX_JSON_BACKLOG = 512
"""JSON messages allowed to queue for one recipient before it is closed.

JSON is never dropped, so this is only a backstop against a recipient that
has stopped reading while the room keeps talking; normal traffic never comes
close to it.
"""

SEND_STALL_LIMIT = 10.0
"""Seconds one send may take before the recipient is treated as gone and closed."""

CLOSE_WAIT = 2.0
"""Seconds allowed for the close handshake on a recipient already known to be stuck."""

LAGGARD_CLOSE_CODE = 1013
"""Close code for a recipient that stopped draining: 1013, try again later."""


class Outbox:
    """Queue a room connection's outbound messages and send them from one task.

    The drain task starts on the first queued message, so an outbox can be
    built before the event loop that serves the connection is known.
    """

    def __init__(self, websocket: WebSocket, label: str) -> None:
        """Wrap a room connection's socket.

        Args:
            websocket: The accepted room socket the messages go out on.
            label: The member's display name, for log lines only.
        """
        self._websocket = websocket
        self._label = label
        self._json: collections.deque[dict[str, Any]] = collections.deque()
        self._media: collections.deque[bytes] = collections.deque(maxlen=MAX_MEDIA_BACKLOG)
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._closer: Optional[asyncio.Task[None]] = None
        self.dropped_frames = 0
        """Media frames discarded because this recipient was behind; for logs and tests."""

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
        """Queue a media frame, dropping the oldest queued one if the recipient is behind.

        Args:
            frame: The stamped frame in the room's binary wire format.
        """
        if self._closed:
            return
        if len(self._media) == MAX_MEDIA_BACKLOG:
            self.dropped_frames += 1
            if self.dropped_frames == 1 or self.dropped_frames % 1000 == 0:
                log.info(
                    "room outbox: %s is behind, %d media frame(s) dropped so far",
                    self._label,
                    self.dropped_frames,
                )
        self._media.append(frame)
        self._kick()

    async def flush(self, timeout: float) -> bool:
        """Wait for everything queued so far to be sent.

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
        """
        self._closed = True
        self._json.clear()
        self._media.clear()
        self._idle.set()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _kick(self) -> None:
        """Mark the outbox busy and make sure its drain task is running."""
        self._idle.clear()
        self._wake.set()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._drain())

    def _shut(self) -> None:
        """Give up on a stuck recipient: drop its queue and close it in the background."""
        self._closed = True
        self._json.clear()
        self._media.clear()
        self._idle.set()
        self._closer = asyncio.get_running_loop().create_task(self._close_socket())

    async def _close_socket(self) -> None:
        """Close the socket with `LAGGARD_CLOSE_CODE`, giving up after `CLOSE_WAIT`."""
        try:
            await asyncio.wait_for(self._websocket.close(code=LAGGARD_CLOSE_CODE), CLOSE_WAIT)
        except Exception as exc:
            log.debug("room outbox: closing %s failed: %s", self._label, exc)

    async def _drain(self) -> None:
        """Send queued messages, JSON first, until the outbox is closed."""
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            while not self._closed and (self._json or self._media):
                item: Union[dict[str, Any], bytes]
                if self._json:
                    item = self._json.popleft()
                else:
                    item = self._media.popleft()
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
            if not (self._json or self._media):
                self._idle.set()
