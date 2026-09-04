"""REST endpoints for the session lifecycle and the room frontend's bootstrap.

Covers activate, join, invite, exit and status, the save-state, disc-swap and
memory-card routes, the save archive import and export routes, and the context
endpoint the room frontend bootstraps from. All endpoints live under the
SUBFOLDER prefix; the RomM-facing ones require `X-Broker-Secret` when
`BROKER_SECRET` is set.
"""

import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from . import callback, memcard, saves, selkies, session, settings
from .emulators import get_emulator
from .emulators.base import Emulator, reap_orphan

log = logging.getLogger(__name__)
router = APIRouter()

TOKEN_CLEAR_GRACE_SECONDS = 1.5
"""Seconds the room gets between being told the session ended and losing its tokens.

Selkies errors on a connected client whose token vanished, so browsers are
given this long to tear the stream iframe down before the token set empties.
"""

_ACTIVATE_LOCK = threading.Lock()
"""Held for the whole of activate, so two launches never end up sharing one screen.

The 409 gate reads `session.SESSION`, but every step between that read and
`new_session` awaits, so without this two callers both pass the gate and both
launch an emulator. Taken without blocking: a second caller is told to come
back rather than queued behind a launch that can take a minute.
"""


class SaveIn(BaseModel):
    """Save data to restore before launch, and how the memory card travels.

    Attributes:
        archive: Container path of the save archive to restore, if any.
        resume_slot: State slot to resume from after launch, if any.
        memory_card_synced: Whether the caller synced the whole memory card separately.
    """

    archive: Optional[str] = None
    """Container path of the save archive to restore before the emulator boots, if any."""
    resume_slot: Optional[int] = None
    """State slot to load once the emulator is up, if any."""
    memory_card_synced: bool = False
    """Set when the caller syncs the whole memory card on the memory-card routes.

    The card then travels as its own image, so it comes out of the archive on
    both the restore and the dump.
    """


class RomIn(BaseModel):
    """The rom RomM resolved for the launch.

    Attributes:
        id: RomM's id for the rom, if known.
        name: The rom's display name, if known.
        platform: The platform slug, which general-purpose emulators use to pick a core.
        language: The rom's language, for emulators whose games ship several in one folder.
        path: Absolute container path to the rom, validated against ROM_ROOT on activate.
    """

    id: Optional[int] = None
    name: Optional[str] = None
    platform: Optional[str] = None
    language: Optional[str] = None
    """The rom's language, for a launcher whose games ship several in one folder.

    ScummVM detects one target per language in a game folder and the target is
    what boots, so without this a multilingual game starts in whichever
    language sorts first. Unknown or absent values leave the emulator on its
    own default rather than failing the launch.
    """
    path: str


class CallbackIn(BaseModel):
    """Where and how to push the exit save archive.

    Attributes:
        base_url: The parent's origin; derived from the request when omitted.
        token: The bearer token the parent expects on the push, if any.
    """

    base_url: Optional[str] = None
    token: Optional[str] = None


class JoinIn(BaseModel):
    """A user RomM is seating in the running session.

    Attributes:
        user: The RomM user record, if one is known.
        permission: Either `participant` or `readonly`.
    """

    user: Optional[dict[str, Any]] = None
    permission: str = "participant"


class InviteIn(BaseModel):
    """The permission an invite link grants to everyone who opens it.

    Attributes:
        permission: Either `participant` or `readonly`.
    """

    permission: str = "participant"


class StateIn(BaseModel):
    """The slot a save-state or load-state request names.

    Attributes:
        slot: The requested slot, resolved to the emulator's single working slot.
    """

    slot: int = Field(default=0, ge=0, le=10)
    """The requested slot, which the emulator resolves to its working slot.

    RomM is the library of states, so the emulator works in a single slot and
    resolves whatever is asked for to it. The bound is kept only to reject
    obvious garbage, and 0 is the other brokers' "use your default slot".
    """


class DiscIn(BaseModel):
    """The disc a swap-disc request points at.

    Attributes:
        path: Absolute container path to the disc file.
    """

    path: str
    """Absolute container path to the disc file, which RomM builds from the RomFile it resolved.

    Validated against ROM_ROOT the same way activate validates its rom path.
    """


class ActivateIn(BaseModel):
    """The launch request RomM sends to start a session.

    Attributes:
        session_id: RomM's id for the session, if it assigns one.
        user: The RomM user taking the controller seat, if known.
        emulator: The emulator name to launch.
        rom: The rom to boot; optional for launch types without a game.
        gui_language: The player's interface language, for emulators with a translated UI.
        save: Save data to restore before launch, if any.
        callback: Where the exit save archive goes, if not derived from the request.
        multiplayer: Whether RomM advertises the session for joining.
    """

    session_id: Optional[str] = None
    user: Optional[dict[str, Any]] = None
    emulator: str
    rom: Optional[RomIn] = None
    """The rom to boot. Optional for launch types without a game (e.g. emulator "desktop")."""
    gui_language: Optional[str] = None
    """The player's own interface language, from their RomM UI locale.

    Describes the player rather than the rom, so it is sent for a launch with
    no rom at all. An emulator with a translated interface follows it (ScummVM
    pins it in scummvm.ini), and a multilingual game folder falls back to it
    when the rom itself carries no language: it is the only thing that says
    which of several detected variants this player wants. Unknown or absent
    values leave the emulator on its own default rather than failing the launch.
    """
    save: Optional[SaveIn] = None
    callback: Optional[CallbackIn] = None
    multiplayer: bool = False
    """Whether the session is advertised for joining, decided once on RomM's launch screen.

    Fixed for the life of the session. It governs whether RomM advertises this
    session for joining and whether the room shows its comms surface while the
    host is alone. The invite route ignores it: a link always works.
    """


def _ct_eq(a: str, b: str) -> bool:
    """Compare two strings in constant time, tolerating non-ASCII input.

    Args:
        a: One side of the comparison.
        b: The other side.

    Returns:
        True when both sides encode to the same bytes.
    """
    return hmac.compare_digest(
        a.encode("utf-8", "replace"), b.encode("utf-8", "replace")
    )


def _check_secret(header_value: Optional[str]) -> None:
    """Reject the request unless it carries the configured broker secret.

    A no-op when `BROKER_SECRET` is unset.

    Args:
        header_value: The `X-Broker-Secret` header as received, or None when absent.

    Raises:
        HTTPException: 403 when a secret is configured and the header is missing or wrong.
    """
    if not settings.BROKER_SECRET:
        return
    if not header_value or not _ct_eq(header_value, settings.BROKER_SECRET):
        raise HTTPException(status_code=403, detail="bad broker secret")


def _landing_url(token: str) -> str:
    """Build the room landing URL that carries the given seat token.

    Args:
        token: The controller or viewer token to embed in the query string.

    Returns:
        The prefixed landing path with the token as its `token` query parameter.
    """
    return f"{settings.PREFIX}/?token={token}"


