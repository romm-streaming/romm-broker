"""Room websocket: presence, chat, webcam/mic binary fanout, resolution negotiation, and input assignment.

Binary wire format:

```
[0..7] 8-byte ASCII publicId of sender
[8]    0x01 video frame / 0x02 audio frame / 0x03 video config / 0x04 pcm
[9..]  payload
```

The publicId indirection keeps real tokens out of the media byte stream. It
is a per-connection id (`connection_info["public_id"]` below), regenerated
on every reconnect and sent to the room over `state_update` as `mediaId`;
that is distinct from a seat's persistent `public_id` in `session.py`, which
`state_update` sends as `publicId` and which survives reconnects for the
seat's whole life.
"""

import json
import logging
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import session, settings
from .api import _ct_eq

log = logging.getLogger(__name__)
router = APIRouter()

# Comfortably above any legitimate text frame (chat caps at 500 chars,
# username at 25); rejects a flood of oversized frames before json.loads
# ever runs on them.
MAX_TEXT_FRAME_BYTES = 8 * 1024

CHAT_COOLDOWN_SECONDS = 1.0

MAX_MEDIA_FRAME_BYTES = 1024 * 1024
"""Ceiling on one binary media frame; anything larger is dropped, not relayed."""

MEDIA_ID_BYTES = session.PUBLIC_ID_HEX_CHARS
"""Width of the sender-id prefix on a binary media frame.

Every offset in the wire format is measured from it, and both ends read the
frame type from the byte straight after it, so it has to stay exactly as wide
as the ids `session.new_public_id` mints. A connection whose id does not match
is refused rather than left stamping frames a recipient would misparse.
"""

MEDIA_HEADER_BYTES = MEDIA_ID_BYTES + 1
"""Sender id plus the frame-type byte: the shortest frame that can carry one."""

MAX_MESSAGE_ID_CHARS = 64
"""Ceiling on a `replyTo` id, which is relayed to the room as the client sent it."""

MAX_RESOLUTION_PIXELS = 16384
"""Ceiling on a self-reported client width or height, well past any real display."""


def _gamepad_slot(raw: Any) -> tuple[bool, Optional[int]]:
    """Validate a gamepad slot named in a client frame.

    The slot is written straight into the session and the token map selkies
    routes input by, so an out-of-range or non-numeric one would strand a pad
    on a slot no emulator has.

    Args:
        raw: The `slot` value the client sent, of any type.

    Returns:
        `(True, None)` to unassign, `(True, slot)` for a slot within
        `settings.GAMEPAD_SLOTS`, and `(False, None)` for anything else.
    """
    if raw is None:
        return True, None
    # bool is an int subclass, and True would otherwise pass as gamepad 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return False, None
    if not 1 <= raw <= settings.GAMEPAD_SLOTS:
        return False, None
    return True, raw


