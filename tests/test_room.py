"""Room websocket: oversized-frame rejection, chat rate limiting, and video/audio/cursor state validation."""

import asyncio
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, cast

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect

from webstation_broker import outbox, room, session, settings

from .conftest import PREFIX, FakeEmulator

API = f"{PREFIX}/api"


def _activate(client: TestClient, broker_dirs: dict[str, Path], **overrides: object) -> str:
    """Activate a session for the fake emulator and return the controller token."""
    rom = broker_dirs["roms"] / "Game.iso"
    rom.write_bytes(b"iso")
    body = {
        "session_id": "sess-1",
        "emulator": "fake",
        "user": {"id": 1, "username": "ana", "display_name": "Ana"},
        "rom": {"id": 5, "name": "Game", "platform": "ps2", "path": str(rom)},
    }
    body.update(overrides)
    response = client.post(f"{API}/session/activate", json=body)
    return response.json()["url"].split("token=")[1]


@contextmanager
def _connect(client: TestClient, token: str) -> Iterator[WebSocketTestSession]:
    """Open the room socket and drain the join broadcasts every connect sends."""
    with client.websocket_connect(f"{PREFIX}/ws/room?token={token}") as conn:
        conn.receive_json()  # user_joined
        conn.receive_json()  # state_update
        yield conn


def test_an_oversized_text_frame_closes_the_connection(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An oversized text frame closes the connection.

    8 KiB is already comfortably above any legitimate text frame, so the server treats one oversized
    frame as abuse and closes rather than dropping it and leaving the connection open to a repeat
    flood.
    """
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_text("x" * (room.MAX_TEXT_FRAME_BYTES + 1))

        with pytest.raises(WebSocketDisconnect):
            conn.receive_json()


def test_a_chat_message_inside_the_cooldown_window_is_dropped(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat message inside the cooldown window is dropped."""
    token = _activate(client, broker_dirs)
    clock = {"now": 1000.0}
    monkeypatch.setattr(room.time, "time", lambda: clock["now"])
    with _connect(client, token) as conn:
        conn.send_json({"action": "send_chat_message", "message": "first"})
        first = conn.receive_json()
        assert first["message"] == "first"

        clock["now"] += 0.1
        conn.send_json({"action": "send_chat_message", "message": "too soon"})
        # The room reads the clock when it actually processes a message, not
        # when the test sends one, so a harmless message the server does
        # reply to forces it to have handled "too soon" before the clock
        # moves again -- otherwise both sends race the same clock value.
        conn.send_json({"action": "video_state", "state": 1})
        sync = conn.receive_json()
        assert sync["payload"]["state"] == 1

        clock["now"] += room.CHAT_COOLDOWN_SECONDS
        conn.send_json({"action": "send_chat_message", "message": "after cooldown"})
        second = conn.receive_json()

        assert second["message"] == "after cooldown"


def test_the_chat_cooldown_survives_a_reconnect(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat cooldown survives a reconnect.

    The cooldown is keyed by token in session.ROOM["cooldowns"], not on the per-connection object,
    since otherwise disconnecting and reconnecting with the same token would hand out a fresh,
    unthrottled connection on every reconnect.
    """
    token = _activate(client, broker_dirs)
    clock = {"now": 1000.0}
    monkeypatch.setattr(room.time, "time", lambda: clock["now"])

    with _connect(client, token) as conn:
        conn.send_json({"action": "send_chat_message", "message": "first"})
        first = conn.receive_json()
        assert first["message"] == "first"

    clock["now"] += 0.1
    with _connect(client, token) as conn:
        conn.send_json({"action": "send_chat_message", "message": "too soon"})
        conn.send_json({"action": "video_state", "state": 1})
        sync = conn.receive_json()
        assert sync["payload"]["state"] == 1


def test_a_video_state_of_zero_or_one_is_forwarded(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A video state of zero or one is forwarded."""
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "video_state", "state": 1})
        message = conn.receive_json()

        assert message["type"] == "control"
        assert message["payload"]["state"] == 1