def _check_callback_scheme(base_url: str) -> None:
    """Reject anything but http(s) for a callback base_url.

    Defense in depth: this route already requires the caller to hold
    BROKER_SECRET, but the callback base_url itself is otherwise unrestricted
    (split-origin deployments need it to point anywhere), so without this a
    malicious base_url could turn the exit-save upload into a file:// read or
    point it at a non-HTTP internal service.

    Args:
        base_url: The callback base_url to validate.

    Raises:
        HTTPException: When the scheme is not http or https.
    """
    scheme = urlsplit(base_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise HTTPException(
            status_code=422, detail=f"callback.base_url must be http(s): {base_url!r}"
        )


def _resolve_callback(body_cb: Optional[CallbackIn], request: Request) -> dict[str, Any]:
    """Decide where the exit save archive goes.

    Same-origin deployments don't need to send a base_url: the broker sits
    under the parent's SUBFOLDER, so the origin that served the activate
    request IS the parent. An explicit base_url still wins for split-origin
    setups.

    Args:
        body_cb: The callback block from the activate body, if one was sent.
        request: The activate request, whose forwarded headers name the parent origin.

    Returns:
        A dict with `base_url`, `token` and `derived`, the last True when the
        origin was taken from the request rather than the body.
    """
    if body_cb and body_cb.base_url:
        _check_callback_scheme(body_cb.base_url)
        return {"base_url": body_cb.base_url, "token": body_cb.token, "derived": False}
    proto = (
        request.headers.get("x-forwarded-proto") or request.url.scheme
    ).split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    ).split(",")[0].strip()
    return {
        "base_url": f"{proto}://{host}",
        "token": body_cb.token if body_cb else None,
        "derived": True,
    }


def _archive_subtrees(
    emulator: Emulator, memory_card_synced: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the save subtrees this session ships, and the ones it leaves alone.

    With the whole card synced, leaving it in the archive too would have the
    restore and the card hydrate writing over each other, and a stale card
    inside an older archive would land on top of the one RomM just laid down.
    The excluded set is returned rather than simply dropped because archives
    RomM took before the card was synced still carry it, and a restore has to
    pass those members over instead of refusing the whole archive.

    Args:
        emulator: The emulator whose save layout is being consulted.
        memory_card_synced: Whether the caller synced the whole memory card separately.

    Returns:
        A pair of tuples: the subtrees to include in the archive, and the ones
        to pass over on a restore and skip on a dump.
    """
    if not memory_card_synced or emulator.memory_card_subtree is None:
        return emulator.save_subtrees, ()
    card = emulator.memory_card_subtree
    return tuple(s for s in emulator.save_subtrees if s != card), (card,)


def _archive_identity(sess: dict[str, Any], emulator: Emulator) -> dict[str, Any]:
    """Session identity for the dump archive's manifest.

    Args:
        sess: The active session record.
        emulator: The emulator that just exited.

    Returns:
        The emulator, its core (for a launcher that fronts many), the platform,
        the ROM's RomM id, name and file, and the state slot the archive was
        taken in.
    """
    rom = sess.get("rom") or {}
    return {
        "emulator": emulator.name,
        "core": emulator.archive_core(),
        "platform": rom.get("platform"),
        "rom_id": rom.get("id"),
        "rom": rom.get("name"),
        "rom_file": sess.get("rom_file"),
        "state_slot": emulator.state_slot if emulator.supports_states else None,
    }


async def _push_seat_tokens(sess: dict[str, Any], operation: str) -> bool:
    """Publish the session's seat tokens to selkies and say whether it took them.

    A dropped push is not cosmetic: a seat whose token selkies never learned
    cannot open the stream at all. The push itself already retries, so the job
    here is to name the session and the operation it was for, and to hand the
    outcome back for the route to report.

    Args:
        sess: The session whose token map is pushed.
        operation: What the push is for, e.g. `activate`, named in the failure log.

    Returns:
        True when selkies accepted the push.
    """
    if await selkies.push_tokens(sess):
        return True
    log.error(
        "selkies token push failed (%s, session %s): seats cannot reach the stream",
        operation,
        sess["id"],
    )
    return False


async def _clear_seat_tokens(session_id: str) -> bool:
    """Empty the selkies token set and say whether it took.

    A token set that survives its session keeps every seat's stream credential
    live against whatever runs next, so the outcome is logged and reported
    rather than dropped on the floor.

    Args:
        session_id: The session being retired, named in the failure log.

    Returns:
        True when selkies accepted the empty set.
    """
    if await selkies.clear_tokens():
        return True
    log.error(
        "selkies token clear failed (session %s): stream tokens may still be live",
        session_id,
    )
    return False


async def _send_to_controller(payload: dict[str, Any]) -> None:
    """Send a JSON message to the controller's room socket and no one else.

    The room fanout reaches anonymous invite guests too, so anything naming a
    container path, a callback URL or a raw error goes through here instead.

    Args:
        payload: The JSON-serializable message, normally carrying a `type` key.
    """
    conn = session.ROOM.get("controller")
    if not conn:
        return
    ws = conn["websocket"]
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.send_json(payload)
    except Exception as exc:
        log.warning("controller send failed: %s", exc)


@router.get("/api/health")
async def health() -> dict[str, str]:
    """Report that the broker is up."""
    return {"status": "ok"}


@router.post("/api/session/activate")
async def activate(
    body: ActivateIn,
    request: Request,
    x_broker_secret: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Start a session: restore save data, launch the emulator and mint the controller seat.

    Serialized on `_ACTIVATE_LOCK`, so a second caller arriving mid-launch is
    refused rather than left to start an emulator over the top of the first.

    Args:
        body: The launch request: emulator, rom, save data, callback and the multiplayer flag.
        request: The incoming request, used to derive the callback origin.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        The launch report from `_start_session`.

    Raises:
        HTTPException: 403 on a bad secret; 409 when a session is already active
            or another activate is still running; everything else `_start_session` raises.
    """
    _check_secret(x_broker_secret)
    if not _ACTIVATE_LOCK.acquire(blocking=False):
        log.warning("activate refused: another activate is still in progress")
        raise HTTPException(
            status_code=409, detail="another activate is already in progress"
        )
    try:
        return await _start_session(body, request)
    finally:
        _ACTIVATE_LOCK.release()


async def _start_session(body: ActivateIn, request: Request) -> dict[str, Any]:
    """Do the launch itself, with the activate lock already held.

    Any emulator left behind by an earlier broker process is reaped first. The
    save archive is located and read before the working slot is emptied, so a
    request that turns out to name an archive that is not there leaves the slot
    as it found it. The restore is then extracted into the emptied slot before
    the emulator boots, so the restore rather than the previous session decides
    what is in it, and the seat tokens are pushed to selkies once the emulator
    is launching.

    Args:
        body: The launch request: emulator, rom, save data, callback and the multiplayer flag.
        request: The incoming request, used to derive the callback origin.

    Returns:
        A dict with `status`, `session_id`, `rom_file`, `save_restore` (the
        extraction report, or None when nothing was restored),
        `selkies_tokens_pushed` and the controller's landing `url`.

    Raises:
        HTTPException: 409 when a session is already active; 422 for an unknown
            emulator, a missing rom on an emulator that needs one, no bootable
            file, or a failed restore; 400 for a rom path that cannot be
            resolved or lies outside ROM_ROOT; 404 for a rom path or save
            archive that does not exist.
    """
    if session.SESSION is not None and session.SESSION.get("active"):
        raise HTTPException(
            status_code=409,
            detail="a session is already active; exit it before activating a new one",
        )

    # No session is active, so any emulator still running was left behind by an
    # earlier broker process. Killing it here is the only way it ever gets
    # killed: nothing else holds a handle on it, and launching over the top
    # would leave two emulators sharing the screen and the audio sink.
    await anyio.to_thread.run_sync(reap_orphan)

    emulator = get_emulator(body.emulator)
    if emulator is None:
        raise HTTPException(status_code=422, detail=f"unknown emulator: {body.emulator}")
    # General-purpose emulators (retroarch) pick their core from the platform,
    # and a multilingual folder (ScummVM) picks its target from the language.
    # gui_language describes the player, not the rom, so it is set for a launch
    # that carries no rom at all.
    emulator.gui_language = body.gui_language
    if body.rom is not None:
        emulator.platform = body.rom.platform
        emulator.language = body.rom.language

    rom_file = None
    if emulator.requires_rom:
        if body.rom is None:
            raise HTTPException(
                status_code=422, detail=f"emulator {body.emulator} requires a rom"
            )
        try:
            rom_path = Path(body.rom.path).resolve()
        except OSError:
            raise HTTPException(status_code=400, detail="invalid rom path")
        rom_root = settings.rom_root()
        if not rom_path.is_relative_to(rom_root):
            raise HTTPException(
                status_code=400, detail=f"rom path must live under {rom_root}"
            )
        if not rom_path.exists():
            raise HTTPException(status_code=404, detail="rom path does not exist")
        rom_file = emulator.resolve_rom_file(rom_path)
        if rom_file is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "no bootable file found",
                    "extensions": list(emulator.rom_extensions),
                },
            )

    save = body.save
    subtrees, excluded = _archive_subtrees(
        emulator, bool(save and save.memory_card_synced)
    )
    # Read before the working slot is emptied: an archive that is not there
    # has to fail with the slot still holding whatever it held.
    content = None
    if save and save.archive and subtrees:
        archive_path = Path(save.archive)
        if not archive_path.is_file():
            log.warning("activate: save archive not found: %s", save.archive)
            raise HTTPException(
                status_code=404, detail=f"save archive not found: {save.archive}"
            )
        content = await anyio.to_thread.run_sync(archive_path.read_bytes)

    await anyio.to_thread.run_sync(emulator.clear_working_slot)
    restore_report = None
    if content is not None:
        await anyio.to_thread.run_sync(emulator.prepare_restore)
        restore_report = await anyio.to_thread.run_sync(
            saves.extract_save_archive,
            content,
            emulator.save_root,
            subtrees,
            excluded,
        )
        if restore_report["error"]:
            raise HTTPException(
                status_code=422, detail=f"save restore failed: {restore_report['error']}"
            )
        log.info("save restore: %s", restore_report)

    payload = body.model_dump()
    payload["callback"] = _resolve_callback(body.callback, request)
    sess = session.new_session(
        payload, emulator, str(rom_file) if rom_file else None
    )

    log.info(
        "session %s started: emulator=%s multiplayer=%s",
        sess["id"],
        body.emulator,
        sess["multiplayer"],
    )

    resume_slot = save.resume_slot if save else None
    try:
        await anyio.to_thread.run_sync(emulator.launch, rom_file, resume_slot)
    except Exception:
        # Otherwise the session stays marked active with no emulator behind
        # it, and every retry 409s instead of reaching launch again.
        log.error("session %s: launch failed, retiring the session", sess["id"], exc_info=True)
        session.retire_session()
        raise
    # Baseline after launch: the exit dump only ships files this session wrote.
    sess["save_baseline"] = time.time()

    tokens_pushed = await _push_seat_tokens(sess, "activate")

    return {
        "status": "launching",
        "session_id": sess["id"],
        "rom_file": str(rom_file) if rom_file else None,
        "save_restore": restore_report,
        "selkies_tokens_pushed": tokens_pushed,
        "url": _landing_url(sess["controller_token"]),
    }


