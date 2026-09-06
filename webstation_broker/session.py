"""Single-session state and room fanout.

The container hosts exactly one play session at a time, so session and room
state are module globals.
"""

import asyncio
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, Optional

from starlette.websockets import WebSocket, WebSocketState

from . import selkies, settings

if TYPE_CHECKING:
    from .emulators.base import Emulator

log = logging.getLogger(__name__)

SESSION: Optional[dict[str, Any]] = None
"""The active play session, or None.

Shape:

```
{
  "id", "active", "created_at",
  "user": {...}, "emulator": "pcsx2", "rom": {...}, "rom_file": str | None,
  "save": {...} | None, "callback": {...} | None, "multiplayer": bool,
  "controller_token", "controller_public_id",
  "invites": {"participant": str, "readonly": str},
  "viewers": [{"token","public_id","user_id","anonymous","last_seen","slot",
               "mk_control","username","permission"}...],
  "controller_slot", "mk_owner_token", "designated_speaker",
  "save_baseline": float, "emulator_obj": Emulator,
}
```

`token` is each seat's bearer credential; `public_id` is a non-sensitive
stand-in safe to broadcast to the room in its place (see `broadcast_state`).
"""

LAST_EXIT: Optional[dict[str, Any]] = None
"""What is left of the session that just exited: `{"id", "rom", "emulator_obj"}`.

RomM files the exit state in its library after the teardown has answered, so
the emulator that captured it has to outlive the session for the read routes
to find the file. Dropped at the next activate.
"""

ROOM: dict[str, Any] = {"controller": None, "viewers": {}, "cooldowns": {}}
"""Live websocket connections for the room.

`controller` is the controller's connection info dict (or None while offline),
`viewers` maps each online viewer's token to its connection info dict, and
`cooldowns` tracks per-token rate limits.
"""

PUBLIC_ID_HEX_CHARS = 8
"""Fixed width, in ASCII characters, of every public id the broker mints.

The room's binary media frames carry the sender's id in a prefix of exactly
this width and read the frame type from the byte straight after it (see
`room.MEDIA_ID_BYTES`), so an id of any other length would put every frame's
type byte at the wrong offset for every recipient.
"""

SEAT_RECLAIM_GRACE_SECONDS = 60.0
"""How long a seat is protected from reclaim after its `last_seen` was stamped.

`last_seen` is stamped when the seat is minted and again when its socket
drops, so the same window covers the gap before a freshly invited browser has
finished loading and the gap a reload leaves behind. Without it a burst of
joins at the cap keeps reclaiming seats handed out seconds earlier, and every
arrival evicts the one before it instead of anybody getting in.
"""