def test_a_json_boolean_state_is_forwarded(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A JSON boolean state is forwarded.

    JS booleans serialize as JSON true/false, which Python decodes as True/False -- these have to keep
    working since the room.js webcam/mic toggles send exactly this shape.
    """
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "audio_state", "state": True})
        message = conn.receive_json()

        assert message["payload"]["state"] is True


def test_an_out_of_range_state_value_is_dropped(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An out-of-range state value is dropped."""
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "video_state", "state": 2})
        # Nothing broadcasts for the bad value; a well-formed follow-up message
        # proves the connection is still alive and the bad one wasn't queued.
        conn.send_json({"action": "video_state", "state": 0})
        message = conn.receive_json()

        assert message["payload"]["state"] == 0


def _invite(client: TestClient, controller_token: str, permission: str = "participant") -> str:
    """Take a seat through the controller's invite link and return the seat token."""
    response = client.post(
        f"{API}/session/invite?token={controller_token}", json={"permission": permission}
    )
    invite = response.json()["url"].split("invite=")[1]
    return client.get(f"{API}/session/context?invite={invite}").json()["userToken"]


def _wait_for_state(conn: WebSocketTestSession, predicate) -> dict:  # noqa: ANN001
    """Read room broadcasts until a state_update satisfies `predicate`, and return it."""
    while True:
        message = conn.receive_json()
        if message["type"] == "state_update" and predicate(message):
            return message


def test_an_invite_link_stays_valid_after_its_viewer_disconnects(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An invite link stays valid after its viewer disconnects.

    The seat is minted once and lives for the whole session: a reload, a closed tab or the room's
    own reconnect must land back in the same seat rather than on a "session does not exist" page.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, viewer):
        pass

    assert session.find_viewer(viewer) is not None
    assert client.get(f"{API}/session/context?token={viewer}").status_code == 200
    with _connect(client, viewer) as conn:
        conn.send_json({"action": "video_state", "state": 1})
        assert conn.receive_json()["payload"]["state"] == 1