def _positive_pixels(raw: Any) -> Optional[int]:
    """Validate a self-reported display dimension.

    Args:
        raw: The `width` or `height` value the client sent, of any type.

    Returns:
        The dimension, or None when it is not a plausible pixel count.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 1 <= raw <= MAX_RESOLUTION_PIXELS else None


@router.websocket("/ws/room")
async def room_websocket(websocket: WebSocket) -> None:
    """Join the play session's room and relay its presence, chat, control and media traffic.

    The `token` query parameter must be the session's controller token or a
    minted viewer token, otherwise the socket is closed with 1008. Text frames
    are JSON actions: the controller may assign gamepad slots, mouse/keyboard
    control and the designated speaker and request resolutions; viewers may
    rename themselves (rate limited to one change per two seconds); anyone may
    chat, report their resolution and send video/audio control messages. Binary
    frames are media in the module's wire format and are fanned out to the
    other members, except from read-only viewers, frames over
    `MAX_MEDIA_FRAME_BYTES` or shorter than their own header, and audio from
    anyone but the designated speaker while one is set. Every frame is checked
    against the session and seat as they stand at that moment, so a connection
    left over from a session that has since been retired, or from a seat that
    has since been reclaimed, closes instead of writing into state nothing
    serves.

    On disconnect the member is removed from the room; a departing viewer also
    loses its slot, mouse/keyboard ownership and speaker role, and the token
    map is pushed to selkies when it held either. The seat itself stays in
    the session, so its token (and the invite link carrying it) keeps working
    until the session ends; a new connection on a token that is already
    connected replaces the old socket. A departing viewer's seat also records
    when it went offline, so the room cap can reclaim the longest-idle
    anonymous seat first when the room is full.

    Args:
        websocket: The incoming room connection.
    """
    token = websocket.query_params.get("token")
    sess = session.SESSION
    if sess is None or not sess.get("active") or not token:
        await websocket.close(code=1008)
        return

    is_controller = _ct_eq(token, sess["controller_token"])
    viewer_ref = session.find_viewer(token)
    is_viewer = viewer_ref is not None
    if not is_controller and not is_viewer:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    if not is_controller:
        # accept() is an await point: a join at the room cap can reclaim this
        # very seat while the handshake is in flight, so the seat is checked
        # again rather than trusting the lookup made before it.
        viewer_ref = session.find_viewer(token)
        if viewer_ref is None:
            await websocket.close(code=1008)
            return

    username = "Controller"
    if is_controller:
        username = (sess.get("user") or {}).get("display_name") or "Controller"
    elif is_viewer:
        # Never fall back to anything derived from the token: this name is
        # broadcast to the whole room, and the token is the seat's credential.
        username = viewer_ref.get("username") or "Viewer"

    connection_info = {
        "websocket": websocket,
        "username": username,
        "token": token,
        "public_id": session.new_public_id(),
        "has_joined": False,
    }
    media_id = connection_info["public_id"].encode("ascii")
    if len(media_id) != MEDIA_ID_BYTES:
        log.error(
            "room websocket: media id for %s is %d bytes, not the %d the wire format carries",
            username,
            len(media_id),
            MEDIA_ID_BYTES,
        )
        await websocket.close(code=1011)
        return
    if is_controller:
        session.ROOM["controller"] = connection_info
    else:
        # A seat lives for the whole session, so the same token can come back
        # after a reload or from another device. The earlier socket, if any,
        # is a leftover of that and is closed rather than left to share the seat.
        previous = session.ROOM["viewers"].get(token)
        session.ROOM["viewers"][token] = connection_info
        if previous is not None:
            try:
                await previous["websocket"].close(code=1000)
            except Exception:
                log.debug("stale room socket for %s was already gone", username)
    await session.broadcast_to_room(
        {"type": "user_joined", "username": username, "timestamp": int(time.time() * 1000)}
    )
    connection_info["has_joined"] = True
    await session.broadcast_state()

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            # Re-read rather than keep the dicts captured at connect time: a
            # retire (with a re-activate on top of it) or a seat reclaim can
            # replace both while this loop is parked in receive(), and writes
            # to the old ones land in state nothing serves anymore.
            sess = session.SESSION
            stale = None
            if sess is None or not sess.get("active"):
                stale = "its session ended"
            elif is_controller:
                if not _ct_eq(token, sess["controller_token"]):
                    stale = "its session was replaced"
            else:
                viewer_ref = session.find_viewer(token)
                if viewer_ref is None:
                    stale = "its seat is gone"
            if stale is not None:
                log.info("room websocket: closing %s's socket because %s", username, stale)
                # Close rather than only leaving the loop: the peer is otherwise
                # left waiting on a socket the broker has already stopped serving.
                try:
                    await websocket.close(code=1001)
                except Exception as exc:
                    log.debug("room websocket for %s was already gone: %s", username, exc)
                break

            if "text" in message:
                if len(message["text"]) > MAX_TEXT_FRAME_BYTES:
                    # 8 KiB is already comfortably above any legitimate text
                    # frame, so one oversized frame is abuse, not a benign
                    # edge case -- close instead of dropping and letting an
                    # attacker hold the connection open for a repeat flood.
                    log.warning("room websocket: oversized text frame from %s, closing", username)
                    await websocket.close(code=1009)
                    return
                try:
                    data = json.loads(message["text"])
                except ValueError as exc:
                    log.warning("room websocket: undecodable frame from %s: %s", username, exc)
                    continue
                # Everything below reads named keys off this frame, so a
                # non-object (a bare number, a list) has to go before it is
                # treated like one.
                if not isinstance(data, dict):
                    log.warning("room websocket: non-object frame from %s, dropped", username)
                    continue
                action = data.get("action")
                if not isinstance(action, str):
                    log.warning("room websocket: frame with no action from %s, dropped", username)
                    continue

                # Clients only ever learn public ids for other members (see
                # session.broadcast_state), so a target named from the wire is
                # a public id and has to be resolved back to its real token.
                if action == "assign_slot" and is_controller:
                    target_token = session.resolve_public_id(data.get("viewer_public_id"))
                    valid, slot = _gamepad_slot(data.get("slot"))
                    if not valid:
                        log.warning(
                            "room websocket: rejecting gamepad slot %r from %s",
                            data.get("slot"),
                            username,
                        )
                        continue
                    await session.handle_assign_slot(target_token, slot)

                elif action == "assign_mk" and is_controller:
                    target_token = session.resolve_public_id(data.get("public_id"))
                    await session.handle_assign_mk(target_token)

                elif action == "set_designated_speaker" and is_controller:
                    requested = data.get("public_id")
                    target_token = session.resolve_public_id(requested)
                    # An unknown or already-departed id resolves to None, which
                    # is also how the room says "no speaker restriction", so
                    # honouring it would quietly reopen audio to everyone.
                    if requested is not None and target_token is None:
                        log.warning(
                            "room websocket: set_designated_speaker names unknown member %r",
                            requested,
                        )
                        continue
                    sess["designated_speaker"] = target_token
                    await session.broadcast_state()

                elif action == "set_username" and is_viewer:
                    # Keyed by token in session.ROOM["cooldowns"], not on
                    # connection_info: a fresh WebSocket reconnect gets a new
                    # connection_info every time, which would otherwise reset
                    # the cooldown for free.
                    cooldowns = session.ROOM["cooldowns"].setdefault(token, {})
                    now = time.time()
                    if now - cooldowns.get("username", 0) < 2.0:
                        continue
                    requested = data.get("username")
                    if not isinstance(requested, str):
                        log.warning(
                            "room websocket: rejecting non-string username %r from %s",
                            requested,
                            username,
                        )
                        continue
                    new_username = requested.strip()
                    if new_username and 1 <= len(new_username) <= 25:
                        old_username = viewer_ref.get("username")
                        if old_username == new_username:
                            continue
                        viewer_ref["username"] = new_username
                        cooldowns["username"] = now
                        connection_info["username"] = new_username
                        username = new_username
                        await session.broadcast_to_room(
                            {
                                "type": "username_changed",
                                "old_username": old_username,
                                "new_username": new_username,
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                        await session.broadcast_state()

                elif action == "send_chat_message":
                    # Same reconnect-proof keying as set_username above.
                    cooldowns = session.ROOM["cooldowns"].setdefault(token, {})
                    now = time.time()
                    if now - cooldowns.get("chat", 0) < CHAT_COOLDOWN_SECONDS:
                        continue
                    body = data.get("message")
                    if not isinstance(body, str):
                        log.warning(
                            "room websocket: rejecting non-string chat message from %s", username
                        )
                        continue
                    text = body.strip()
                    if text and 1 <= len(text) <= 500:
                        cooldowns["chat"] = now
                        # Anything that is not one of this room's message ids
                        # is relayed as no reply at all rather than as an
                        # arbitrary value every client has to render.
                        reply_to = data.get("replyTo")
                        if not isinstance(reply_to, str) or len(reply_to) > MAX_MESSAGE_ID_CHARS:
                            reply_to = None
                        await session.broadcast_to_room(
                            {
                                "type": "chat_message",
                                "sender": username,
                                # Identity clients key their own messages on:
                                # usernames are member-chosen and repeatable, so
                                # matching on them lets one member wear another's.
                                "senderPublicId": session.public_id_for(token),
                                "message": text,
                                "timestamp": int(time.time() * 1000),
                                "messageId": f"{int(time.time() * 1000)}-{secrets.token_hex(4)}",
                                "replyTo": reply_to,
                            }
                        )

                elif action in ("video_state", "audio_state", "force_cursor_render"):
                    # Self-reported (webcam/mic) or self-triggered (gaming-mode
                    # cursor baking by a non-controller viewer, so this can't be
                    # gated to the controller). The payload is rebuilt from the
                    # three fields the room actually acts on, with the sender's
                    # identity stamped by the server: relaying the client's own
                    # dict would carry every extra key it packed in, spoofed
                    # identity included, on to every member.
                    state = data.get("state")
                    if state in (0, 1):
                        await session.broadcast_to_room(
                            {
                                "type": "control",
                                "payload": {
                                    "action": action,
                                    "state": state,
                                    "sender_public_id": session.public_id_for(token),
                                },
                            }
                        )

                elif action == "request_resolutions" and is_controller:
                    await session.broadcast_to_room({"type": "request_resolutions"})

                elif action == "client_resolution":
                    width = _positive_pixels(data.get("width"))
                    height = _positive_pixels(data.get("height"))
                    if width is None or height is None:
                        log.warning(
                            "room websocket: rejecting client resolution %rx%r from %s",
                            data.get("width"),
                            data.get("height"),
                            username,
                        )
                        continue
                    await session.broadcast_to_room(
                        {
                            "type": "resolution_update",
                            "public_id": session.public_id_for(token),
                            "width": width,
                            "height": height,
                        }
                    )

            elif "bytes" in message:
                binary_data = message["bytes"]
                if is_viewer and viewer_ref.get("permission") == "readonly":
                    continue
                if len(binary_data) > MAX_MEDIA_FRAME_BYTES:
                    # Debug, not warning: a real encoder can overshoot on a
                    # keyframe, and one log line per frame would flood.
                    log.debug(
                        "room websocket: dropping %d-byte media frame from %s, over the %d cap",
                        len(binary_data),
                        username,
                        MAX_MEDIA_FRAME_BYTES,
                    )
                    continue
                if len(binary_data) < MEDIA_HEADER_BYTES:
                    log.warning(
                        "room websocket: dropping %d-byte media frame from %s, "
                        "shorter than its %d-byte header",
                        len(binary_data),
                        username,
                        MEDIA_HEADER_BYTES,
                    )
                    continue
                designated = sess.get("designated_speaker")
                is_audio = binary_data[MEDIA_ID_BYTES] == 0x02
                if designated and is_audio and token != designated:
                    continue
                # The leading bytes are the publicId recipients attribute the
                # frame to (see the module docstring); trusting whatever the
                # sender put there would let any participant forge another
                # member's video/audio tile, so it is replaced with this
                # connection's own publicId rather than relayed as sent.
                stamped = media_id + binary_data[MEDIA_ID_BYTES:]
                await session.broadcast_binary_to_room(stamped, websocket)

    except (WebSocketDisconnect, RuntimeError):
        log.info("room websocket disconnected for %s", username)
    except Exception:
        log.exception("unhandled room websocket error for %s", username)
    finally:
        current_username = connection_info.get("username")
        # A socket that was replaced by a newer one on the same token is not
        # the member leaving, so it neither cleans up nor announces a departure.
        was_live = (
            session.ROOM.get("controller") is connection_info
            if is_controller
            else session.ROOM["viewers"].get(token) is connection_info
        )
        if is_controller:
            if was_live:
                session.ROOM["controller"] = None
        elif was_live:
            # Only the live connection for the seat cleans up; a socket that
            # was replaced by a newer one on the same token must not release
            # what the newer one now holds. The seat itself stays: its invite
            # link is valid for the life of the session, and it only gives up
            # the input it was holding, since a pad nobody is driving should
            # go back to the tray.
            session.ROOM["viewers"].pop(token, None)
            sess = session.SESSION
            if sess is not None:
                if sess.get("designated_speaker") == token:
                    sess["designated_speaker"] = None
                disconnected = session.find_viewer(token)
                input_released = False
                if disconnected:
                    disconnected["last_seen"] = time.time()
                    released_slot = disconnected.get("slot")
                    if released_slot:
                        # Give the pad up before announcing it: this runs while
                        # the connection is being torn down, and an announcement
                        # that never finishes must not leave the seat holding a
                        # pad nobody can drive.
                        disconnected["slot"] = None
                        input_released = True
                        await session.broadcast_to_room(
                            {
                                "type": "gamepad_change",
                                "message": f"{disconnected.get('username', 'A user')} disconnected "
                                f"and was unassigned from Gamepad {released_slot}.",
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                    if sess.get("mk_owner_token") == token:
                        sess["mk_owner_token"] = None
                        disconnected["mk_control"] = False
                        input_released = True
                        await session.broadcast_to_room(
                            {
                                "type": "mk_change",
                                "message": f"{disconnected.get('username', 'User')} disconnected. "
                                "MK control reverted to Controller.",
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                if input_released:
                    from . import selkies

                    if not await selkies.push_tokens(sess):
                        log.error(
                            "room: selkies kept the old token map after %r disconnected, "
                            "their input routing is still live",
                            current_username,
                        )

        if was_live and connection_info.get("has_joined"):
            await session.broadcast_to_room(
                {
                    "type": "user_left",
                    "username": current_username,
                    "timestamp": int(time.time() * 1000),
                }
            )
        await session.broadcast_state()