async def _mint_viewer(
    permission: str, user: Optional[dict[str, Any]]
) -> tuple[dict[str, Any], bool]:
    """Add a seat to the running session and publish it to the control plane.

    Shared by the RomM-facing join route and the controller's invite route so
    token creation, the selkies push and the room broadcast stay in one place.

    Args:
        permission: Either `participant` or `readonly`.
        user: The RomM user taking the seat, or None for an anonymous invite.

    Returns:
        The viewer record, including its `token` and `username`, and whether
        selkies took the new token set. A seat selkies never learned about is
        still a seat, but it cannot open the stream until a later push lands.

    Raises:
        HTTPException: 409 when no session is active; 422 for an unknown
            permission; 429 when the room is already at its seat cap.
    """
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session to join")

    if permission not in ("participant", "readonly"):
        raise HTTPException(
            status_code=422, detail="permission must be participant or readonly"
        )

    viewer = await session.add_viewer(permission, user)
    if viewer is None:
        raise HTTPException(status_code=429, detail="room is full")
    tokens_pushed = await _push_seat_tokens(sess, f"seat a {permission}")
    await session.broadcast_state()
    return viewer, tokens_pushed


@router.post("/api/session/join")
async def join(body: JoinIn, x_broker_secret: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Add a user to the running session.

    Membership policy belongs to the caller; whoever is sent gets a personal
    token and landing URL.

    Args:
        body: The user to seat and the permission they get.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `status`, `session_id`, `permission`, `username`,
        `selkies_tokens_pushed` and the seat's landing `url`.

    Raises:
        HTTPException: 403 on a bad secret; 409 when no session is active; 422
            for an unknown permission; 429 when the room is already at its
            seat cap.
    """
    _check_secret(x_broker_secret)

    viewer, tokens_pushed = await _mint_viewer(body.permission, body.user)
    sess = session.SESSION
    return {
        "status": "joined",
        "session_id": sess["id"],
        "permission": body.permission,
        "username": viewer["username"],
        "selkies_tokens_pushed": tokens_pushed,
        "url": _landing_url(viewer["token"]),
    }


@router.post("/api/session/invite")
async def invite(body: InviteIn, token: str = Query()) -> dict[str, Any]:
    """Return the session's shareable invite link for a permission.

    The link is the same on every call for the session and seats no one by
    itself: each person who opens it is given their own seat by the context
    route, so the host sends one link to everyone rather than juggling one per
    friend. The room frontend does not hold the broker secret, so this is
    gated on the controller token the same way the exit route is. The
    session's multiplayer flag is deliberately not consulted: a link the host
    went out of their way to copy should work whichever way they set the
    switch.

    Args:
        body: The permission everyone arriving on the link gets.
        token: The controller token, passed as a query parameter.

    Returns:
        A dict with `status`, `session_id`, `permission` and the link's `url`.

    Raises:
        HTTPException: 409 when no session is active; 403 when the token is not
            the controller's; 422 for an unknown permission.
    """
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session")
    if not _ct_eq(token, sess["controller_token"]):
        raise HTTPException(status_code=403, detail="controller token required")
    if body.permission not in ("participant", "readonly"):
        raise HTTPException(
            status_code=422, detail="permission must be participant or readonly"
        )

    return {
        "status": "invited",
        "session_id": sess["id"],
        "permission": body.permission,
        "url": f"{settings.PREFIX}/?invite={session.invite_token(body.permission)}",
    }


def _exit_outcomes(
    upload: dict[str, Any],
    archive_path: Optional[str],
    cb: Optional[dict[str, Any]],
    dump_error: Optional[str],
) -> tuple[str, Optional[str]]:
    """Describe what became of the save archive, once for the room and once for the controller.

    The room summary reaches everyone seated, anonymous invite guests included,
    so it names no container path, no callback URL and no raw error text. The
    controller runs the session and is the one who has to act on a failure, so
    those details go to that seat alone.

    Args:
        upload: The upload report block, whose `mode` decides the wording.
        archive_path: Where the archive was kept on the container, if it was.
        cb: The session's callback block, if it has one.
        dump_error: Why the save dump failed, or None when it did not.

    Returns:
        The phrase for the room, and the controller-only line, the latter None
        when there is nothing further to tell them.
    """
    if dump_error:
        public = "the save dump failed, so nothing was uploaded"
        detail = f"Save dump failed: {dump_error}."
    elif upload["mode"] == "report-only":
        public = "nothing was uploaded (dev mode)"
        detail = f"Dev mode: would have uploaded to {(cb or {}).get('base_url')}."
    elif upload["mode"] == "uploaded":
        public = "saves uploaded"
        detail = f"Uploaded to {upload['url']}."
    elif upload["mode"] == "skipped":
        public = "nothing to upload"
        detail = None
    else:
        public = "the save upload failed, the archive was kept on the container"
        detail = f"Upload failed: {upload.get('error')}."
    if archive_path:
        kept = f"Archive kept at {archive_path}."
        detail = f"{detail} {kept}" if detail else kept
    return public, detail


async def _do_exit(save_slot: Optional[int]) -> dict[str, Any]:
    """Save state, stop the emulator, dump the save delta, and report.

    A `save_slot` of None exits without writing a state. The save dump still
    runs: the game's own save data belongs to the player either way. In dev
    mode nothing is uploaded and the archive is written to EXPORT_DIR instead;
    a failed push also keeps the archive on disk so save data is never lost.
    The room is told the outcome in a broker chat message, and the controller
    gets a second one carrying the paths and errors the rest of the room has no
    business seeing, before the tokens are cleared and the session retired.

    Args:
        save_slot: The state slot to save into before exiting, or None to skip the state.

    Returns:
        The exit report: `status`, `session_id`, `rom`, the emulator's own exit
        fields, a `save_dump` block, an `upload` block describing what happened
        to the archive, and `selkies_tokens_cleared`.

    Raises:
        HTTPException: 409 when no session is active.
    """
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session")

    emulator = sess["emulator_obj"]
    exit_report = await anyio.to_thread.run_sync(emulator.save_and_exit, save_slot)

    dump_subtrees, _ = _archive_subtrees(
        emulator, bool((sess.get("save") or {}).get("memory_card_synced"))
    )
    dump = await anyio.to_thread.run_sync(
        saves.build_save_archive,
        emulator.save_root,
        dump_subtrees,
        sess["save_baseline"],
        _archive_identity(sess, emulator),
        emulator.save_file_kind,
    )
    cb = sess.get("callback")
    archive_name = f"{sess['id']}-{int(time.time())}.zip"
    archive_path = None
    if dump["error"]:
        # A dump that failed produced no archive, which is not the same thing as
        # a session that changed nothing: reporting it as a successful no-op is
        # what would have RomM record the session as saved and move on.
        log.error(
            "session %s: save dump failed (%s); nothing was uploaded",
            sess["id"],
            dump["error"],
        )
        upload = {
            "mode": "failed",
            "ok": False,
            "error": f"save dump failed: {dump['error']}",
        }
    elif settings.DEV_MODE:
        if dump.get("zip_bytes"):
            archive_path = await anyio.to_thread.run_sync(saves.write_export, dump["zip_bytes"], archive_name)
        upload = {
            "mode": "report-only",
            "note": "dev mode: nothing was uploaded",
            "callback": callback.public_view(cb),
            "would_send": {
                "files": len(dump["files"]),
                "total_bytes": dump["total_bytes"],
                "archive_path": archive_path,
            },
        }
    elif not dump.get("zip_bytes"):
        upload = {"mode": "skipped", "ok": True, "note": "no save changes to upload"}
    else:
        upload = await callback.push_save_archive(
            cb, dump["zip_bytes"], archive_name, sess
        )
        if not upload["ok"]:
            # Keep the archive on disk so a failed push never loses save data.
            archive_path = await anyio.to_thread.run_sync(saves.write_export, dump["zip_bytes"], archive_name)
            upload["archive_path"] = archive_path

    report = {
        "status": "exited",
        "session_id": sess["id"],
        "rom": sess.get("rom"),
        **exit_report,
        "save_dump": {
            "files": dump["files"],
            "skipped": dump["skipped"],
            "total_bytes": dump["total_bytes"],
            "archive_path": archive_path,
            "error": dump["error"],
        },
        "upload": upload,
    }

    outcome, detail = _exit_outcomes(upload, archive_path, cb, dump["error"])
    now_ms = int(time.time() * 1000)
    summary = (
        f"Session ended. Dumped {len(dump['files'])} save file(s), "
        f"{dump['total_bytes']} bytes; {outcome}"
        f". State saved: {exit_report.get('state_saved')}."
    )
    await session.broadcast_to_room(
        {
            "type": "chat_message",
            "sender": "Broker",
            "message": summary,
            "timestamp": now_ms,
            "messageId": f"broker-{now_ms}-summary",
        }
    )
    if detail:
        await _send_to_controller(
            {
                "type": "chat_message",
                "sender": "Broker",
                "message": detail,
                "timestamp": now_ms,
                "messageId": f"broker-{now_ms}-detail",
            }
        )
    await session.notify_session_ended()
    await anyio.sleep(TOKEN_CLEAR_GRACE_SECONDS)
    report["selkies_tokens_cleared"] = await _clear_seat_tokens(sess["id"])
    session.retire_session()
    log.info("exit report: %s", report)
    return report


@router.post("/api/session/exit")
async def exit_session(
    request: Request,
    x_broker_secret: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
    slot: int = Query(default=0, ge=0, le=10),
    save: bool = Query(default=True),
) -> dict[str, Any]:
    """End the session, accepting either the broker secret or the controller token.

    `save=0` is the stop that writes no state. Slot 0 is a real slot on this
    broker, which is why the two are separate: there is no slot number left to
    spend on meaning "do not save". A caller with no opinion on the slot gets
    slot 0 too: every emulator here saves into its own working slot and echoes
    back the one it used, so any other default would just be a lie in the log.

    Args:
        request: The incoming request.
        x_broker_secret: The shared secret RomM sends; checked only when no controller token matches.
        token: The controller token, which lets the room frontend exit without the secret.
        slot: The state slot to save into; ignored when `save` is false.
        save: Whether to write a state before stopping.

    Returns:
        The exit report from `_do_exit`.

    Raises:
        HTTPException: 403 when neither credential is accepted; 409 when no session is active.
    """
    sess = session.SESSION
    is_controller = bool(sess and token and _ct_eq(token, sess["controller_token"]))
    if not is_controller:
        _check_secret(x_broker_secret)
    return await _do_exit(slot if save else None)


def _state_emulator() -> Emulator:
    """Return the running emulator, if it is in a position to take a state command.

    Returns:
        The live session's emulator.

    Raises:
        HTTPException: 409 when no session is active or the emulator is not
            running; 400 when the emulator has no save states.
    """
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session")
    emulator = sess["emulator_obj"]
    if not emulator.supports_states:
        raise HTTPException(
            status_code=400,
            detail=f"{emulator.display_name} has no save states",
        )
    if not emulator.alive():
        raise HTTPException(status_code=409, detail="emulator is not running")
    return emulator


def _swap_emulator() -> Emulator:
    """Return the running emulator, if it is in a position to change discs.

    Returns:
        The live session's emulator.

    Raises:
        HTTPException: 409 when no session is active or the emulator is not
            running; 400 when the emulator cannot swap discs.
    """
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session")
    emulator = sess["emulator_obj"]
    if not emulator.supports_disc_swap:
        raise HTTPException(
            status_code=400,
            detail=f"{emulator.display_name} cannot swap discs",
        )
    if not emulator.alive():
        raise HTTPException(status_code=409, detail="emulator is not running")
    return emulator


def _readable_emulator() -> Emulator:
    """Return the emulator whose state files can be read right now.

    The live session's while one is up, otherwise the one that just exited.
    Reading a state needs the file on disk and the emulator's naming rules, not
    a running process, and the exit state is the one RomM comes back for.

    Returns:
        The live session's emulator, or the retired one from the last exit.

    Raises:
        HTTPException: 409 when there is neither a live session nor a retired one.
    """
    sess = session.SESSION
    if sess is not None and sess.get("active"):
        return sess["emulator_obj"]
    retired = session.LAST_EXIT
    if retired is None:
        raise HTTPException(status_code=409, detail="no session to read a state from")
    return retired["emulator_obj"]


@router.post("/api/session/save-state")
async def save_state(body: StateIn, x_broker_secret: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Save a state into the emulator's working slot.

    The requested slot is resolved to the single working slot and the reply
    reports the slot actually used.

    Args:
        body: The requested slot.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `status` (`saved` or `failed`), the `slot` used and a `saved` flag.

    Raises:
        HTTPException: 403 on a bad secret; 409 when no session is active or the
            emulator is not running; 400 when the emulator has no save states.
    """
    _check_secret(x_broker_secret)
    emulator = _state_emulator()
    saved = await anyio.to_thread.run_sync(emulator.save_state, body.slot)
    slot = emulator.state_slot
    log.info("save state slot %d: %s", slot, "ok" if saved else "failed")
    return {"status": "saved" if saved else "failed", "slot": slot, "saved": saved}


@router.post("/api/session/load-state")
async def load_state(body: StateIn, x_broker_secret: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Load the state in the emulator's working slot.

    The requested slot is resolved to the single working slot and the reply
    reports the slot actually used.

    Args:
        body: The requested slot.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `status` (`loaded` or `failed`), the `slot` used and a `loaded` flag.

    Raises:
        HTTPException: 403 on a bad secret; 409 when no session is active or the
            emulator is not running; 400 when the emulator has no save states.
    """
    _check_secret(x_broker_secret)
    emulator = _state_emulator()
    loaded = await anyio.to_thread.run_sync(emulator.load_state, body.slot)
    slot = emulator.state_slot
    log.info("load state slot %d: %s", slot, "ok" if loaded else "failed")
    return {
        "status": "loaded" if loaded else "failed",
        "slot": slot,
        "loaded": loaded,
    }


@router.post("/api/session/swap-disc")
async def swap_disc(body: DiscIn, x_broker_secret: Optional[str] = Header(default=None)) -> dict[str, str]:
    """Swap the running emulator's disc for the one at the given path.

    Args:
        body: The absolute container path of the new disc.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `status` and the resolved disc `path`.

    Raises:
        HTTPException: 403 on a bad secret; 409 when no session is active or the
            emulator is not running; 400 when the emulator cannot swap discs,
            the path cannot be resolved or it lies outside ROM_ROOT; 404 when
            the disc does not exist; 502 when the emulator refuses the swap.
    """
    _check_secret(x_broker_secret)
    emulator = _swap_emulator()
    try:
        disc_path = Path(body.path).resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="invalid disc path")
    rom_root = settings.rom_root()
    if not disc_path.is_relative_to(rom_root):
        raise HTTPException(
            status_code=400, detail=f"disc path must live under {rom_root}"
        )
    if not disc_path.exists():
        raise HTTPException(status_code=404, detail="disc path does not exist")

    swapped = await anyio.to_thread.run_sync(emulator.swap_disc, disc_path)
    if not swapped:
        raise HTTPException(status_code=502, detail="emulator refused the disc swap")
    log.info("disc swapped to %s", disc_path.name)
    return {"status": "ok", "path": str(disc_path)}


def _header_token(value: str, fallback: str) -> str:
    """Reduce `value` to something safe to put in a response header.

    A Linux filename may hold CR, LF and any non-ASCII byte, all of which would
    either split the response or fail the header encoder, so anything outside
    printable ASCII is dropped rather than passed through.

    Args:
        value: The raw text, typically a filename.
        fallback: What to return when nothing printable survives.

    Returns:
        The printable-ASCII subset of `value`, or `fallback` when that is empty.
    """
    cleaned = "".join(c for c in value if 32 <= ord(c) < 127)
    return cleaned or fallback


@router.get("/api/session/state-file")
async def get_state_file(x_broker_secret: Optional[str] = Header(default=None)) -> FileResponse:
    """Serve the working slot's state file so RomM can file it in the library.

    The slot is the emulator's own, not the caller's: `slot` is accepted for
    symmetry with the per-emulator brokers and ignored the same way the save
    routes ignore it. Served after exit as well as during the session, because
    the state exit captures is exactly the one RomM comes back for once the
    teardown has answered.

    Args:
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        The state file as an octet stream, with `X-State-Filename` and `X-State-Slot` headers.

    Raises:
        HTTPException: 403 on a bad secret; 409 when there is no session to
            read from; 404 when the slot has no state file; 500 when the file
            cannot be read; 413 when it exceeds STATE_FILE_MAX_BYTES.
    """
    _check_secret(x_broker_secret)
    emulator = _readable_emulator()
    path = await anyio.to_thread.run_sync(emulator.state_path)
    if path is None:
        raise HTTPException(status_code=404, detail="no state file for slot")
    try:
        size = path.stat().st_size
    except OSError as exc:
        log.warning("state-file: could not stat %s: %s", path, exc)
        raise HTTPException(status_code=500, detail="could not read state file")
    if size > settings.STATE_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="state file exceeds size limit")
    log.info("state-file: serving %s (%d bytes)", path.name, size)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={
            "X-State-Filename": _header_token(path.name, "state"),
            "X-State-Slot": str(emulator.state_slot),
        },
    )


@router.get("/api/session/state-screenshot")
async def get_state_screenshot(x_broker_secret: Optional[str] = Header(default=None)) -> FileResponse:
    """Serve the frame captured with the working slot's state.

    Only for emulators that write the thumbnail as its own file; the ones that
    embed it in the state answer 404, which is the caller's cue to read the
    frame out of the state it already fetched. `slot` is accepted and ignored
    the same way the state-file routes ignore it, and like the state itself the
    frame stays readable after exit.

    Args:
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        The screenshot as a PNG.

    Raises:
        HTTPException: 403 on a bad secret; 409 when there is no session to
            read from; 404 when the slot has no screenshot file; 500 when the
            file cannot be read; 413 when it exceeds STATE_SCREENSHOT_MAX_BYTES.
    """
    _check_secret(x_broker_secret)
    emulator = _readable_emulator()
    path = await anyio.to_thread.run_sync(emulator.state_screenshot_path)
    if path is None:
        raise HTTPException(status_code=404, detail="no state screenshot for slot")
    try:
        size = path.stat().st_size
    except OSError as exc:
        log.warning("state-screenshot: could not stat %s: %s", path, exc)
        raise HTTPException(status_code=500, detail="could not read screenshot")
    if size > settings.STATE_SCREENSHOT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="screenshot exceeds size limit")
    log.info("state-screenshot: serving %s (%d bytes)", path.name, size)
    return FileResponse(path, media_type="image/png")


def _unlink_best_effort(path: Path) -> None:
    """Remove a temp file without letting a failed cleanup mask the real error.

    tmp.unlink(missing_ok=True) still raises if the parent turned out not
    to be a directory at all; the cleanup itself must never crash a request
    that is already reporting the failure that led here.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove temp file %s: %s", path, exc)


@router.put("/api/session/state-file")
async def put_state_file(
    request: Request,
    filename: str = Query(...),
    x_broker_secret: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Write a state RomM is sending back into the working slot.

    The name has to be one the running emulator could have written for the
    loaded game, which is what state_target decides; anything else is refused
    rather than dropped somewhere in the save tree under a name nothing reads.
    The slot it was captured in does not have to match, since RomM holds the
    library, so the reply reports the name it was filed under.

    Args:
        request: The request whose raw body is the state file, streamed to disk.
        filename: The name RomM filed the state under; only its basename is used.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `status`, the `filename` it was stored under and the `slot`.

    Raises:
        HTTPException: 403 on a bad secret; 409 when no session is active or
            the emulator is not running; 400 when the emulator has no save
            states, the name is not one it would write, or the body is empty;
            413 when the body exceeds STATE_FILE_MAX_BYTES; 500 when the file
            cannot be written.
    """
    _check_secret(x_broker_secret)
    emulator = _state_emulator()
    name = Path(filename).name
    target = emulator.state_target(name)
    if target is None:
        raise HTTPException(status_code=400, detail="filename is not a state this emulator would write")

    # Unique per request: two pushes sharing one temp name interleave their
    # writes, and the os.replace below then publishes the mixture as a state.
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    written = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Streamed to disk rather than buffered: a state runs to hundreds of
        # megabytes and holding one per request on the heap is the whole limit
        # multiplied by however many callers are pushing at once.
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                written += len(chunk)
                if written > settings.STATE_FILE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="state file exceeds size limit")
                await anyio.to_thread.run_sync(out.write, chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="empty request body")
        os.replace(tmp, target)
    except HTTPException:
        _unlink_best_effort(tmp)
        raise
    except OSError as exc:
        _unlink_best_effort(tmp)
        log.warning("state-file: could not write %s: %s", target, exc)
        raise HTTPException(status_code=500, detail="could not write state file")

    log.info("state-file: stored %s (%d bytes)", target.name, written)
    return {"status": "ok", "filename": target.name, "slot": emulator.state_slot}


def _scratch_file(label: str) -> Path:
    """Return a fresh path under IMPORT_DIR to stream a request body into.

    Bodies are staged here rather than beside whatever they are destined for:
    the memory card directory is scanned by the emulator, and a half-written
    upload sitting in it would be read as part of the card.

    Args:
        label: What is being staged, so a file left behind is traceable.

    Returns:
        A path that does not exist yet, in a directory that does.
    """
    settings.IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    return settings.IMPORT_DIR / f".{label}.{secrets.token_hex(8)}.part"


def _refuse_while_session_active(action: str) -> None:
    """Refuse a whole-card operation for as long as a session is running.

    The emulator holds the card open for the life of the game, so a card read
    out from under it is a card caught mid-write, and one written under it is
    corrupted outright. RomM hydrates before activate and evacuates after exit,
    so neither direction has to work with something running.

    Args:
        action: The verb for the reply, e.g. `capture` or `replace`.

    Raises:
        HTTPException: 409 when a session is active.
    """
    sess = session.SESSION
    if sess is not None and sess.get("active"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot {action} the memory card while a session is active",
        )


def _memory_card(name: str, platform: Optional[str]) -> tuple[Path, Optional[str]]:
    """Return the card the named emulator syncs, and the marker file it needs inside.

    Named rather than read off the session, because the card is container
    state, not session state: RomM lays one down before activate, when there is
    no session to resolve it through. `platform` disambiguates an emulator that
    only has a card on some of the platforms it serves (Dolphin: GC, not Wii).

    Args:
        name: The emulator name as RomM knows it.
        platform: The platform slug, when the emulator's card depends on it.

    Returns:
        The card's path and the marker filename, the latter None when the
        emulator needs no marker.

    Raises:
        HTTPException: 422 for an unknown emulator; 400 when it has no memory card to sync.
    """
    emulator = get_emulator(name)
    if emulator is None:
        raise HTTPException(status_code=422, detail=f"unknown emulator: {name}")
    card = emulator.memory_card_path(platform)
    if card is None:
        raise HTTPException(
            status_code=400,
            detail=f"{emulator.display_name} has no memory card to sync",
        )
    return card, emulator.memory_card_marker


@router.get("/api/session/memory-card")
async def get_memory_card(
    emulator: str = Query(...),
    platform: Optional[str] = Query(default=None),
    x_broker_secret: Optional[str] = Header(default=None),
) -> Response:
    """Serve the whole Slot-1 card so RomM can file it against the player.

    A 404 carrying `X-Memory-Card: absent` is the broker confirming the slot is
    empty, which is what tells RomM the card is safe to wipe. Every other
    failure has to read as "could not be captured", or a card RomM never
    managed to read would be destroyed on the next claim.

    Args:
        emulator: The name of the emulator whose card to capture.
        platform: The platform slug, when the emulator's card depends on it.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        The card as a zip, with an `X-Memory-Card-Slot: 1` header.

    Raises:
        HTTPException: 403 on a bad secret; 422 for an unknown emulator; 400
            when it has no memory card; 409 when another card operation is in
            flight, a session is active, or the card could not be captured; 404
            with `X-Memory-Card: absent` when the slot is empty.
    """
    _check_secret(x_broker_secret)
    card, marker = _memory_card(emulator, platform)
    # Contention means a card operation is already in flight, and the caller
    # should come back rather than queue behind it.
    if not memcard.LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a memory card operation is in progress")
    try:
        _refuse_while_session_active("capture")
        result = await anyio.to_thread.run_sync(memcard.build_archive, card, marker)
    finally:
        memcard.LOCK.release()
    if isinstance(result, str):
        raise HTTPException(status_code=409, detail=result)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="no memory card in slot 1",
            headers={"X-Memory-Card": "absent"},
        )
    log.info("memory-card: serving slot 1 (%d bytes)", len(result))
    return Response(
        content=result,
        media_type="application/zip",
        headers={"X-Memory-Card-Slot": "1"},
    )


@router.put("/api/session/memory-card")
async def put_memory_card(
    request: Request,
    emulator: str = Query(...),
    platform: Optional[str] = Query(default=None),
    x_broker_secret: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Wipe Slot 1 and lay down the card RomM is sending.

    Refused while a session is up: the emulator holds the card open for as long
    as the game runs, and swapping it underneath corrupts it. RomM hydrates
    before activate and evacuates after exit, so the card is only ever replaced
    with nothing running.

    Args:
        request: The request whose raw body is the card archive.
        emulator: The name of the emulator whose card to replace.
        platform: The platform slug, when the emulator's card depends on it.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `status`, the number of files `written` and the `slot`.

    Raises:
        HTTPException: 403 on a bad secret; 422 for an unknown emulator; 400
            when it has no memory card, the body is empty or the archive is
            rejected; 413 when the body exceeds SAVE_FILE_MAX_BYTES; 409 when
            another card operation is in flight or a session is active; 500 when
            the body cannot be staged on disk.
    """
    _check_secret(x_broker_secret)
    card, marker = _memory_card(emulator, platform)

    # Staged on disk rather than buffered: a card runs to the whole size limit,
    # and holding both the body and the copy handed to memcard.replace costs
    # twice that per caller pushing at once.
    tmp = _scratch_file("memory-card")
    written = 0
    try:
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                written += len(chunk)
                if written > saves.SAVE_FILE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="memory card exceeds size limit")
                await anyio.to_thread.run_sync(out.write, chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="empty request body")

        if not memcard.LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a memory card operation is in progress")
        try:
            _refuse_while_session_active("replace")
            content = await anyio.to_thread.run_sync(tmp.read_bytes)
            result = await anyio.to_thread.run_sync(memcard.replace, card, content, marker)
        finally:
            memcard.LOCK.release()
    except OSError as exc:
        log.error("memory-card: could not stage the upload at %s: %s", tmp, exc)
        raise HTTPException(status_code=500, detail="could not store the memory card")
    finally:
        _unlink_best_effort(tmp)
    if isinstance(result, str):
        raise HTTPException(status_code=400, detail=result)
    log.info("memory-card: replaced slot 1, %d file(s)", result)
    return {"status": "ok", "written": result, "slot": 1}


@router.get("/api/session/status")
async def status(x_broker_secret: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Report the current session, or that there is none.

    Args:
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        `{"active": False}` when no session exists. Otherwise the session's
        `active` flag, `session_id`, `emulator`, `rom`, `rom_file` and
        `multiplayer` flag, whether the process is `emulator_alive`, the
        emulator's `boot_failed`, `extraction_phase`, `supports_states` and
        `state_slot` signals, `started_at`, the controlling `user` and the
        seated `viewers`.

    Raises:
        HTTPException: 403 on a bad secret.
    """
    _check_secret(x_broker_secret)
    sess = session.SESSION
    if sess is None:
        return {"active": False}
    return {
        "active": sess.get("active", False),
        "session_id": sess["id"],
        "emulator": sess["emulator"],
        "rom": sess.get("rom"),
        "rom_file": sess.get("rom_file"),
        "multiplayer": bool(sess.get("multiplayer")),
        "emulator_alive": sess["emulator_obj"].alive(),
        # Set only by emulators with their own boot-verification signal (PCSX2
        # today, via PINE). Passive: RomM decides what to do about it, this
        # route only reports it.
        "boot_failed": sess["emulator_obj"].boot_failed,
        # Set while a slow pre-launch extraction (shadPS4/RPCS3 pkg or
        # archive) is running, else None. Same passive-signal shape as
        # boot_failed: RomM decides what to show, this route only reports it.
        "extraction_phase": sess["emulator_obj"].extraction_phase,
        # The emulator class is the authority on what it can do, so RomM reads
        # this rather than keeping its own per-emulator table.
        "supports_states": sess["emulator_obj"].supports_states,
        # The slot every state route resolves to. There is only one because
        # RomM holds the library of states.
        "state_slot": sess["emulator_obj"].state_slot,
        "started_at": sess.get("created_at"),
        "user": sess.get("user"),
        "viewers": [
            {"username": v.get("username"), "user_id": v.get("user_id"),
             "permission": v.get("permission")}
            for v in sess.get("viewers", [])
        ],
    }


def _archive_name(name: str) -> str:
    """Return a safe zip basename, rejecting anything with path structure.

    Args:
        name: The archive name as the caller sent it.

    Returns:
        The name unchanged, once it is known to be a bare `.zip` basename.

    Raises:
        HTTPException: 400 when the name has path components, a leading dot or no `.zip` suffix.
    """
    safe = Path(name).name
    if safe != name or not safe.endswith(".zip") or safe.startswith("."):
        raise HTTPException(status_code=400, detail="invalid archive name")
    return safe


def _export_file(name: str) -> Path:
    """Resolve an archive name to a file inside EXPORT_DIR.

    Args:
        name: The archive's basename.

    Returns:
        The resolved path of the export.

    Raises:
        HTTPException: 400 when the name is unsafe or resolves outside
            EXPORT_DIR; 404 when no such export exists.
    """
    candidate = (settings.EXPORT_DIR / _archive_name(name)).resolve()
    if candidate.parent != settings.EXPORT_DIR.resolve():
        raise HTTPException(status_code=400, detail="invalid export name")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"no such export: {name}")
    return candidate


@router.put("/api/session/imports/{name}")
async def upload_import(
    name: str, request: Request, x_broker_secret: Optional[str] = Header(default=None)
) -> dict[str, Any]:
    """Take a save archive from the parent and return the path activate wants.

    Activate's save.archive is a container path, but the parent is a separate
    service with only bytes, so it uploads here first and passes back the
    path this returns. Body is the raw zip, matching the exit download.

    Args:
        name: The archive basename to store it under.
        request: The request whose raw body is the zip.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `status`, the stored `name`, its container `path` and its `size` in bytes.

    Raises:
        HTTPException: 403 on a bad secret; 400 for an unsafe name; 413 when
            the body exceeds SAVE_FILE_MAX_BYTES; 422 when the body is not a
            zip; 500 when the archive cannot be written.
    """
    _check_secret(x_broker_secret)
    safe = _archive_name(name)

    settings.IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = settings.IMPORT_DIR / safe
    # Streamed through a temp file rather than buffered: an archive runs to the
    # whole size limit, and the rename is what keeps a half-received upload from
    # ever being handed to activate as a restore source.
    tmp = _scratch_file(safe)
    total = 0
    header = b""
    try:
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                if len(header) < 2:
                    header = (header + chunk)[:2]
                    # Refused on the first bytes, so a huge non-zip body is not
                    # written to disk in full before being rejected.
                    if not b"PK".startswith(header):
                        raise HTTPException(status_code=422, detail="archive is not a zip")
                total += len(chunk)
                if total > saves.SAVE_FILE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="archive too large")
                await anyio.to_thread.run_sync(out.write, chunk)
        if header != b"PK":
            raise HTTPException(status_code=422, detail="archive is not a zip")
        os.replace(tmp, target)
    except HTTPException:
        _unlink_best_effort(tmp)
        raise
    except OSError as exc:
        _unlink_best_effort(tmp)
        log.error("import: could not store %s: %s", safe, exc)
        raise HTTPException(status_code=500, detail="could not store the archive")

    log.info("import stored: %s (%d bytes)", safe, total)
    return {"status": "stored", "name": safe, "path": str(target), "size": total}


@router.get("/api/session/exports")
async def list_exports(x_broker_secret: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """List the save archives sitting on disk, newest first.

    Args:
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `exports`, a list of entries each carrying `name`, `size` and `mtime`.

    Raises:
        HTTPException: 403 on a bad secret.
    """
    _check_secret(x_broker_secret)
    if not settings.EXPORT_DIR.is_dir():
        return {"exports": []}
    items = []
    for p in settings.EXPORT_DIR.glob("*.zip"):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"exports": items}


@router.get("/api/session/exports/{name}")
async def download_export(
    name: str, x_broker_secret: Optional[str] = Header(default=None)
) -> FileResponse:
    """Hand an archive to the parent on request.

    Pulling covers the two cases the exit push cannot: dev mode, where the
    upload is disabled and the archive only ever lands here, and a failed
    upload, where it is the sole remaining copy of the save data.

    Args:
        name: The archive basename.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        The archive as a zip download.

    Raises:
        HTTPException: 403 on a bad secret; 400 for an unsafe name; 404 when no such export exists.
    """
    _check_secret(x_broker_secret)
    path = _export_file(name)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete("/api/session/exports/{name}")
async def delete_export(
    name: str, x_broker_secret: Optional[str] = Header(default=None)
) -> dict[str, str]:
    """Drop an archive once the parent has stored it.

    Args:
        name: The archive basename.
        x_broker_secret: The shared secret RomM sends; required when `BROKER_SECRET` is set.

    Returns:
        A dict with `status` and the deleted `name`.

    Raises:
        HTTPException: 403 on a bad secret; 400 for an unsafe name; 404 when no such export exists.
    """
    _check_secret(x_broker_secret)
    path = _export_file(name)
    await anyio.to_thread.run_sync(path.unlink)
    log.info("export collected and removed: %s", path.name)
    return {"status": "deleted", "name": path.name}


@router.get("/api/session/context")
async def context(
    token: Optional[str] = Query(default=None),
    invite: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """Resolve an arriving token into the caller's role, for the frontend bootstrap.

    A seat token identifies someone already in the session. An invite token
    identifies a shareable link instead: the arrival is seated on the spot and
    handed the new seat's token as `userToken`, which is what the room keeps
    for the rest of its stay (and across its own reloads) so one person does
    not take a seat per visit.

    Args:
        token: The seat token from the landing URL, if the caller has one.
        invite: The invite token from a shared link, used when `token` is absent.

    Returns:
        The room's bootstrap context: `sessionId`, `userRole` (`controller` or
        `viewer`), `userToken`, `userPublicId` (the caller's own non-sensitive
        id, for cross-referencing itself in `state_update` broadcasts),
        `userPermission`, `username`, `gameName`, `controllerName`,
        `multiplayer` and the stream `iframeSrc`.

    Raises:
        HTTPException: 409 when no session is active; 401 when neither token is
            given or the one given is unknown; 429 when an invite arrival finds
            the room already at its seat cap.
    """
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session")
    if not token and invite:
        permission = session.find_invite(invite)
        if permission is None:
            raise HTTPException(status_code=401, detail="invalid invite")
        viewer, _ = await _mint_viewer(permission, None)
        token = viewer["token"]
        log.info("seated an arrival from the %s invite link", permission)
    if not token:
        raise HTTPException(status_code=401, detail="missing token")

    role = None
    permission = "participant"
    username = None

    if _ct_eq(token, sess["controller_token"]):
        role = "controller"
        username = (sess.get("user") or {}).get("display_name") or (
            sess.get("user") or {}
        ).get("username")
    else:
        viewer = session.find_viewer(token)
        if viewer is not None:
            role = "viewer"
            permission = viewer.get("permission", "participant")
            username = viewer.get("username")

    if role is None:
        raise HTTPException(status_code=401, detail="invalid token")

    return {
        "sessionId": sess["id"],
        "userRole": role,
        "userToken": token,
        "userPublicId": session.public_id_for(token),
        "userPermission": permission if role == "viewer" else "participant",
        "username": username,
        "gameName": (sess.get("rom") or {}).get("name")
        or sess["emulator_obj"].display_name,
        "controllerName": (sess.get("user") or {}).get("display_name") or "Controller",
        "multiplayer": bool(sess.get("multiplayer")),
        # Relative to the page base; the selkies client reads the token from
        # its query string.
        "iframeSrc": f"stream/?token={token}",
    }