def new_public_id() -> str:
    """Mint a broadcast-safe id at the width the room's media wire format assumes.

    Returns:
        A hex id exactly `PUBLIC_ID_HEX_CHARS` characters wide.
    """
    return secrets.token_hex(PUBLIC_ID_HEX_CHARS // 2)


def _session_id(raw: object) -> str:
    """Reduce a caller-supplied id to what is safe in the export filename.

    The exit archive is named after the session, and the export routes reject
    anything with path structure in it, so an id carrying a slash or a dot
    would produce an archive the parent could never fetch back.

    Args:
        raw: The `session_id` from the activate payload, of any type or None.

    Returns:
        `raw` stringified and stripped to `[A-Za-z0-9_-]`, at most 64
        characters, or a fresh random hex id when nothing is left.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:64]
    return cleaned or secrets.token_hex(8)


def new_session(
    payload: dict[str, Any], emulator_obj: "Emulator", rom_file: Optional[str]
) -> dict[str, Any]:
    """Replace the module-level session with a fresh one built from the activate payload.

    The controller starts with gamepad 1 and mouse/keyboard control; the viewer
    list starts empty and the save baseline is the moment of creation.

    Args:
        payload: The activate request body; `emulator` is required, the rest
            (`session_id`, `user`, `rom`, `save`, `callback`, `multiplayer`) is
            optional.
        emulator_obj: The emulator instance driving this session.
        rom_file: The resolved path of the ROM file being played, or None for a
            session with nothing to boot from (a resumed state, or an emulator
            launched without a ROM). Only ever read back out for the status
            routes to report.

    Returns:
        The new session dict, which is also stored in `SESSION`.
    """
    global SESSION, LAST_EXIT
    # The previous session's state is about to be cleared off the working slot,
    # and serving it under this session's rom would file it against the wrong
    # game, so the record goes before the new one is built.
    LAST_EXIT = None
    SESSION = {
        "id": _session_id(payload.get("session_id")),
        "active": True,
        "created_at": time.time(),
        "user": payload.get("user") or {},
        "emulator": payload["emulator"],
        "rom": payload.get("rom") or {},
        "rom_file": rom_file,
        "save": payload.get("save"),
        "callback": payload.get("callback"),
        "multiplayer": bool(payload.get("multiplayer")),
        "controller_token": secrets.token_urlsafe(16),
        "controller_public_id": new_public_id(),
        "viewers": [],
        # Reusable invite tokens by permission, minted on first request. One
        # link per role is what a host hands round; each arrival on it takes
        # its own seat.
        "invites": {},
        # The controller starts with gamepad 1.
        "controller_slot": 1,
        "mk_owner_token": None,
        "designated_speaker": None,
        "save_baseline": time.time(),
        "emulator_obj": emulator_obj,
    }
    return SESSION


def retire_session() -> None:
    """End the session, keeping what the state routes still have to answer with.

    Exit captures a state and then tears the session down, but RomM only asks
    for that state once the teardown has replied, so clearing outright is what
    made every post-exit read a 409. Only the emulator is kept, and only the
    read routes consult it: nothing here can be played, written to or resumed.
    """
    global SESSION, LAST_EXIT
    if SESSION is not None:
        LAST_EXIT = {
            "id": SESSION["id"],
            "rom": SESSION.get("rom") or {},
            "emulator_obj": SESSION["emulator_obj"],
        }
    SESSION = None


def find_viewer(token: str) -> Optional[dict[str, Any]]:
    """Look up a viewer entry in the active session by its token.

    Args:
        token: The viewer's streaming token.

    Returns:
        The viewer dict, or None when there is no session or no viewer holds
        that token.
    """
    if SESSION is None:
        return None
    return next((v for v in SESSION.get("viewers", []) if v["token"] == token), None)


def public_id_for(token: str) -> Optional[str]:
    """Resolve a seat's real token to the non-sensitive id safe to broadcast for it.

    Args:
        token: A controller or viewer token already known to be valid.

    Returns:
        The matching public id, or None when there is no session or no seat
        holds that token.
    """
    if SESSION is None:
        return None
    if SESSION.get("controller_token") == token:
        return SESSION.get("controller_public_id")
    viewer = find_viewer(token)
    return viewer.get("public_id") if viewer else None


def resolve_public_id(public_id: Optional[str]) -> Optional[str]:
    """Resolve a broadcast-safe public id back to the seat's real token.

    The inverse of `public_id_for`, used to translate a room-management
    target (e.g. who a gamepad slot goes to) that a client can only name by
    its public id back into the token the rest of the session logic keys on.

    Args:
        public_id: The publicId a client sent to name a room member, or None.

    Returns:
        The matching seat token, or None when there is no session, no seat
        holds that public id, or `public_id` is None.
    """
    if SESSION is None or public_id is None:
        return None
    if SESSION.get("controller_public_id") == public_id:
        return SESSION["controller_token"]
    for v in SESSION.get("viewers", []):
        if v.get("public_id") == public_id:
            return v["token"]
    return None


def invite_token(permission: str) -> str:
    """Return the session's shareable invite token for a permission, minting it on first use.

    The token identifies the session and the permission, not a person: every
    arrival on it is seated separately by `add_viewer`, so the same link can go
    to any number of friends and stays valid until the session ends.

    Args:
        permission: The permission the link grants, `participant` or `readonly`.

    Returns:
        The invite token for that permission.
    """
    invites = SESSION.setdefault("invites", {})
    if permission not in invites:
        invites[permission] = secrets.token_urlsafe(16)
    return invites[permission]


def find_invite(token: str) -> Optional[str]:
    """Resolve an invite token to the permission it grants.

    Args:
        token: The `invite` query parameter from a landing URL.

    Returns:
        The permission, or None when no session is active or the token is unknown.
    """
    if SESSION is None:
        return None
    for permission, minted in SESSION.get("invites", {}).items():
        if secrets.compare_digest(minted, token):
            return permission
    return None


def _release_seat(token: str) -> Optional[dict[str, Any]]:
    """Drop a removed seat's rate-limit cooldowns and role, and detach its live connection.

    Called for a seat leaving `SESSION["viewers"]` outside the normal disconnect
    path (a same-user rejoin replacing it, or the reclaim of a stale seat), both
    of which can happen while the seat still holds the speaker or MK role from
    the unvalidated set_designated_speaker/assign_mk actions, or a cooldowns
    entry, both cleared here. A rejoin can also evict a seat whose socket is
    still connected (a second tab, a reload racing the old tab's close): that
    connection is detached from `ROOM["viewers"]` here, synchronously, so
    nothing can look it up as the seat's connection anymore, but closing it is
    left to the caller. It must stay a synchronous pop rather than an awaited
    close here, since add_viewer's cap check right after this needs the whole
    admission decision to run without yielding control, or a concurrent join
    could interleave mid-decision and seat past the cap.

    Args:
        token: The removed seat's token.

    Returns:
        The token's live connection info, for the caller to close, or None if
        it had none.
    """
    ROOM["cooldowns"].pop(token, None)
    if SESSION.get("designated_speaker") == token:
        SESSION["designated_speaker"] = None
    if SESSION.get("mk_owner_token") == token:
        SESSION["mk_owner_token"] = None
    return ROOM["viewers"].pop(token, None)


async def add_viewer(permission: str, user: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Mint a viewer token and add the viewer to the active session.

    A re-join by the same user (matched by id, else username) replaces the old
    entry and invalidates its token. If that old seat still has a live room
    connection (a second tab, a reload racing the first tab's close), that
    connection is force-closed rather than left running on a token nothing
    recognizes anymore. The close is deferred until every seat has already
    been decided (see `_release_seat`), so it never interleaves with a
    concurrent join's own admission decision.

    Args:
        permission: The viewer's permission level, e.g. `readonly`.
        user: The joining user's details (`id`, `display_name`, `username`), or
            None for an anonymous viewer who gets a generated username.

    Returns:
        The new viewer dict: `{"token", "public_id", "user_id", "anonymous",
        "last_seen", "slot", "mk_control", "username", "permission"}`. None when the
        session is at `settings.MAX_ROOM_VIEWERS` seats and none of them can
        be reclaimed: every seat is either a named user, an anonymous seat
        that is still connected, or one whose `last_seen` is still inside
        `SEAT_RECLAIM_GRACE_SECONDS`.
    """
    import random

    user = user or {}
    username = user.get("display_name") or user.get("username")
    anonymous = user.get("id") is None and not username

    def _same_user(v: dict[str, Any]) -> bool:
        """Whether viewer entry `v` belongs to the user now joining."""
        if user.get("id") is not None and v.get("user_id") is not None:
            return v["user_id"] == user["id"]
        return bool(username) and v.get("username") == username

    to_close: list[dict[str, Any]] = []

    replaced = [v for v in SESSION.get("viewers", []) if _same_user(v)]
    SESSION["viewers"] = [v for v in SESSION.get("viewers", []) if not _same_user(v)]
    # A rejoining seat's old token is invalidated above but was never
    # disconnected, so it can still hold the speaker/MK role or a cooldowns
    # entry that would otherwise dangle on a token nothing can use anymore,
    # and its socket, if still open, is still relaying room traffic.
    for old in replaced:
        live = _release_seat(old["token"])
        if live is not None:
            to_close.append(live)

    if len(SESSION["viewers"]) >= settings.MAX_ROOM_VIEWERS:
        # A seat lives for the whole session by design (its invite link stays
        # valid after the tab closes), so an anonymous arrival never frees its
        # own slot on disconnect. Reclaim the anonymous seat idle longest
        # rather than treat every seat ever minted as permanent. "anonymous"
        # is fixed at creation from the same fields _same_user matches on, so
        # a later self-chosen nickname can't exempt a seat from reclaim.
        # A seat is only idle once it has been out of touch for the grace
        # window: a seat minted seconds ago has an unconnected browser still
        # loading behind it, and reclaiming that is indistinguishable from
        # reclaiming one whose user left an hour ago.
        online_tokens = set(ROOM.get("viewers", {}).keys())
        now = time.time()
        reclaimable = [
            v
            for v in SESSION["viewers"]
            if v.get("anonymous")
            and v["token"] not in online_tokens
            and now - v.get("last_seen", 0.0) >= SEAT_RECLAIM_GRACE_SECONDS
        ]
        stale = min(reclaimable, key=lambda v: v["last_seen"], default=None)
        if stale is None:
            log.warning(
                "session: refusing new viewer, room is at its %d-seat cap",
                settings.MAX_ROOM_VIEWERS,
            )
            return None
        log.info(
            "session: reclaiming disconnected anonymous seat %r for a new arrival",
            stale.get("username"),
        )
        SESSION["viewers"].remove(stale)
        # The reclaimable filter above already proved stale's token is offline,
        # so if it still holds the speaker or MK role that role has to be
        # cleared here. It never has a live connection to close.
        _release_seat(stale["token"])

    viewer = {
        "token": secrets.token_urlsafe(16),
        "public_id": new_public_id(),
        "user_id": user.get("id"),
        "anonymous": anonymous,
        "last_seen": time.time(),
        "slot": None,
        "mk_control": False,
        "username": username or f"User-{random.randint(100, 999)}",
        "permission": permission,
    }
    SESSION.setdefault("viewers", []).append(viewer)

    # Deferred until every seat's admission is fully decided above, so an
    # awaited close can't yield control mid-decision to a concurrent join.
    for conn in to_close:
        try:
            await conn["websocket"].close(code=1008)
        except Exception:
            log.debug("room socket for a released seat was already gone")

    return viewer


async def broadcast_to_room(payload: dict[str, Any]) -> None:
    """Send a JSON message to every connected room member.

    Sends run concurrently; a failure on one socket is logged and does not
    stop delivery to the others.

    Args:
        payload: The JSON-serializable message, normally carrying a `type` key.
    """
    all_ws = []
    if ROOM.get("controller"):
        all_ws.append(ROOM["controller"]["websocket"])
    for conn in ROOM.get("viewers", {}).values():
        all_ws.append(conn["websocket"])
    tasks = [
        ws.send_json(payload)
        for ws in all_ws
        if ws.client_state == WebSocketState.CONNECTED
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            log.warning("room send failed: %s", result)


async def broadcast_binary_to_room(payload: bytes, sender_ws: WebSocket) -> None:
    """Relay a binary media frame to every connected room member except its sender.

    Args:
        payload: The raw frame in the room's binary wire format.
        sender_ws: The websocket the frame arrived on; it is skipped.
    """
    all_ws = []
    if ROOM.get("controller") and ROOM["controller"]["websocket"] != sender_ws:
        all_ws.append(ROOM["controller"]["websocket"])
    for conn in ROOM.get("viewers", {}).values():
        if conn["websocket"] != sender_ws:
            all_ws.append(conn["websocket"])
    tasks = [
        ws.send_bytes(payload)
        for ws in all_ws
        if ws.client_state == WebSocketState.CONNECTED
    ]
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.warning("room binary send failed: %s", result)


async def broadcast_state() -> None:
    """Broadcast a `state_update` describing every room member to the room.

    The controller is listed first, then each viewer with its online status
    and mouse/keyboard ownership. Members are identified by `publicId`, a
    non-sensitive stand-in for their seat token: broadcasting the real token
    would hand every room member, including anonymous viewers, the bearer
    credential needed to reconnect as (or impersonate) anyone else. While a
    member is connected its `mediaId`, the per-connection id its live socket
    tags binary media frames with, is included too. Does nothing when there
    is no session.

    Each entry is assembled field by field rather than copied from the seat:
    everyone in the room reads this, so a seat's RomM `user_id` and the
    bookkeeping the room has no use for stay on the server side.
    """
    if SESSION is None:
        return
    controller_name = (SESSION.get("user") or {}).get("display_name") or "Controller"
    controller_info = {
        "publicId": SESSION["controller_public_id"],
        "username": controller_name,
        "slot": SESSION.get("controller_slot"),
        "online": ROOM.get("controller") is not None,
        "has_mk": (SESSION.get("mk_owner_token") == SESSION["controller_token"])
        or (SESSION.get("mk_owner_token") is None),
        "permission": "controller",
        "mediaId": ROOM["controller"]["public_id"] if ROOM.get("controller") else None,
    }
    users = [controller_info]
    for v in SESSION.get("viewers", []):
        conn = ROOM.get("viewers", {}).get(v["token"])
        users.append(
            {
                "publicId": v.get("public_id"),
                "username": v.get("username"),
                "slot": v.get("slot"),
                "permission": v.get("permission"),
                "has_mk": SESSION.get("mk_owner_token") == v["token"],
                "online": conn is not None,
                "mediaId": conn.get("public_id") if conn else None,
            }
        )
    await broadcast_to_room(
        {
            "type": "state_update",
            "viewers": users,
            "designated_speaker": public_id_for(SESSION["designated_speaker"])
            if SESSION.get("designated_speaker")
            else None,
        }
    )


async def handle_assign_slot(viewer_token: Optional[str], slot: Optional[int]) -> None:
    """Assign a gamepad slot to a room member, or take theirs away.

    A slot can only be held by one member, so whoever held it before is
    unassigned first. The new token map is pushed to selkies and a
    `gamepad_change` notification is broadcast for every change made, followed
    by a state update. Unknown tokens are logged and ignored.

    A push selkies refuses is logged and the room is still told: the
    notification describes what the session now holds, and the next successful
    push carries the whole map, so nothing here is worth leaving the room's
    view of itself out of step with the broker's over.

    Args:
        viewer_token: The token of the member to change; the controller's own
            token targets the controller.
        slot: The gamepad slot to assign, or None to unassign.
    """
    if SESSION is None:
        return
    target_user = None
    target_username = "Unknown"
    old_slot = None
    if viewer_token == SESSION["controller_token"]:
        target_user = SESSION
        target_username = "Controller"
        old_slot = SESSION.get("controller_slot")
    else:
        for v in SESSION.get("viewers", []):
            if v["token"] == viewer_token:
                target_user = v
                target_username = v.get("username", "Unnamed")
                old_slot = v.get("slot")
                break
    if not target_user:
        log.warning("assign_slot for unknown token")
        return

    notifications = []
    if slot is not None:
        cleared = False
        if (
            SESSION.get("controller_slot") == slot
            and SESSION["controller_token"] != viewer_token
        ):
            SESSION["controller_slot"] = None
            notifications.append(f"Controller was unassigned from Gamepad {slot}.")
            cleared = True
        if not cleared:
            for v in SESSION.get("viewers", []):
                if v.get("slot") == slot and v.get("token") != viewer_token:
                    v["slot"] = None
                    notifications.append(
                        f"{v.get('username', 'Unnamed')} was unassigned from Gamepad {slot}."
                    )
                    break

    if target_user is SESSION:
        SESSION["controller_slot"] = slot
    else:
        target_user["slot"] = slot

    if slot is not None and old_slot != slot:
        notifications.append(f"Gamepad {slot} was assigned to {target_username}.")
    elif slot is None and old_slot is not None:
        notifications.append(f"{target_username} was unassigned from Gamepad {old_slot}.")

    if not await selkies.push_tokens(SESSION):
        log.error(
            "session %s: selkies kept the old token map, gamepad slot %s for %r is not live yet",
            SESSION["id"],
            slot,
            target_username,
        )
    for msg in notifications:
        await broadcast_to_room(
            {"type": "gamepad_change", "message": msg, "timestamp": int(time.time() * 1000)}
        )
    await broadcast_state()


async def handle_assign_mk(target_token: Optional[str]) -> None:
    """Hand mouse and keyboard control to a room member.

    The controller's own token and None both mean control returns to the
    controller. A no-op when the target already holds it; otherwise the token
    map is pushed to selkies and an `mk_change` notification and a state update
    are broadcast. A push selkies refuses is logged, since input keeps routing
    by the map it already has until some later push lands.

    Args:
        target_token: The token of the viewer to receive control, or None (or
            the controller's token) to give it back to the controller.
    """
    if SESSION is None:
        return
    if target_token == SESSION["controller_token"]:
        target_token = None
    if SESSION.get("mk_owner_token") == target_token:
        return
    SESSION["mk_owner_token"] = target_token

    username = "Controller"
    if target_token:
        for v in SESSION.get("viewers", []):
            if v["token"] == target_token:
                username = v.get("username", "User")
                break

    if not await selkies.push_tokens(SESSION):
        log.error(
            "session %s: selkies kept the old token map, mouse and keyboard control "
            "is still routed to whoever held it before %r",
            SESSION["id"],
            username,
        )
    await broadcast_to_room(
        {
            "type": "mk_change",
            "message": f"Mouse & Keyboard control assigned to {username}.",
            "timestamp": int(time.time() * 1000),
        }
    )
    await broadcast_state()


async def notify_session_ended() -> None:
    """Tell the room the session has ended, then close and forget every connection.

    Forgetting a socket does not end it: the handler behind it stays parked in
    `receive()` forever, so every connection of every ended session would
    survive as a live socket and a running task. Each one is detached from
    `ROOM` first and closed after, which also means a handler waking on the
    close finds itself already replaced and skips its per-member cleanup: the
    seat, role and token-map releases it would do are moot against a session
    being torn down, and its departure notice would go to a room that has
    already been told the whole session ended.
    """
    await broadcast_to_room({"type": "session_ended"})
    connections = []
    if ROOM.get("controller"):
        connections.append(ROOM["controller"])
    connections.extend(ROOM.get("viewers", {}).values())
    ROOM["controller"] = None
    ROOM["viewers"] = {}
    ROOM["cooldowns"] = {}
    for conn in connections:
        try:
            await conn["websocket"].close(code=1000)
        except Exception as exc:
            log.debug(
                "session: room socket for %r was already gone at session end: %s",
                conn.get("username"),
                exc,
            )