def test_a_disconnecting_viewer_releases_its_gamepad_but_keeps_its_seat(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A disconnecting viewer releases its gamepad but keeps its seat.

    A pad nobody is driving goes back to the tray so the host can hand it on, while the seat (and
    the link to it) survives for when that person comes back.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    viewer_public_id = session.find_viewer(viewer)["public_id"]
    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        host.send_json({"action": "assign_slot", "viewer_public_id": viewer_public_id, "slot": 2})
        _wait_for_state(
            guest,
            lambda m: any(u["publicId"] == viewer_public_id and u["slot"] == 2 for u in m["viewers"]),
        )

    seat = session.find_viewer(viewer)
    assert seat is not None
    assert seat["slot"] is None


def test_a_seat_reclaimed_mid_handshake_gets_the_new_socket_closed(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seat reclaimed by a concurrent join while accept() is still pending closes the stale socket.

    accept() is an await point: a join at the room cap can reclaim this very seat while the
    handshake is in flight, so the seat is re-checked after accept() rather than trusting the lookup
    made before it.
    """
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)
    # Past the grace a just-minted seat gets, so the reclaim below is the one
    # the arrival would really make rather than one this test forces.
    session.find_viewer(viewer)["last_seen"] = 0.0

    original_accept = room.WebSocket.accept

    async def accept_then_reclaim(self: object, *args: object, **kwargs: object) -> None:
        await original_accept(self, *args, **kwargs)
        # A second arrival at the cap goes through the real reclaim path in
        # add_viewer, evicting `viewer`'s seat: it is still the only anonymous,
        # not-yet-online candidate at this point in the handshake.
        await session.add_viewer("participant", None)

    monkeypatch.setattr(room.WebSocket, "accept", accept_then_reclaim)

    with client.websocket_connect(f"{PREFIX}/ws/room?token={viewer}") as conn:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            conn.receive_json()
        assert excinfo.value.code == 1008

    assert session.find_viewer(viewer) is None


def test_state_update_never_carries_a_raw_token(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """state_update identifies members by publicId, never by their real bearer token.

    Every room member (including anonymous viewers) receives this broadcast, so a raw token in it
    would hand out the credential needed to reconnect as, or impersonate, anyone else in the room.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host, _connect(client, viewer):
        # The viewer's own _connect already drained its join broadcast; the host's
        # queue still holds the state_update that announced the viewer, now with
        # both members in it.
        message = _wait_for_state(host, lambda m: len(m["viewers"]) == 2)

        assert controller not in json.dumps(message)
        assert viewer not in json.dumps(message)
        for user in message["viewers"]:
            assert "token" not in user
            assert user["publicId"]


def test_resolution_update_and_control_messages_use_public_id_not_token(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """resolution_update and control messages identify the sender by public id, not raw token."""
    controller = _activate(client, broker_dirs)
    controller_public_id = session.public_id_for(controller)

    with _connect(client, controller) as conn:
        conn.send_json({"action": "client_resolution", "width": 1280, "height": 720})
        resolution_message = conn.receive_json()

        assert resolution_message["type"] == "resolution_update"
        assert resolution_message["public_id"] == controller_public_id
        assert controller not in json.dumps(resolution_message)

        conn.send_json({"action": "video_state", "state": 1})
        control_message = conn.receive_json()

        assert control_message["payload"]["sender_public_id"] == controller_public_id
        assert controller not in json.dumps(control_message)


def test_a_forged_media_sender_id_is_overwritten_before_relay(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A viewer's forged sender id in a media frame is overwritten with its own before relay.

    Recipients attribute an incoming video/audio frame to whichever publicId sits in its first 8
    bytes (see room.py's wire format docstring); trusting the sender's own bytes there would let
    any participant paint its frames onto another member's video tile.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        guest_media_id = session.ROOM["viewers"][viewer]["public_id"]
        forged = b"AAAAAAAA" + bytes([0x01]) + b"payload"
        guest.send_bytes(forged)

        while True:
            raw = host.receive()
            if "bytes" in raw:
                relayed = raw["bytes"]
                break

        assert relayed[:8] == guest_media_id.encode("ascii")
        assert relayed[8:] == forged[8:]


def test_designated_speaker_is_reported_as_a_public_id(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """The controller can name a viewer as designated speaker using only its public id.

    The controller never learns the viewer's real token (see test_state_update_never_carries_a_raw_token),
    so this is the only handle it has to target the viewer with.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)
    viewer_public_id = session.find_viewer(viewer)["public_id"]

    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        host.send_json({"action": "set_designated_speaker", "public_id": viewer_public_id})
        message = _wait_for_state(guest, lambda m: m["designated_speaker"] == viewer_public_id)

        assert message["designated_speaker"] == viewer_public_id
        assert viewer not in json.dumps(message)


def test_controller_can_assign_mk_to_a_viewer_using_its_public_id(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """The controller can hand mouse/keyboard control to a viewer using only its public id."""
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)
    viewer_public_id = session.find_viewer(viewer)["public_id"]

    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        host.send_json({"action": "assign_mk", "public_id": viewer_public_id})
        message = _wait_for_state(
            guest,
            lambda m: any(u["publicId"] == viewer_public_id and u["has_mk"] for u in m["viewers"]),
        )

        assert any(u["publicId"] == viewer_public_id and u["has_mk"] for u in message["viewers"])


def test_a_rejoin_closes_the_old_seats_still_connected_socket(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A same-user rejoin closes the old seat's socket rather than leaving it live on a dead token.

    Unlike a same-token reconnect, a rejoin mints a new token for a different seat: nothing else
    would ever close the old one, so it would otherwise keep relaying room traffic on a token
    `find_viewer` no longer recognizes.
    """
    controller = _activate(client, broker_dirs)
    user = {"id": 7, "username": "ana"}
    first = (
        client.post(f"{API}/session/join", json={"user": user, "permission": "participant"})
        .json()["url"]
        .split("token=")[1]
    )

    with _connect(client, controller) as host, _connect(client, first) as old:
        second = (
            client.post(f"{API}/session/join", json={"user": user, "permission": "participant"})
            .json()["url"]
            .split("token=")[1]
        )
        assert second != first
        # The evicted token's connection is detached the moment the join call
        # returns, before the socket close below is even awaited.
        assert first not in session.ROOM["viewers"]
        second_public_id = session.find_viewer(second)["public_id"]

        with pytest.raises(WebSocketDisconnect) as excinfo:
            old.receive_json()
        assert excinfo.value.code == 1008
        assert session.find_viewer(first) is None

        # The host and the new seat are the only two members left; the evicted
        # seat is gone from the roster with no departure notice for it.
        message = _wait_for_state(
            host, lambda m: any(u["publicId"] == second_public_id for u in m["viewers"])
        )
        assert len(message["viewers"]) == 2


def test_a_new_connection_on_the_same_token_replaces_the_old_one(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A new connection on the same token replaces the old one.

    The stale socket is closed and, since it was not the member leaving, it must not announce a
    departure or release what the live socket now holds.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host:
        with _connect(client, viewer) as first:
            with _connect(client, viewer) as second:
                with pytest.raises(WebSocketDisconnect):
                    first.receive_json()
                second.send_json({"action": "video_state", "state": 1})
                assert second.receive_json()["payload"]["state"] == 1

        # The host saw a join per socket but no departure from the replaced one.
        seen = []
        while not seen or seen[-1] != "control":
            seen.append(host.receive_json()["type"])
        assert seen.count("user_left") == 0


def _next_control(conn: WebSocketTestSession) -> dict:
    """Read room traffic until a control message arrives, and return its payload.

    A control message the sender knows will be relayed doubles as a barrier:
    once it lands, every frame sent before it has already been handled.

    Args:
        conn: The socket to read from.

    Returns:
        The control message's payload.
    """
    while True:
        message = conn.receive_json()
        if message["type"] == "control":
            return message["payload"]


def test_the_session_ending_closes_every_room_socket(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Ending the session closes the room's sockets instead of only forgetting them.

    A forgotten socket is still an open socket with its handler parked in receive() forever, so
    every connection of every ended session would outlive the session that owned it.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        client.portal.call(session.notify_session_ended)

        for conn in (host, guest):
            with pytest.raises(WebSocketDisconnect):
                while True:
                    conn.receive_json()

    assert session.ROOM["controller"] is None
    assert session.ROOM["viewers"] == {}


def test_a_frame_from_a_lingering_socket_never_lands_in_a_replaced_session(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A socket left over from a retired session writes nothing into the session that replaced it.

    The handler holds no reference to the session it opened on: it reads the live one per frame, so
    a retire (and an activate on top of it) closes the leftover socket rather than letting it keep
    setting roles on a session dict nothing serves.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)
    viewer_public_id = session.find_viewer(viewer)["public_id"]

    with _connect(client, controller) as host:
        retired = session.SESSION
        session.retire_session()
        _activate(client, broker_dirs)

        host.send_json({"action": "set_designated_speaker", "public_id": viewer_public_id})

        with pytest.raises(WebSocketDisconnect):
            host.receive_json()
        assert retired["designated_speaker"] is None
        assert session.SESSION["designated_speaker"] is None


def test_a_non_object_frame_is_dropped_without_dropping_the_connection(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A frame that is not a JSON object, or carries a null field, is dropped but the socket lives.

    Every handler below reads named keys off the frame, so anything else would raise into the
    handler's own catch-all and take a member's connection down on a message it chose to send.
    """
    token = _activate(client, broker_dirs)

    with _connect(client, token) as conn:
        for frame in ("5", "[]", '"hello"', "not json at all"):
            conn.send_text(frame)
        conn.send_json({"action": "send_chat_message", "message": None})
        conn.send_json({"action": "set_username", "username": None})
        conn.send_json({"action": None})

        conn.send_json({"action": "video_state", "state": 1})
        assert _next_control(conn)["state"] == 1


def test_a_gamepad_slot_outside_the_configured_range_is_refused(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A gamepad slot from the wire is only honoured inside settings.GAMEPAD_SLOTS.

    The slot goes into the token map selkies routes input by, so an out-of-range or non-numeric one
    would strand a pad on a slot no emulator has.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)
    viewer_public_id = session.find_viewer(viewer)["public_id"]

    with _connect(client, controller) as host:
        for slot in (settings.GAMEPAD_SLOTS + 1, 0, -1, "2", True, 1.5):
            host.send_json(
                {"action": "assign_slot", "viewer_public_id": viewer_public_id, "slot": slot}
            )
        host.send_json({"action": "video_state", "state": 1})
        assert _next_control(host)["state"] == 1

        assert session.find_viewer(viewer)["slot"] is None

        host.send_json(
            {
                "action": "assign_slot",
                "viewer_public_id": viewer_public_id,
                "slot": settings.GAMEPAD_SLOTS,
            }
        )
        _wait_for_state(
            host,
            lambda m: any(
                u["publicId"] == viewer_public_id and u["slot"] == settings.GAMEPAD_SLOTS
                for u in m["viewers"]
            ),
        )


def test_naming_an_unknown_member_as_speaker_leaves_the_restriction_alone(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A speaker request naming nobody in the room leaves the standing restriction in place.

    An unknown or departed public id resolves to None, which is also how the room says "anyone may
    speak", so honouring it would silently reopen audio to everyone.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)
    viewer_public_id = session.find_viewer(viewer)["public_id"]

    with _connect(client, controller) as host:
        host.send_json({"action": "set_designated_speaker", "public_id": viewer_public_id})
        _wait_for_state(host, lambda m: m["designated_speaker"] == viewer_public_id)

        host.send_json({"action": "set_designated_speaker", "public_id": "deadbeef"})
        host.send_json({"action": "video_state", "state": 1})
        assert _next_control(host)["state"] == 1

        assert session.SESSION["designated_speaker"] == viewer

        # An explicit null is the one way to clear it, and still works.
        host.send_json({"action": "set_designated_speaker", "public_id": None})
        _wait_for_state(host, lambda m: m["designated_speaker"] is None)
        assert session.SESSION["designated_speaker"] is None


def test_a_control_message_relays_only_the_fields_the_room_acts_on(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A control message is rebuilt from the fields the room acts on, not relayed as sent.

    The payload reaches every member, so forwarding the sender's own dict would carry every extra
    key it packed in, a spoofed sender identity included.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        guest.send_json(
            {
                "action": "audio_state",
                "state": 1,
                "sender_public_id": "forged00",
                "extra": {"anything": "at all"},
            }
        )
        payload = _next_control(host)

        assert payload == {
            "action": "audio_state",
            "state": 1,
            "sender_public_id": session.find_viewer(viewer)["public_id"],
        }


def test_a_seat_with_no_username_is_never_named_after_its_token(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A seat with no username of its own gets a generic name, never one built from its token.

    The name goes to the whole room in user_joined and state_update, and the token is the seat's
    bearer credential: any part of it in that name is a credential leak.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)
    del session.find_viewer(viewer)["username"]

    with client.websocket_connect(f"{PREFIX}/ws/room?token={viewer}") as conn:
        joined = conn.receive_json()
        state = conn.receive_json()

        assert joined["type"] == "user_joined"
        assert joined["username"] == "Viewer"
        assert viewer[:6] not in json.dumps([joined, state])


def test_state_update_never_carries_a_seats_romm_user_id(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """state_update carries no seat bookkeeping beyond what the room renders.

    Every member reads this broadcast, anonymous guests included, so a seat's RomM user id in it
    would tell the whole room which library account is behind each name.
    """
    controller = _activate(client, broker_dirs)
    joined = client.post(
        f"{API}/session/join",
        json={"permission": "participant", "user": {"id": 4242, "username": "bo"}},
    ).json()
    viewer = joined["url"].split("token=")[1]

    with _connect(client, controller) as host, _connect(client, viewer):
        message = _wait_for_state(host, lambda m: len(m["viewers"]) == 2)

        assert "4242" not in json.dumps(message)
        for user in message["viewers"]:
            assert set(user) == {
                "publicId",
                "username",
                "slot",
                "permission",
                "has_mk",
                "online",
                "mediaId",
            }


def test_a_media_frame_shorter_than_its_header_is_dropped_with_a_log(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A media frame too short to carry its own header is dropped, and says so in the log."""
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        with caplog.at_level(logging.WARNING, logger="webstation_broker.room"):
            guest.send_bytes(b"\x01" * (room.MEDIA_HEADER_BYTES - 1))
            guest.send_json({"action": "video_state", "state": 1})

            relayed = []
            while True:
                message = host.receive()
                if "bytes" in message:
                    relayed.append(message["bytes"])
                    continue
                if json.loads(message["text"])["type"] == "control":
                    break

        assert relayed == []
        assert "shorter than its" in caplog.text


def test_a_connection_whose_media_id_is_the_wrong_width_is_refused(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection whose media id is not the width the wire format carries is refused.

    Recipients read the frame type from the byte straight after the id, so an id of any other width
    would put every frame's type byte at an offset nothing parses.
    """
    token = _activate(client, broker_dirs)
    monkeypatch.setattr(session, "new_public_id", lambda: "too-short")

    with client.websocket_connect(f"{PREFIX}/ws/room?token={token}") as conn:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            conn.receive_json()

        assert excinfo.value.code == 1011


def test_a_client_resolution_is_only_relayed_as_a_plausible_pixel_count(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A self-reported resolution is relayed only when both dimensions are plausible pixel counts."""
    token = _activate(client, broker_dirs)

    with _connect(client, token) as conn:
        conn.send_json({"action": "client_resolution", "width": {"x": 1}, "height": "720"})
        conn.send_json({"action": "client_resolution", "width": 1280, "height": 720})

        message = conn.receive_json()

        assert message["type"] == "resolution_update"
        assert (message["width"], message["height"]) == (1280, 720)


def test_a_chat_message_carries_the_senders_public_id(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A relayed chat message names its sender by public id as well as by display name.

    Usernames are member-chosen and repeatable, so a client deciding whose message is whose by
    comparing them would let one member wear another's identity in the transcript.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        guest.send_json({"action": "send_chat_message", "message": "hello"})
        while True:
            message = host.receive_json()
            if message["type"] == "chat_message":
                break

    assert message["senderPublicId"] == session.find_viewer(viewer)["public_id"]
    assert viewer not in json.dumps(message)


_SLOW_DELIVERY_WAIT = 1.0
"""Seconds a healthy member waits for a frame that a slow member must not hold up."""


def _stall_sends(token: str, released: threading.Event) -> None:
    """Make one member's socket a slow consumer: every send to it parks until `released` is set.

    A thread event, polled, so the test thread can release the parked sends.

    Args:
        token: The seat token of the member to slow down.
        released: Set by the test to let the parked sends finish.
    """
    ws = session.ROOM["viewers"][token]["websocket"]

    async def parked(*_args: object, **_kwargs: object) -> None:
        """Wait for the test to release the send."""
        while not released.is_set():
            await asyncio.sleep(0.01)

    ws.send_bytes = parked
    ws.send_json = parked


def _receive_within(conn: WebSocketTestSession, seconds: float, kind: str) -> Optional[Message]:
    """Read messages until one carries `kind` ("bytes" or a JSON `type`), or give up after `seconds`.

    `WebSocketTestSession.receive` has no timeout, and a read that never
    returns would hang the suite, so this reads the session's queue on its own
    portal under a deadline.

    Args:
        conn: The client-side session to read from.
        seconds: How long to wait in total.
        kind: "bytes" for a media frame, otherwise the JSON message `type` to wait for.

    Returns:
        The matching message, or None when the deadline passed first.
    """

    async def read() -> Optional[Message]:
        """Drain the queue under the deadline until a match turns up."""
        with anyio.move_on_after(seconds):
            while True:
                message = await conn._send_rx.receive()
                if kind == "bytes" and "bytes" in message:
                    return message
                text = message.get("text")
                if kind != "bytes" and text and json.loads(text).get("type") == kind:
                    return message
        return None

    return conn.portal.call(read)


def _video_frame(body: bytes) -> bytes:
    """Build an unstamped video frame as a client sends it.

    Args:
        body: The encoded payload.

    Returns:
        The frame in the room's binary wire format.
    """
    return b"AAAAAAAA" + bytes([outbox.VIDEO_FRAME]) + body


def test_a_slow_viewer_does_not_hold_up_the_senders_next_media_frame(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """One viewer on a stalled link must not freeze a sender's video for everyone else."""
    controller = _activate(client, broker_dirs)
    healthy = _invite(client, controller)
    slow = _invite(client, controller)
    released = threading.Event()

    with _connect(client, controller) as host, _connect(client, healthy) as ok, _connect(client, slow):
        _stall_sends(slow, released)
        try:
            host.send_bytes(_video_frame(b"frame-1"))
            first = _receive_within(ok, _SLOW_DELIVERY_WAIT, "bytes")
            host.send_bytes(_video_frame(b"frame-2"))
            second = _receive_within(ok, _SLOW_DELIVERY_WAIT, "bytes")
        finally:
            released.set()

    assert first is not None and first["bytes"].endswith(b"frame-1")
    assert second is not None and second["bytes"].endswith(b"frame-2")


def test_a_slow_viewer_does_not_hold_up_the_senders_chat(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """One viewer on a stalled link must not stall the sender's chat to everyone else."""
    controller = _activate(client, broker_dirs)
    healthy = _invite(client, controller)
    slow = _invite(client, controller)
    released = threading.Event()

    with _connect(client, controller) as host, _connect(client, healthy) as ok, _connect(client, slow):
        _stall_sends(slow, released)
        try:
            host.send_bytes(_video_frame(b"frame-1"))
            host.send_json({"action": "send_chat_message", "message": "still here"})
            chat = _receive_within(ok, _SLOW_DELIVERY_WAIT, "chat_message")
        finally:
            released.set()

    assert chat is not None and json.loads(chat["text"])["message"] == "still here"


def test_a_viewer_that_stops_reading_leaves_the_room(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A laggard the outbox gives up on is dropped from the room, not left holding its seat's connection."""
    monkeypatch.setattr(outbox, "SEND_STALL_LIMIT", 0.1)
    controller = _activate(client, broker_dirs)
    slow = _invite(client, controller)
    released = threading.Event()

    with _connect(client, controller) as host, _connect(client, slow):
        _stall_sends(slow, released)
        try:
            host.send_json({"action": "send_chat_message", "message": "anyone there"})
            left = _receive_within(host, _SLOW_DELIVERY_WAIT * 3, "user_left")
            still_seated = slow in session.ROOM["viewers"]
        finally:
            released.set()

    assert left is not None
    assert not still_seated


class _SilentSocket:
    """A room socket whose client never sends anything."""

    async def receive(self) -> Message:
        """Wait forever."""
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_the_reader_stops_a_parked_read_once_the_outbox_gives_up() -> None:
    """A read waiting on a silent client returns None when `abandoned` is set."""
    abandoned = asyncio.Event()
    reader = room._RoomReader(cast(WebSocket, _SilentSocket()), abandoned)
    read = asyncio.ensure_future(asyncio.wait_for(reader.receive(), 1.0))
    await asyncio.sleep(0)
    abandoned.set()
    try:
        assert await read is None
    finally:
        reader.close()


async def test_the_reader_lets_an_outside_cancel_through() -> None:
    """A cancel that did not come from the outbox giving up still cancels the handler."""
    abandoned = asyncio.Event()

    async def handler() -> None:
        reader = room._RoomReader(cast(WebSocket, _SilentSocket()), abandoned)
        try:
            await reader.receive()
        finally:
            reader.close()

    task = asyncio.ensure_future(handler())
    await asyncio.sleep(0)
    abandoned.set()
    await asyncio.sleep(0)
    assert task.cancelling() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_reader_refuses_a_read_from_another_task() -> None:
    """Once bound to the task that first read it, the reader will not serve another."""
    abandoned = asyncio.Event()
    abandoned.set()
    reader = room._RoomReader(cast(WebSocket, _SilentSocket()), abandoned)
    try:
        assert await reader.receive() is None
        with pytest.raises(RuntimeError):
            await asyncio.ensure_future(reader.receive())
    finally:
        reader.close()
