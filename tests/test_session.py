"""Session bookkeeping, viewer roster, and the selkies token map.

Covers session id sanitising, the session lifecycle, viewer seats and the token map selkies routes
on.
"""

from typing import Any, Optional

import pytest

from webstation_broker import selkies, session, settings

from .conftest import FakeEmulator

REAL_PUSH_TOKENS = selkies.push_tokens
"""The real push, captured before conftest's autouse stub replaces it for every test."""


def _activate(session_id: str = "sess-1", user: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Start a session on a FakeEmulator with a minimal activate body.

    Args:
        session_id: The id RomM hands over.
        user: The controller's user record, empty when omitted.

    Returns:
        The session record new_session built.
    """
    return session.new_session(
        {"session_id": session_id, "emulator": "fake", "user": user or {}, "rom": {"name": "Game"}},
        FakeEmulator(),
        "/romm/roms/ps2/Game.iso",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sess-1", "sess-1"),
        ("../../etc/passwd", "etcpasswd"),
        ("a b.c/d", "abcd"),
        ("Session_42", "Session_42"),
    ],
)
def test_the_session_id_keeps_nothing_that_could_shape_a_path(raw: str, expected: str) -> None:
    """The session id keeps nothing that could shape a path."""
    assert session._session_id(raw) == expected


def test_an_unusable_session_id_falls_back_to_a_generated_one() -> None:
    """An unusable session id falls back to a generated one."""
    for raw in (None, "", "///", "..."):
        generated = session._session_id(raw)
        assert generated and generated.isalnum()


def test_the_session_id_is_bounded() -> None:
    """The session id is bounded to 64 characters."""
    assert len(session._session_id("x" * 500)) == 64


def test_activating_drops_the_record_of_the_session_that_just_exited() -> None:
    """Activating drops the record of the session that just exited."""
    _activate()
    session.retire_session()
    assert session.LAST_EXIT is not None

    _activate("sess-2")

    assert session.LAST_EXIT is None


def test_retiring_keeps_the_emulator_the_state_routes_still_have_to_read() -> None:
    """Retiring keeps the emulator the state routes still have to read."""
    sess = _activate()
    emulator = sess["emulator_obj"]

    session.retire_session()

    assert session.SESSION is None
    assert session.LAST_EXIT["emulator_obj"] is emulator
    assert session.LAST_EXIT["id"] == "sess-1"


async def test_a_viewer_gets_a_token_that_resolves_back_to_them() -> None:
    """A viewer gets a token that resolves back to them."""
    _activate()

    viewer = await session.add_viewer("readonly", {"id": 7, "username": "ana"})

    assert session.find_viewer(viewer["token"]) == viewer
    assert viewer["permission"] == "readonly"
    assert viewer["username"] == "ana"
    assert viewer["public_id"]
    assert viewer["public_id"] != viewer["token"]


def test_public_id_for_and_resolve_public_id_round_trip_for_the_controller() -> None:
    """A public id round-trips back to the controller's token, and vice versa."""
    sess = _activate()

    public_id = session.public_id_for(sess["controller_token"])

    assert public_id == sess["controller_public_id"]
    assert public_id != sess["controller_token"]
    assert session.resolve_public_id(public_id) == sess["controller_token"]


async def test_public_id_for_and_resolve_public_id_round_trip_for_a_viewer() -> None:
    """A public id round-trips back to a viewer's token, and vice versa."""
    _activate()
    viewer = await session.add_viewer("participant", {"id": 7, "username": "ana"})

    public_id = session.public_id_for(viewer["token"])

    assert public_id == viewer["public_id"]
    assert session.resolve_public_id(public_id) == viewer["token"]


async def test_resolve_public_id_returns_none_for_an_unknown_or_missing_id() -> None:
    """An unrecognized or missing public id resolves to no token, not a stale match."""
    _activate()
    await session.add_viewer("participant", {"id": 7, "username": "ana"})

    assert session.resolve_public_id("not-a-real-id") is None
    assert session.resolve_public_id(None) is None


def test_public_id_for_returns_none_for_an_unknown_token() -> None:
    """An unrecognized token has no public id to hand back."""
    _activate()

    assert session.public_id_for("not-a-real-token") is None


def test_public_id_helpers_return_none_when_there_is_no_session() -> None:
    """Both public id helpers return None rather than raising with no active session."""
    sess = _activate()
    controller_token = sess["controller_token"]
    session.retire_session()

    assert session.public_id_for(controller_token) is None
    assert session.resolve_public_id("anything") is None


async def test_rejoining_replaces_the_entry_and_kills_the_old_token() -> None:
    """Rejoining replaces the entry and kills the old token."""
    _activate()
    first = await session.add_viewer("participant", {"id": 7, "username": "ana"})

    second = await session.add_viewer("participant", {"id": 7, "username": "ana"})

    assert len(session.SESSION["viewers"]) == 1
    assert second["token"] != first["token"]
    assert session.find_viewer(first["token"]) is None


async def test_rejoining_clears_the_old_tokens_speaker_and_mk_role() -> None:
    """A rejoin that replaces a seat clears the old token's role, since it is never disconnected."""
    sess = _activate()
    first = await session.add_viewer("participant", {"id": 7, "username": "ana"})
    sess["designated_speaker"] = first["token"]
    sess["mk_owner_token"] = first["token"]

    await session.add_viewer("participant", {"id": 7, "username": "ana"})

    assert sess["designated_speaker"] is None
    assert sess["mk_owner_token"] is None


async def test_rejoining_drops_the_old_tokens_rate_limit_cooldowns() -> None:
    """A rejoin that replaces a seat also drops the old token's cooldowns entry."""
    _activate()
    first = await session.add_viewer("participant", {"id": 7, "username": "ana"})
    session.ROOM["cooldowns"][first["token"]] = {"chat": 1.0}

    await session.add_viewer("participant", {"id": 7, "username": "ana"})

    assert first["token"] not in session.ROOM["cooldowns"]


def test_release_seat_pops_and_returns_the_tokens_live_connection() -> None:
    """`_release_seat` pops and returns a token's live connection, so its caller can close it."""
    _activate()
    sentinel = object()
    session.ROOM["viewers"]["tok"] = {"websocket": sentinel}

    live = session._release_seat("tok")

    assert live is not None
    assert live["websocket"] is sentinel
    assert "tok" not in session.ROOM["viewers"]


def test_release_seat_returns_none_for_a_token_with_no_live_connection() -> None:
    """`_release_seat` returns None for a token that was never connected, or already dropped."""
    _activate()

    assert session._release_seat("never-connected") is None


async def test_a_rejoin_without_an_id_is_matched_on_the_name() -> None:
    """A rejoin without an id is matched on the name."""
    _activate()
    await session.add_viewer("participant", {"username": "ana"})

    await session.add_viewer("participant", {"username": "ana"})

    assert len(session.SESSION["viewers"]) == 1


async def test_two_different_users_both_keep_a_seat() -> None:
    """Two different users both keep a seat."""
    _activate()
    await session.add_viewer("participant", {"id": 7, "username": "ana"})
    await session.add_viewer("participant", {"id": 8, "username": "bo"})

    assert len(session.SESSION["viewers"]) == 2


async def test_an_anonymous_viewer_still_gets_a_name() -> None:
    """An anonymous viewer still gets a name."""
    _activate()

    viewer = await session.add_viewer("participant", None)

    assert viewer["username"].startswith("User-")


async def test_a_seat_past_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seat past the cap is refused when nothing is reclaimable."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate()
    await session.add_viewer("participant", {"id": 7, "username": "ana"})

    refused = await session.add_viewer("participant", {"id": 8, "username": "bo"})

    assert refused is None
    assert len(session.SESSION["viewers"]) == 1


async def test_a_disconnected_anonymous_seat_is_reclaimed_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disconnected anonymous seat is reclaimed to seat a new arrival at the cap."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate()
    ghost = await session.add_viewer("participant", None)
    ghost["last_seen"] = 0.0  # long past the grace a just-minted seat gets

    arrival = await session.add_viewer("participant", None)

    assert arrival is not None
    assert arrival["token"] != ghost["token"]
    assert len(session.SESSION["viewers"]) == 1
    assert session.find_viewer(ghost["token"]) is None


async def test_a_connected_anonymous_seat_is_never_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connected anonymous seat is not reclaimed even at the cap."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate()
    online = await session.add_viewer("participant", None)
    session.ROOM["viewers"][online["token"]] = {"websocket": object()}

    refused = await session.add_viewer("participant", None)

    assert refused is None
    assert session.find_viewer(online["token"]) is not None


async def test_a_named_seat_is_never_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named user's seat is never reclaimed, even disconnected and at the cap."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate()
    named = await session.add_viewer("participant", {"id": 7, "username": "ana"})

    refused = await session.add_viewer("participant", None)

    assert refused is None
    assert session.find_viewer(named["token"]) is not None


async def test_a_named_by_username_only_seat_is_never_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user with only a username and no id is still named, not anonymous, so its seat holds at the cap."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate()
    named = await session.add_viewer("participant", {"username": "ana"})

    refused = await session.add_viewer("participant", None)

    assert refused is None
    assert session.find_viewer(named["token"]) is not None


async def test_reclaiming_a_seat_clears_its_speaker_and_mk_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reclaiming a seat that was still wired up as speaker or MK owner clears those session fields."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    sess = _activate()
    ghost = await session.add_viewer("participant", None)
    ghost["last_seen"] = 0.0  # long past the grace a just-minted seat gets
    sess["designated_speaker"] = ghost["token"]
    sess["mk_owner_token"] = ghost["token"]

    await session.add_viewer("participant", None)

    assert sess["designated_speaker"] is None
    assert sess["mk_owner_token"] is None


async def test_reclaiming_a_seat_leaves_another_members_speaker_and_mk_role_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reclaiming an idle seat does not clear the speaker or MK role of a different, surviving member."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 2)
    sess = _activate()
    holder = await session.add_viewer("participant", None)
    ghost = await session.add_viewer("participant", None)
    ghost["last_seen"] = 0.0  # guarantee ghost, not holder, is the idle one reclaimed
    sess["designated_speaker"] = holder["token"]
    sess["mk_owner_token"] = holder["token"]

    await session.add_viewer("participant", None)

    assert session.find_viewer(ghost["token"]) is None
    assert session.find_viewer(holder["token"]) is not None
    assert sess["designated_speaker"] == holder["token"]
    assert sess["mk_owner_token"] == holder["token"]


async def test_the_longest_idle_anonymous_seat_is_reclaimed_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """The longest-idle anonymous seat is reclaimed first, regardless of mint order."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 2)
    _activate()
    minted_first = await session.add_viewer("participant", None)
    minted_second = await session.add_viewer("participant", None)
    minted_first["last_seen"] = 200.0
    minted_second["last_seen"] = 100.0  # idle longer despite minting later

    await session.add_viewer("participant", None)

    assert session.find_viewer(minted_first["token"]) is not None
    assert session.find_viewer(minted_second["token"]) is None


async def test_reclaiming_a_seat_drops_its_rate_limit_cooldowns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reclaiming a seat also drops its cooldowns entry, so it does not linger for the rest of the session."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate()
    ghost = await session.add_viewer("participant", None)
    ghost["last_seen"] = 0.0  # long past the grace a just-minted seat gets
    session.ROOM["cooldowns"][ghost["token"]] = {"chat": 1.0}

    await session.add_viewer("participant", None)

    assert ghost["token"] not in session.ROOM["cooldowns"]


async def test_a_freshly_minted_seat_is_not_reclaimed_ahead_of_a_long_idle_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-idle seat is reclaimed before one just minted, even though the fresh one hasn't connected."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 2)
    _activate()
    long_idle = await session.add_viewer("participant", None)
    long_idle["last_seen"] = 100.0

    fresh = await session.add_viewer("participant", None)
    await session.add_viewer("participant", None)

    assert session.find_viewer(long_idle["token"]) is None
    assert session.find_viewer(fresh["token"]) is not None


async def test_a_rejoin_at_the_cap_still_replaces_its_own_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejoin at the cap still replaces its own seat, since it frees the seat it re-takes."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate()
    first = await session.add_viewer("participant", {"id": 7, "username": "ana"})

    second = await session.add_viewer("participant", {"id": 7, "username": "ana"})

    assert second is not None
    assert second["token"] != first["token"]


async def test_the_controller_holds_mouse_and_keyboard_until_it_is_handed_over() -> None:
    """The controller holds mouse and keyboard until it is handed over."""
    sess = _activate()
    viewer = await session.add_viewer("participant", {"id": 7, "username": "ana"})

    tokens = selkies.build_token_map(sess)
    assert tokens[sess["controller_token"]]["mk_control"] is True
    assert tokens[viewer["token"]]["mk_control"] is False

    sess["mk_owner_token"] = viewer["token"]
    tokens = selkies.build_token_map(sess)
    assert tokens[sess["controller_token"]]["mk_control"] is False
    assert tokens[viewer["token"]]["mk_control"] is True


async def test_the_token_map_carries_the_gamepad_slot_selkies_routes_on() -> None:
    """The token map carries the gamepad slot selkies routes on."""
    sess = _activate()
    viewer = await session.add_viewer("participant", {"id": 7, "username": "ana"})
    viewer["slot"] = 2

    tokens = selkies.build_token_map(sess)

    assert tokens[sess["controller_token"]]["slot"] == 1
    assert tokens[viewer["token"]] == {"role": "viewer", "slot": 2, "mk_control": False}


async def test_a_seat_that_was_just_handed_out_is_never_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An arrival at the cap is refused rather than reclaiming a seat handed out moments ago.

    A seat is minted before its holder has had time to open the room socket, so reclaiming on
    "not connected" alone would let a burst of arrivals eat each other's seats and admit nobody.
    """
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate()
    fresh = await session.add_viewer("participant", None)

    refused = await session.add_viewer("participant", None)

    assert refused is None
    assert session.find_viewer(fresh["token"]) is not None


async def test_every_public_id_is_the_width_the_media_wire_format_carries() -> None:
    """Every public id is exactly as wide as the room's media frames assume.

    Room media frames carry the sender's id in a fixed-width prefix and read the frame type from
    the byte after it, so an id of any other width would misalign every frame for every recipient.
    """
    sess = _activate()
    viewer = await session.add_viewer("participant", {"id": 7, "username": "ana"})

    for public_id in (session.new_public_id(), sess["controller_public_id"], viewer["public_id"]):
        assert len(public_id.encode("ascii")) == session.PUBLIC_ID_HEX_CHARS


def _fake_httpx_client(accepting: set[str], attempted: list[str]) -> type:
    """Build a stand-in for `httpx.AsyncClient` that only accepts POSTs to `accepting`.

    Args:
        accepting: The endpoint URLs the stand-in answers successfully.
        attempted: List every attempted URL is recorded on, in order.

    Returns:
        A class usable in place of `httpx.AsyncClient`.
    """

    class _Response:
        """The bare slice of an httpx response the token push looks at."""

        def raise_for_status(self) -> None:
            """Accept the response, as a 2xx one does."""

    class _Client:
        """An async client that answers only the endpoints it was told to accept."""

        def __init__(self, **_kwargs: Any) -> None:
            """Ignore the client settings the real one takes.

            Args:
                _kwargs: Timeout and friends, unused here.
            """

        async def __aenter__(self) -> "_Client":
            """Enter the async context.

            Returns:
                The client itself.
            """
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            """Leave the async context without swallowing anything.

            Args:
                _exc: The exception triple, unused here.

            Returns:
                False, so any exception keeps propagating.
            """
            return False

        async def post(self, url: str, **_kwargs: Any) -> _Response:
            """Record the attempt and answer only for an accepted endpoint.

            Args:
                url: The endpoint being posted to.
                _kwargs: The body and headers, unused here.

            Returns:
                A stand-in response.

            Raises:
                OSError: When `url` is not one of the accepted endpoints.
            """
            attempted.append(url)
            if url not in accepting:
                raise OSError("connection refused")
            return _Response()

    return _Client


async def test_a_token_push_falls_through_to_the_next_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused endpoint is logged and the next candidate is tried, not left to fail silently."""
    attempted: list[str] = []
    monkeypatch.setattr(settings, "SELKIES_TOKEN_URLS", ["http://first/tokens", "http://second/tokens"])
    monkeypatch.setattr(selkies, "_active_url", None)
    monkeypatch.setattr(
        selkies.httpx, "AsyncClient", _fake_httpx_client({"http://second/tokens"}, attempted)
    )
    sess = _activate()

    assert await REAL_PUSH_TOKENS(sess) is True
    assert attempted == ["http://first/tokens", "http://second/tokens"]
    # Remembered so a steady-state push stops re-probing the endpoint that refused.
    assert selkies._active_url == "http://second/tokens"


async def test_a_token_push_that_fails_everywhere_is_logged_and_retried_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A sweep that fails everywhere retries the last-known-good endpoint and then says so.

    Input routing is left on the map selkies already has, so a push nobody is told about leaves a
    viewer holding a pad the room believes it took away.
    """
    attempted: list[str] = []
    monkeypatch.setattr(settings, "SELKIES_TOKEN_URLS", ["http://first/tokens", "http://second/tokens"])
    monkeypatch.setattr(selkies, "_active_url", None)
    monkeypatch.setattr(selkies, "RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(selkies.httpx, "AsyncClient", _fake_httpx_client(set(), attempted))
    sess = _activate()

    with caplog.at_level("WARNING", logger="webstation_broker.selkies"):
        assert await REAL_PUSH_TOKENS(sess) is False

    assert attempted == ["http://first/tokens", "http://second/tokens", "http://first/tokens"]
    assert "connection refused" in caplog.text
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_a_slot_change_selkies_refused_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A gamepad assignment selkies would not take is an input route the room only thinks it has."""

    async def _refuse(_session: dict[str, Any]) -> bool:
        """Fail the push the way a selkies that is down does.

        Args:
            _session: The session whose tokens would have been pushed.

        Returns:
            Always False.
        """
        return False

    monkeypatch.setattr(selkies, "push_tokens", _refuse)
    sess = _activate()
    viewer = await session.add_viewer("participant")

    with caplog.at_level("ERROR", logger="webstation_broker.session"):
        await session.handle_assign_slot(viewer["token"], 2)

    assert sess["viewers"][0]["slot"] == 2
    assert "selkies kept the old token map" in caplog.text
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_an_mk_handover_selkies_refused_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Mouse and keyboard control keeps routing to whoever held it, so a refused push has to say so."""

    async def _refuse(_session: dict[str, Any]) -> bool:
        """Fail the push the way a selkies that is down does.

        Args:
            _session: The session whose tokens would have been pushed.

        Returns:
            Always False.
        """
        return False

    monkeypatch.setattr(selkies, "push_tokens", _refuse)
    sess = _activate()
    viewer = await session.add_viewer("participant")

    with caplog.at_level("ERROR", logger="webstation_broker.session"):
        await session.handle_assign_mk(viewer["token"])

    assert sess["mk_owner_token"] == viewer["token"]
    assert "selkies kept the old token map" in caplog.text
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_a_session_with_no_bootable_rom_file_records_none() -> None:
    """Activate resolves no rom file for some launches, and the session has to carry that through."""
    sess = session.new_session(
        {"session_id": "sess-noboot", "emulator": "fake", "rom": {"name": "Game"}},
        FakeEmulator(),
        None,
    )

    assert sess["rom_file"] is None
