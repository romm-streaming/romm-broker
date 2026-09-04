"""The HTTP surface RomM drives the container through.

Covers every route under the API prefix: health, activate, join, states, memory cards, imports,
exports, context, exit and disc swap.
"""

import io
import json
import os
import shutil
import signal
import subprocess
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState

from webstation_broker import api, callback, saves, selkies, session, settings
from webstation_broker.app import create_app
from webstation_broker.emulators import base, rpcs3, shadps4

from .conftest import PREFIX, SLEEPER_CMD, FakeEmulator

API = f"{PREFIX}/api"


def _zip(members: dict[str, bytes]) -> bytes:
    """Build an in-memory zip archive from a name-to-content mapping.

    Args:
        members: Archive member names mapped to their bytes.

    Returns:
        The zip file contents.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _rom(broker_dirs: dict[str, Path], name: str = "Game.iso") -> Path:
    """Write a placeholder ROM into the redirected library.

    Args:
        broker_dirs: The redirected ROM root and archive directories.
        name: File name to create under the ROM root.

    Returns:
        The path of the written ROM.
    """
    rom = broker_dirs["roms"] / name
    rom.write_bytes(b"iso")
    return rom


def _activate(client: TestClient, broker_dirs: dict[str, Path], **overrides: object) -> httpx.Response:
    """Post a well-formed activate request for the fake emulator.

    Args:
        client: The client to send the request through.
        broker_dirs: The redirected ROM root and archive directories. The ROM is written into them.
        **overrides: Body fields that replace the defaults.

    Returns:
        The activate response.
    """
    body = {
        "session_id": "sess-1",
        "emulator": "fake",
        "user": {"id": 1, "username": "ana", "display_name": "Ana"},
        "rom": {"id": 5, "name": "Game", "platform": "ps2", "path": str(_rom(broker_dirs))},
    }
    body.update(overrides)
    return client.post(f"{API}/session/activate", json=body)


def test_health_answers_without_a_secret(client: TestClient) -> None:
    """Health answers without a secret."""
    assert client.get(f"{API}/health").json() == {"status": "ok"}


def test_status_is_inactive_before_anything_runs(client: TestClient) -> None:
    """Status reports inactive before anything runs."""
    assert client.get(f"{API}/session/status").json() == {"active": False}


def test_status_requires_the_broker_secret_when_one_is_set(
    secret_client: TestClient, broker_dirs: dict[str, Path]
) -> None:
    """Status requires X-Broker-Secret, unlike /health, since it exposes usernames and ROM details."""
    response = secret_client.get(f"{API}/session/status")

    assert response.status_code == 403

    secret_client.headers["X-Broker-Secret"] = "s3cret"
    assert secret_client.get(f"{API}/session/status").json() == {"active": False}


def test_a_wrong_secret_is_refused(secret_client: TestClient, broker_dirs: dict[str, Path]) -> None:
    """A request carrying the wrong secret is refused with 403."""
    response = _activate(secret_client, broker_dirs)

    assert response.status_code == 403


def test_the_right_secret_gets_through(
    secret_client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A request carrying the right secret gets through."""
    secret_client.headers["X-Broker-Secret"] = "s3cret"

    assert _activate(secret_client, broker_dirs).status_code == 200


def test_activate_hands_the_emulator_the_rom_language(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """The rom's language reaches the emulator, the same way its platform does.

    A launcher whose games ship several languages in one folder (ScummVM
    registers one target per language) has no other way to know which to boot.
    """
    rom = {
        "id": 5,
        "name": "Game",
        "platform": "ps2",
        "language": "fr",
        "path": str(broker_dirs["roms"] / "Game.iso"),
    }

    assert _activate(client, broker_dirs, rom=rom).status_code == 200
    assert fake_emulator[0].language == "fr"


def test_activate_without_a_language_leaves_it_unset(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A payload that names no language leaves the emulator on its own default."""
    assert _activate(client, broker_dirs).status_code == 200
    assert fake_emulator[0].language is None


def test_activate_hands_the_emulator_the_gui_language(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """The player's interface language reaches the emulator too.

    An emulator with a translated UI follows it, and a multilingual game folder
    falls back to it when the rom names no language of its own.
    """
    assert _activate(client, broker_dirs, gui_language="fr").status_code == 200
    assert fake_emulator[0].gui_language == "fr"


def test_activate_without_a_gui_language_leaves_it_unset(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A payload that names no interface language leaves the emulator's own."""
    assert _activate(client, broker_dirs).status_code == 200
    assert fake_emulator[0].gui_language is None


def test_activate_launches_the_rom_and_hands_back_a_landing_url(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Activate launches the ROM and hands back a landing URL."""
    response = _activate(client, broker_dirs)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "launching"
    assert body["rom_file"] == str(broker_dirs["roms"] / "Game.iso")
    assert body["url"].startswith(f"{PREFIX}/?token=")
    emulator = fake_emulator[0]
    assert emulator.launched == (broker_dirs["roms"] / "Game.iso", None)
    # The slot is emptied before the restore so the incoming archive, not the
    # last session, decides what the emulator resumes from.
    assert emulator.cleared is True


def test_activate_refuses_a_second_session_over_a_running_one(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Activate refuses a second session over a running one."""
    _activate(client, broker_dirs)

    assert _activate(client, broker_dirs).status_code == 409


def test_activate_retires_the_session_when_launch_fails(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch failure retires the session instead of leaving every retry stuck on 409."""
    monkeypatch.setattr(FakeEmulator, "launch_fails", True)

    with pytest.raises(RuntimeError):
        _activate(client, broker_dirs)

    assert session.SESSION is None

    monkeypatch.setattr(FakeEmulator, "launch_fails", False)
    assert _activate(client, broker_dirs).status_code == 200


def test_activate_refuses_an_emulator_that_is_not_installed(
    client: TestClient, broker_dirs: dict[str, Path]
) -> None:
    """Activate refuses an emulator that is not installed."""
    assert _activate(client, broker_dirs, emulator="gameboy").status_code == 422


def test_activate_refuses_a_rom_outside_the_library(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], tmp_path: Path
) -> None:
    """Activate refuses a ROM outside the library."""
    outside = tmp_path / "elsewhere.iso"
    outside.write_bytes(b"iso")

    response = _activate(
        client, broker_dirs, rom={"path": str(outside), "platform": "ps2"}
    )

    assert response.status_code == 400


def test_activate_reports_a_rom_that_is_not_there(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Activate reports a ROM that is not there as 404."""
    response = _activate(
        client,
        broker_dirs,
        rom={"path": str(broker_dirs["roms"] / "gone.iso"), "platform": "ps2"},
    )

    assert response.status_code == 404


def test_activate_reports_a_folder_holding_nothing_bootable(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Activate reports a folder holding nothing bootable as 422."""
    folder = broker_dirs["roms"] / "game"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"nope")

    response = _activate(client, broker_dirs, rom={"path": str(folder), "platform": "ps2"})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "no bootable file found"


def test_activate_restores_the_save_archive_it_is_pointed_at(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], tmp_path: Path
) -> None:
    """Activate restores the save archive it is pointed at."""
    archive = tmp_path / "incoming.zip"
    archive.write_bytes(_zip({"saves/card.bin": b"restored"}))

    response = _activate(client, broker_dirs, save={"archive": str(archive)})

    assert response.json()["save_restore"]["written"] == 1
    root = fake_emulator[0].save_root
    assert (root / "saves" / "card.bin").read_bytes() == b"restored"


def test_activate_refuses_a_save_archive_that_is_not_there(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], tmp_path: Path
) -> None:
    """Activate refuses a save archive that is not there, with the working slot untouched.

    The slot is emptied to make room for the restore, so a refusal that had
    already emptied it would have thrown away the save data of whoever played
    last in exchange for a 404.
    """
    response = _activate(client, broker_dirs, save={"archive": str(tmp_path / "gone.zip")})

    assert response.status_code == 404
    assert fake_emulator[0].cleared is False


def test_activate_rejects_an_archive_it_cannot_unpack(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], tmp_path: Path
) -> None:
    """Activate rejects an archive it cannot unpack."""
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"not a zip")

    assert _activate(client, broker_dirs, save={"archive": str(archive)}).status_code == 422


def test_status_reports_what_romm_reads_off_the_running_session(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Status reports what RomM reads off the running session."""
    _activate(client, broker_dirs)

    body = client.get(f"{API}/session/status").json()

    assert body["active"] is True
    assert body["session_id"] == "sess-1"
    assert body["emulator"] == "fake"
    assert body["emulator_alive"] is True
    assert body["boot_failed"] is False
    assert body["extraction_phase"] is None
    assert body["supports_states"] is True
    assert body["state_slot"] == 3


def test_joining_hands_the_viewer_their_own_landing_url(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Joining hands the viewer their own landing URL."""
    _activate(client, broker_dirs)

    body = client.post(
        f"{API}/session/join",
        json={"user": {"id": 7, "username": "bo"}, "permission": "readonly"},
    ).json()

    assert body["username"] == "bo"
    assert body["url"] != f"{PREFIX}/?token={session.SESSION['controller_token']}"


def test_joining_nothing_is_a_conflict(client: TestClient) -> None:
    """Joining with no session running is a conflict."""
    response = client.post(f"{API}/session/join", json={"permission": "participant"})

    assert response.status_code == 409


def test_an_unknown_permission_is_refused(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An unknown permission is refused on join."""
    _activate(client, broker_dirs)

    response = client.post(f"{API}/session/join", json={"permission": "admin"})

    assert response.status_code == 422


def test_a_state_save_resolves_to_the_emulator_working_slot(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A state save resolves to the emulator working slot."""
    _activate(client, broker_dirs)

    body = client.post(f"{API}/session/save-state", json={"slot": 7}).json()

    assert body == {"status": "saved", "slot": 3, "saved": True}
    # The requested slot is passed through, but the emulator is the authority
    # on where it actually lands.
    assert fake_emulator[0].saved_slots == [7]


def test_a_state_load_answers_with_the_same_working_slot(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A state load answers with the same working slot."""
    _activate(client, broker_dirs)

    body = client.post(f"{API}/session/load-state", json={"slot": 0}).json()

    assert body == {"status": "loaded", "slot": 3, "loaded": True}


def test_a_state_route_with_no_session_is_a_conflict(client: TestClient) -> None:
    """A state route with no session is a conflict."""
    assert client.post(f"{API}/session/save-state", json={"slot": 1}).status_code == 409


def test_a_state_route_refuses_a_slot_outside_the_accepted_range(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A state route refuses a slot outside the accepted range."""
    _activate(client, broker_dirs)

    assert client.post(f"{API}/session/save-state", json={"slot": 99}).status_code == 422


def test_a_state_route_refuses_an_emulator_that_died(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A state route refuses an emulator that died."""
    _activate(client, broker_dirs)
    fake_emulator[0].running = False

    assert client.post(f"{API}/session/save-state", json={"slot": 1}).status_code == 409


def test_the_state_file_is_served_with_the_name_and_slot_romm_files_it_under(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], tmp_path: Path
) -> None:
    """The state file is served with the name and slot RomM files it under."""
    _activate(client, broker_dirs)
    state = tmp_path / "GAME.03.p2s"
    state.write_bytes(b"state bytes")
    fake_emulator[0].state_file = state

    response = client.get(f"{API}/session/state-file")

    assert response.content == b"state bytes"
    assert response.headers["X-State-Filename"] == "GAME.03.p2s"
    assert response.headers["X-State-Slot"] == "3"


def test_an_empty_working_slot_serves_no_state_file(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An empty working slot serves no state file."""
    _activate(client, broker_dirs)

    assert client.get(f"{API}/session/state-file").status_code == 404


def test_a_pushed_state_lands_under_the_name_the_emulator_chose(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], tmp_path: Path
) -> None:
    """A pushed state lands under the name the emulator chose."""
    _activate(client, broker_dirs)
    target = tmp_path / "GAME.03.p2s"
    fake_emulator[0].state_file = target

    response = client.put(
        f"{API}/session/state-file", params={"filename": "GAME.01.p2s"}, content=b"pushed"
    )

    assert response.json() == {"status": "ok", "filename": "GAME.03.p2s", "slot": 3}
    assert target.read_bytes() == b"pushed"


def test_a_pushed_state_the_emulator_would_never_write_is_refused(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A pushed state the emulator would never write is refused."""
    _activate(client, broker_dirs)
    fake_emulator[0].state_file = None

    response = client.put(
        f"{API}/session/state-file", params={"filename": "junk.bin"}, content=b"pushed"
    )

    assert response.status_code == 400


def test_an_empty_state_push_leaves_nothing_behind(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], tmp_path: Path
) -> None:
    """An empty state push leaves nothing behind."""
    _activate(client, broker_dirs)
    target = tmp_path / "GAME.03.p2s"
    fake_emulator[0].state_file = target

    response = client.put(
        f"{API}/session/state-file", params={"filename": "GAME.01.p2s"}, content=b""
    )

    assert response.status_code == 400
    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_the_memory_card_routes_refuse_an_emulator_without_one(
    client: TestClient, fake_emulator: list[FakeEmulator]
) -> None:
    """The memory card routes refuse an emulator without a card."""
    response = client.get(f"{API}/session/memory-card", params={"emulator": "fake"})

    assert response.status_code == 400


def test_the_memory_card_routes_refuse_an_emulator_that_is_not_installed(client: TestClient) -> None:
    """The memory card routes refuse an emulator that is not installed."""
    response = client.get(f"{API}/session/memory-card", params={"emulator": "gameboy"})

    assert response.status_code == 422


def test_an_empty_card_push_is_refused(client: TestClient, fake_emulator: list[FakeEmulator]) -> None:
    """An empty card push is refused."""
    response = client.put(
        f"{API}/session/memory-card", params={"emulator": "fake"}, content=b""
    )

    assert response.status_code == 400


def test_an_import_is_stored_under_the_path_activate_takes(
    client: TestClient, broker_dirs: dict[str, Path]
) -> None:
    """An import is stored under the path activate takes."""
    body = client.put(f"{API}/session/imports/sess-1.zip", content=_zip({"a": b"x"})).json()

    assert body["path"] == str(broker_dirs["imports"] / "sess-1.zip")
    assert body["status"] == "stored"


def test_an_import_that_is_not_a_zip_is_refused(client: TestClient) -> None:
    """An import that is not a zip is refused."""
    response = client.put(f"{API}/session/imports/sess-1.zip", content=b"not a zip")

    assert response.status_code == 422


@pytest.mark.parametrize("name", ["sess-1.tar", ".hidden.zip"])
def test_an_import_that_is_not_a_plain_zip_name_is_refused(client: TestClient, name: str) -> None:
    """An import that is not a plain zip name is refused."""
    response = client.put(f"{API}/session/imports/{name}", content=_zip({"a": b"x"}))

    assert response.status_code == 400


@pytest.mark.parametrize("name", ["../escape.zip", "sub/dir.zip", "/abs.zip", ""])
def test_an_archive_name_carrying_path_structure_is_refused(name: str) -> None:
    """An archive name carrying path structure is refused."""
    from fastapi import HTTPException

    from webstation_broker.api import _archive_name

    with pytest.raises(HTTPException) as raised:
        _archive_name(name)
    assert raised.value.status_code == 400


def test_exports_list_newest_first(client: TestClient, broker_dirs: dict[str, Path]) -> None:
    """Exports list newest first."""
    import os

    old = broker_dirs["exports"] / "old.zip"
    new = broker_dirs["exports"] / "new.zip"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1000, 1000))
    os.utime(new, (3000, 3000))

    names = [e["name"] for e in client.get(f"{API}/session/exports").json()["exports"]]

    assert names == ["new.zip", "old.zip"]


def test_an_export_downloads_and_then_deletes(client: TestClient, broker_dirs: dict[str, Path]) -> None:
    """An export downloads and then deletes."""
    (broker_dirs["exports"] / "sess-1.zip").write_bytes(b"archive")

    assert client.get(f"{API}/session/exports/sess-1.zip").content == b"archive"
    assert client.delete(f"{API}/session/exports/sess-1.zip").json()["status"] == "deleted"
    assert not (broker_dirs["exports"] / "sess-1.zip").exists()


def test_an_export_that_was_never_written_is_a_404(client: TestClient) -> None:
    """An export that was never written is a 404."""
    assert client.get(f"{API}/session/exports/gone.zip").status_code == 404


def test_context_resolves_the_controller_token(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Context resolves the controller token."""
    token = _activate(client, broker_dirs).json()["url"].split("token=")[1]

    body = client.get(f"{API}/session/context", params={"token": token}).json()

    assert body["userRole"] == "controller"
    assert body["username"] == "Ana"
    assert body["gameName"] == "Game"
    assert body["iframeSrc"] == f"stream/?token={token}"


def test_context_resolves_a_viewer_token_to_their_permission(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Context resolves a viewer token to their permission."""
    _activate(client, broker_dirs)
    url = client.post(
        f"{API}/session/join",
        json={"user": {"id": 7, "username": "bo"}, "permission": "readonly"},
    ).json()["url"]

    body = client.get(
        f"{API}/session/context", params={"token": url.split("token=")[1]}
    ).json()

    assert body["userRole"] == "viewer"
    assert body["userPermission"] == "readonly"


@pytest.mark.parametrize("params", [{}, {"token": "made-up"}])
def test_context_refuses_a_token_it_cannot_place(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    params: dict[str, str],
) -> None:
    """Context refuses a token it cannot place."""
    _activate(client, broker_dirs)

    assert client.get(f"{API}/session/context", params=params).status_code == 401


def _write_save_after_launch(path: Path, data: bytes) -> None:
    """Write a save file whose mtime is unambiguously later than the session baseline.

    The kernel stamps files with its coarse clock, which lags `time.time()` by up to a tick, so a
    file written straight after activate can carry an mtime earlier than the baseline the exit
    dump measures against and be left out of the delta. Pushing the mtime a second ahead keeps
    these tests about the delta logic rather than the host's clock granularity.

    Args:
        path: Where to write the file.
        data: Its contents.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    later = time.time() + 1
    os.utime(path, (later, later))


def test_exit_dumps_the_save_delta_and_retires_the_session(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit dumps the save delta and retires the session."""
    monkeypatch.setattr(settings, "DEV_MODE", True)
    _activate(client, broker_dirs)
    _write_save_after_launch(fake_emulator[0].save_root / "saves" / "card.bin", b"played")

    body = client.post(f"{API}/session/exit").json()

    assert body["status"] == "exited"
    assert [f["path"] for f in body["save_dump"]["files"]] == ["saves/card.bin"]
    assert body["upload"]["mode"] == "report-only"
    assert fake_emulator[0].running is False
    assert session.SESSION is None


def test_the_exit_archive_carries_a_manifest_romm_can_sort_by(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exit archive carries a manifest naming the session and each member's kind."""
    monkeypatch.setattr(settings, "DEV_MODE", True)
    _activate(client, broker_dirs)
    root = fake_emulator[0].save_root
    _write_save_after_launch(root / "saves" / "card.bin", b"played")
    _write_save_after_launch(root / "states" / "game.p2s", b"state")

    body = client.post(f"{API}/session/exit").json()
    archive = Path(body["upload"]["would_send"]["archive_path"])
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read(saves.MANIFEST_NAME))

    assert manifest["session"] == {
        "emulator": "fake",
        "core": None,
        "platform": "ps2",
        "rom_id": 5,
        "rom": "Game",
        "rom_file": str(_rom(broker_dirs)),
        "state_slot": 3,
    }
    assert {f["path"]: f["kind"] for f in manifest["files"]} == {
        "saves/card.bin": "save",
        "states/game.p2s": "state",
    }


def test_exit_leaves_the_state_readable_for_the_pull_that_follows(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator], tmp_path: Path
) -> None:
    """Exit leaves the state readable for the pull that follows."""
    _activate(client, broker_dirs)
    state = tmp_path / "GAME.03.p2s"
    state.write_bytes(b"final state")
    fake_emulator[0].state_file = state

    client.post(f"{API}/session/exit")

    # RomM comes back for the exit state after the teardown has answered, so
    # the retired emulator has to keep serving it.
    assert client.get(f"{API}/session/state-file").content == b"final state"


def test_the_controller_token_is_enough_to_exit(
    secret_client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """The controller token is enough to exit."""
    secret_client.headers["X-Broker-Secret"] = "s3cret"
    token = _activate(secret_client, broker_dirs).json()["url"].split("token=")[1]
    del secret_client.headers["X-Broker-Secret"]

    response = secret_client.post(f"{API}/session/exit", params={"token": token})

    assert response.status_code == 200


def test_a_stranger_cannot_exit_someone_else_session(
    secret_client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A stranger cannot exit someone else's session."""
    secret_client.headers["X-Broker-Secret"] = "s3cret"
    _activate(secret_client, broker_dirs)
    del secret_client.headers["X-Broker-Secret"]

    response = secret_client.post(f"{API}/session/exit", params={"token": "made-up"})

    assert response.status_code == 403
    assert session.SESSION is not None


def test_exit_with_save_off_writes_no_state_but_still_dumps_the_saves(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit with save off writes no state but still dumps the saves.

    A player leaving without saving gets no state written, and the game's own save data still
    travels: that progress is theirs either way.
    """
    monkeypatch.setattr(settings, "DEV_MODE", True)
    _activate(client, broker_dirs)
    _write_save_after_launch(fake_emulator[0].save_root / "saves" / "card.bin", b"played")

    body = client.post(f"{API}/session/exit", params={"save": "false"}).json()

    assert fake_emulator[0].exit_slots == [None]
    assert body["state_saved"] is False
    assert body["state_slot"] is None
    assert [f["path"] for f in body["save_dump"]["files"]] == ["saves/card.bin"]


def test_exit_carries_slot_zero_rather_than_falling_back_to_the_default(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Exit carries slot zero rather than falling back to the default.

    Slot 0 is a real slot here, so a request asking for it has to arrive as 0 and not be mistaken
    for "nothing was asked for".
    """
    _activate(client, broker_dirs)

    client.post(f"{API}/session/exit", params={"slot": 0})

    assert fake_emulator[0].exit_slots == [0]


@pytest.mark.parametrize("prefix", [PREFIX, ""])
def test_starting_the_app_reaps_an_emulator_an_earlier_broker_left(
    prefix: str,
    pid_record: Path,
    sleeper: Callable[[], subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting the app reaps an emulator an earlier broker left.

    Exit answers 409 with no session, so a broker that comes back to an orphan can only kill it at
    startup. Both app shapes are checked because Starlette hands the lifespan to the served app,
    never to a mounted one.
    """
    monkeypatch.setattr(settings, "PREFIX", prefix)
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    proc = sleeper()
    base._record_pid("fake", proc.pid, SLEEPER_CMD)

    with TestClient(create_app()):
        pass

    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert not pid_record.exists()


@pytest.mark.parametrize("prefix", [PREFIX, ""])
def test_starting_the_app_sweeps_the_scratch_dirs_both_caching_emulators_leave(
    prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup reclaims orphaned extraction scratch for every emulator that extracts.

    Scratch is otherwise only cleared by the next extraction, and a library
    whose games are already extracted may never run one again.
    """
    monkeypatch.setattr(settings, "PREFIX", prefix)
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    orphans = []
    for module, name in ((shadps4, "shadps4"), (rpcs3, "rpcs3")):
        cache = tmp_path / name
        monkeypatch.setattr(module, "CACHE_DIR", cache)
        scratch = cache / module._SCRATCH_DIR_NAME / "dead-run"
        scratch.mkdir(parents=True)
        (scratch / "leftover").write_bytes(b"x")
        orphans.append(scratch)

    with TestClient(create_app()):
        pass

    assert [o.exists() for o in orphans] == [False, False]


def test_exiting_nothing_is_a_conflict(client: TestClient) -> None:
    """Exiting with no session running is a conflict."""
    assert client.post(f"{API}/session/exit").status_code == 409


# ── callback.base_url scheme validation ─────────────────────────────────


def test_activate_refuses_a_non_http_callback_scheme(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Activate refuses a non-http(s) callback scheme."""
    response = _activate(
        client, broker_dirs, callback={"base_url": "file:///etc/passwd", "token": "t"}
    )

    assert response.status_code == 422


def test_activate_accepts_an_http_callback_scheme(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activate accepts an http(s) callback scheme."""
    monkeypatch.setattr(settings, "DEV_MODE", True)
    response = _activate(
        client, broker_dirs, callback={"base_url": "http://romm.example/api", "token": "t"}
    )
    assert response.status_code == 200

    body = client.post(f"{API}/session/exit").json()

    assert body["upload"]["callback"]["base_url"] == "http://romm.example/api"
    assert body["upload"]["callback"]["derived"] is False


def test_activate_accepts_no_callback_and_derives_one(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activate with no callback derives one from the request origin."""
    monkeypatch.setattr(settings, "DEV_MODE", True)
    response = _activate(client, broker_dirs)
    assert response.status_code == 200

    body = client.post(f"{API}/session/exit").json()

    assert body["upload"]["callback"]["derived"] is True


# ── generic 500s that don't leak filesystem details ─────────────────────


def test_state_file_read_failure_reports_no_path_or_exception_text(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    tmp_path: Path,
) -> None:
    """A state-file read failure reports no path or exception text."""
    _activate(client, broker_dirs)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    fake_emulator[0].state_file = blocker / "state.bin"

    response = client.get(f"{API}/session/state-file")

    assert response.status_code == 500
    assert response.json()["detail"] == "could not read state file"
    assert str(blocker) not in response.text


def test_state_screenshot_read_failure_reports_no_path_or_exception_text(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    tmp_path: Path,
) -> None:
    """A state-screenshot read failure reports no path or exception text."""
    _activate(client, broker_dirs)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    fake_emulator[0].state_screenshot_path = lambda: blocker / "shot.png"

    response = client.get(f"{API}/session/state-screenshot")

    assert response.status_code == 500
    assert response.json()["detail"] == "could not read screenshot"
    assert str(blocker) not in response.text


def test_state_file_write_failure_reports_no_path_or_exception_text(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    tmp_path: Path,
) -> None:
    """A state-file write failure reports no path or exception text."""
    _activate(client, broker_dirs)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    fake_emulator[0].state_file = blocker / "GAME.03.p2s"

    response = client.put(
        f"{API}/session/state-file", params={"filename": "GAME.01.p2s"}, content=b"pushed"
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "could not write state file"
    assert str(blocker) not in response.text


def test_activate_records_a_multiplayer_session(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Activate records a multiplayer session."""
    _activate(client, broker_dirs, multiplayer=True)

    assert session.SESSION["multiplayer"] is True


def test_activate_defaults_to_a_solo_session(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Activate defaults to a solo session."""
    _activate(client, broker_dirs)

    assert session.SESSION["multiplayer"] is False


def test_context_tells_the_room_which_kind_of_session_it_is(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Context tells the room which kind of session it is."""
    _activate(client, broker_dirs, multiplayer=True)
    token = session.SESSION["controller_token"]

    body = client.get(f"{API}/session/context", params={"token": token}).json()

    assert body["multiplayer"] is True


def test_context_reports_a_solo_session_as_such(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Context reports a solo session as such."""
    _activate(client, broker_dirs)
    token = session.SESSION["controller_token"]

    body = client.get(f"{API}/session/context", params={"token": token}).json()

    assert body["multiplayer"] is False


def test_the_controller_can_mint_an_invite_link(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """The controller can mint an invite link.

    The link is the session's shareable one for that permission: the same on every call, and it
    seats nobody until someone opens it.
    """
    _activate(client, broker_dirs)
    token = session.SESSION["controller_token"]

    body = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "participant"},
    ).json()
    again = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "participant"},
    ).json()

    assert body["url"].startswith(f"{PREFIX}/?invite=")
    assert token not in body["url"]
    assert again["url"] == body["url"]
    assert session.SESSION["viewers"] == []


def test_each_arrival_on_an_invite_link_gets_its_own_seat(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """Each arrival on an invite link gets its own seat.

    One link goes to every friend; the context route is what seats each of them, with the link's
    permission and a personal token of their own.
    """
    _activate(client, broker_dirs)
    token = session.SESSION["controller_token"]
    url = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "readonly"},
    ).json()["url"]
    invite = url.split("invite=")[1]

    first = client.get(f"{API}/session/context", params={"invite": invite}).json()
    second = client.get(f"{API}/session/context", params={"invite": invite}).json()

    assert first["userRole"] == "viewer"
    assert first["userPermission"] == "readonly"
    assert first["userToken"] != second["userToken"]
    assert len(session.SESSION["viewers"]) == 2
    # The seat token handed back is a real seat from then on.
    assert client.get(f"{API}/session/context", params={"token": first["userToken"]}).status_code == 200


def test_a_disconnected_arrival_is_reclaimed_at_the_room_cap(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arrival at the room cap reclaims a disconnected anonymous seat rather than piling up."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate(client, broker_dirs)
    token = session.SESSION["controller_token"]
    url = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "readonly"},
    ).json()["url"]
    invite = url.split("invite=")[1]
    first = client.get(f"{API}/session/context", params={"invite": invite}).json()
    # Past the grace a just-minted seat gets, so the seat reads as one whose
    # user left rather than one whose browser is still loading.
    session.SESSION["viewers"][0]["last_seen"] = 0.0

    response = client.get(f"{API}/session/context", params={"invite": invite})

    assert response.status_code == 200
    assert response.json()["userToken"] != first["userToken"]
    assert len(session.SESSION["viewers"]) == 1


def test_an_arrival_past_the_room_cap_gets_a_429_when_nothing_is_reclaimable(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arrival past the room cap gets a 429 when the seat holding it is a named user."""
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    _activate(client, broker_dirs)
    client.post(
        f"{API}/session/join",
        json={"permission": "participant", "user": {"id": 7, "username": "ana"}},
    )

    response = client.post(f"{API}/session/join", json={"permission": "participant"})

    assert response.status_code == 429
    assert len(session.SESSION["viewers"]) == 1


def test_an_unknown_invite_token_is_refused(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An unknown invite token is refused."""
    _activate(client, broker_dirs)

    response = client.get(f"{API}/session/context", params={"invite": "nope"})

    assert response.status_code == 401
    assert session.SESSION["viewers"] == []


def test_an_invite_works_on_a_solo_session(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An invite works on a solo session.

    The switch governs discovery and comms, never the link.
    """
    _activate(client, broker_dirs, multiplayer=False)
    token = session.SESSION["controller_token"]

    response = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "participant"},
    )

    assert response.status_code == 200


def test_a_viewer_cannot_mint_invites(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A viewer cannot mint invites."""
    _activate(client, broker_dirs)
    viewer = client.post(
        f"{API}/session/join", json={"permission": "participant"}
    ).json()
    viewer_token = session.SESSION["viewers"][0]["token"]
    assert viewer["username"]

    response = client.post(
        f"{API}/session/invite",
        params={"token": viewer_token},
        json={"permission": "participant"},
    )

    assert response.status_code == 403


def test_inviting_into_nothing_is_a_conflict(client: TestClient) -> None:
    """Inviting with no session running is a conflict."""
    response = client.post(
        f"{API}/session/invite",
        params={"token": "whatever"},
        json={"permission": "participant"},
    )

    assert response.status_code == 409


def test_an_invite_with_an_unknown_permission_is_refused(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An invite with an unknown permission is refused."""
    _activate(client, broker_dirs)
    token = session.SESSION["controller_token"]

    response = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "admin"},
    )

    assert response.status_code == 422


class TestSwapDisc:
    """Changing the mounted disc mid-session."""

    def _disc(self, broker_dirs: dict[str, Path], name: str = "Game (Disc 2).chd") -> Path:
        """Write a placeholder disc image into the redirected library.

        Args:
            broker_dirs: The redirected ROM root and archive directories.
            name: File name to create under the ROM root.

        Returns:
            The path of the written disc.
        """
        disc = broker_dirs["roms"] / name
        disc.write_bytes(b"disc")
        return disc

    def _session(self, secret_client: TestClient, broker_dirs: dict[str, Path]) -> None:
        """Activate a session on the secret-guarded client.

        `_activate` sends no secret header of its own, so the header goes on the client first, the
        way test_the_right_secret_gets_through does.

        Args:
            secret_client: The client guarded by the broker secret.
            broker_dirs: The redirected ROM root and archive directories.
        """
        secret_client.headers["X-Broker-Secret"] = "s3cret"
        assert _activate(secret_client, broker_dirs).status_code == 200

    def test_a_swap_reaches_the_emulator(
        self, secret_client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
    ) -> None:
        """A swap reaches the emulator with the resolved disc path."""
        self._session(secret_client, broker_dirs)
        disc = self._disc(broker_dirs)
        r = secret_client.post(
            f"{API}/session/swap-disc", json={"path": str(disc)}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert fake_emulator[0].swapped_discs == [disc.resolve()]

    def test_a_refused_swap_is_a_bad_gateway(
        self, secret_client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
    ) -> None:
        """A swap the emulator refuses is a 502."""
        self._session(secret_client, broker_dirs)
        fake_emulator[0].swap_ok = False
        r = secret_client.post(
            f"{API}/session/swap-disc",
            json={"path": str(self._disc(broker_dirs))},
        )
        assert r.status_code == 502

    def test_a_disc_outside_the_rom_root_is_rejected(
        self,
        secret_client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        tmp_path: Path,
    ) -> None:
        """A disc outside the ROM root is rejected."""
        self._session(secret_client, broker_dirs)
        outside = tmp_path / "elsewhere.chd"
        outside.write_bytes(b"x")
        r = secret_client.post(
            f"{API}/session/swap-disc", json={"path": str(outside)}
        )
        assert r.status_code == 400

    def test_a_missing_disc_is_a_not_found(
        self, secret_client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
    ) -> None:
        """A missing disc is a 404."""
        self._session(secret_client, broker_dirs)
        r = secret_client.post(
            f"{API}/session/swap-disc",
            json={"path": str(broker_dirs["roms"] / "nope.chd")},
        )
        assert r.status_code == 404

    def test_an_emulator_without_a_tray_is_refused(
        self, secret_client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
    ) -> None:
        """An emulator without a disc tray is refused."""
        self._session(secret_client, broker_dirs)
        fake_emulator[0].supports_disc_swap = False
        r = secret_client.post(
            f"{API}/session/swap-disc",
            json={"path": str(self._disc(broker_dirs))},
        )
        assert r.status_code == 400

    def test_no_session_means_no_swap(self, secret_client: TestClient, broker_dirs: dict[str, Path]) -> None:
        """No session means no swap."""
        secret_client.headers["X-Broker-Secret"] = "s3cret"
        r = secret_client.post(
            f"{API}/session/swap-disc",
            json={"path": str(self._disc(broker_dirs))},
        )
        assert r.status_code == 409

    def test_the_route_needs_the_broker_secret(
        self, secret_client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
    ) -> None:
        """The swap route needs the broker secret."""
        self._session(secret_client, broker_dirs)
        del secret_client.headers["X-Broker-Secret"]
        r = secret_client.post(
            f"{API}/session/swap-disc",
            json={"path": str(self._disc(broker_dirs))},
        )
        assert r.status_code == 403

    def test_a_disc_under_a_symlinked_library_root_is_accepted(
        self,
        secret_client: TestClient,
        broker_dirs: dict[str, Path],
        fake_emulator: list[FakeEmulator],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A library root reached through a symlink still contains its own discs.

        The path RomM sends is resolved before the check, so a root left
        unresolved never matches and every swap on such a container is refused.
        """
        self._session(secret_client, broker_dirs)
        disc = self._disc(broker_dirs)
        link = tmp_path / "romm-link"
        link.symlink_to(broker_dirs["roms"])
        monkeypatch.setattr(settings, "ROM_ROOT", link)

        r = secret_client.post(
            f"{API}/session/swap-disc", json={"path": str(link / disc.name)}
        )

        assert r.status_code == 200
        assert fake_emulator[0].swapped_discs == [disc.resolve()]


class _RoomSocket:
    """A stand-in for a seated member's room socket that records what it is sent.

    Attributes:
        client_state: What the room fanout checks before sending; always connected.
        sent: Every payload this seat received, in order.
    """

    def __init__(self) -> None:
        """Build a connected socket with nothing sent to it yet."""
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Record a payload sent to this seat.

        Args:
            payload: The JSON message the broker produced.
        """
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        """Accept the close the session teardown sends.

        Args:
            code: The close code; unused.
        """


def _seat_room_sockets() -> tuple[_RoomSocket, _RoomSocket]:
    """Seat a recording socket as the controller and another as an invite guest.

    Returns:
        The controller's socket and the guest's socket.
    """
    controller = _RoomSocket()
    guest = _RoomSocket()
    session.ROOM["controller"] = {"websocket": controller}
    session.ROOM["viewers"] = {"guest-token": {"websocket": guest}}
    return controller, guest


def _chat_messages(socket: _RoomSocket) -> list[str]:
    """List the chat bodies a seat received.

    Args:
        socket: The recording socket to read.

    Returns:
        Every chat message body, in the order it arrived.
    """
    return [m["message"] for m in socket.sent if m.get("type") == "chat_message"]


def test_a_failed_save_dump_is_not_reported_as_a_successful_no_op(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dump that failed is reported as a failure, not as a session with nothing to save.

    Both produce no archive, and calling that a success is what has RomM file
    the session as saved and drop save data it never received.
    """
    monkeypatch.setattr(api, "TOKEN_CLEAR_GRACE_SECONDS", 0.0)
    _activate(client, broker_dirs)
    shutil.rmtree(fake_emulator[0].save_root)

    body = client.post(f"{API}/session/exit").json()

    assert body["upload"]["mode"] == "failed"
    assert body["upload"]["ok"] is False
    assert "save dump failed" in body["upload"]["error"]
    assert body["save_dump"]["error"]


def test_a_session_that_saved_nothing_still_reports_a_clean_exit(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that wrote no saves is a skipped upload, which is still a success."""
    monkeypatch.setattr(api, "TOKEN_CLEAR_GRACE_SECONDS", 0.0)
    _activate(client, broker_dirs)

    body = client.post(f"{API}/session/exit").json()

    assert body["upload"]["mode"] == "skipped"
    assert body["upload"]["ok"] is True
    assert body["save_dump"]["error"] is None


def test_a_second_activate_arriving_mid_launch_is_refused(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An activate arriving while another is still launching is refused rather than run.

    Holding the lock stands in for the first caller being somewhere between the
    409 gate and the session it has not written yet, which is the whole window
    in which two emulators can end up sharing one screen.
    """
    assert api._ACTIVATE_LOCK.acquire(blocking=False)
    try:
        response = _activate(client, broker_dirs)
    finally:
        api._ACTIVATE_LOCK.release()

    assert response.status_code == 409
    assert fake_emulator == []


def test_a_rom_under_a_symlinked_library_root_is_accepted(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A library root reached through a symlink still contains its own ROMs.

    The incoming path is resolved before the containment check, so a root left
    unresolved never matches and every launch on such a container is refused.
    """
    rom = _rom(broker_dirs)
    link = tmp_path / "romm-link"
    link.symlink_to(broker_dirs["roms"])
    monkeypatch.setattr(settings, "ROM_ROOT", link)

    response = _activate(
        client,
        broker_dirs,
        rom={"id": 5, "name": "Game", "platform": "ps2", "path": str(link / rom.name)},
    )

    assert response.status_code == 200
    assert fake_emulator[0].launched[0] == rom.resolve()


def test_a_join_says_when_selkies_never_took_the_seat(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A join whose token push was refused says so, rather than handing back a seat that cannot stream."""
    _activate(client, broker_dirs)

    async def _refuse(_session: dict[str, Any]) -> bool:
        """Refuse the push.

        Args:
            _session: The session whose tokens would have been pushed.

        Returns:
            Always False.
        """
        return False

    monkeypatch.setattr(selkies, "push_tokens", _refuse)

    body = client.post(f"{API}/session/join", json={"permission": "participant"}).json()

    assert body["selkies_tokens_pushed"] is False


def test_exit_reports_stream_tokens_it_could_not_clear(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit reports a token set selkies would not empty, so a live credential is not left unnoticed."""
    monkeypatch.setattr(api, "TOKEN_CLEAR_GRACE_SECONDS", 0.0)
    _activate(client, broker_dirs)

    async def _refuse() -> bool:
        """Refuse the clear.

        Returns:
            Always False.
        """
        return False

    monkeypatch.setattr(selkies, "clear_tokens", _refuse)

    body = client.post(f"{API}/session/exit").json()

    assert body["selkies_tokens_cleared"] is False


def test_the_room_gets_its_grace_period_before_the_tokens_go(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token clear waits out the grace period, so browsers can drop the stream first."""
    monkeypatch.setattr(api, "TOKEN_CLEAR_GRACE_SECONDS", 0.3)
    _activate(client, broker_dirs)
    cleared_at: list[float] = []

    async def _record() -> bool:
        """Record when the clear ran.

        Returns:
            Always True.
        """
        cleared_at.append(time.monotonic())
        return True

    monkeypatch.setattr(selkies, "clear_tokens", _record)
    started = time.monotonic()

    client.post(f"{API}/session/exit")

    assert cleared_at
    assert cleared_at[0] - started >= 0.3


def test_the_exit_summary_keeps_paths_urls_and_errors_off_the_invite_seats(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The room summary names no container path, callback URL or upload error; the controller gets those.

    An invite link seats anonymous guests, and the container layout and the
    parent's endpoints are not theirs to read.
    """
    monkeypatch.setattr(api, "TOKEN_CLEAR_GRACE_SECONDS", 0.0)

    async def _refuse(
        _cb: Optional[dict[str, Any]],
        _zip_bytes: bytes,
        _name: str,
        _sess: dict[str, Any],
    ) -> dict[str, Any]:
        """Report the upload as refused by the parent.

        Args:
            _cb: The session callback block.
            _zip_bytes: The archive that would have been sent.
            _name: The archive filename.
            _sess: The session being exited.

        Returns:
            A failed upload report naming the endpoint and the error.
        """
        return {
            "mode": "failed",
            "ok": False,
            "url": "https://romm.example/api/webstation/saves",
            "error": "connection refused",
        }

    monkeypatch.setattr(callback, "push_save_archive", _refuse)
    _activate(client, broker_dirs)
    _write_save_after_launch(fake_emulator[0].save_root / "saves" / "card.bin", b"played")
    controller, guest = _seat_room_sockets()

    body = client.post(f"{API}/session/exit").json()
    archive_path = body["save_dump"]["archive_path"]

    assert archive_path
    seen_by_guest = " ".join(_chat_messages(guest))
    assert archive_path not in seen_by_guest
    assert "romm.example" not in seen_by_guest
    assert "connection refused" not in seen_by_guest
    seen_by_controller = " ".join(_chat_messages(controller))
    assert archive_path in seen_by_controller
    assert "connection refused" in seen_by_controller


def test_two_state_pushes_never_share_a_staging_file(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each state push stages under its own name, so no two can interleave into one file.

    A shared staging name lets a second push write into the first one's file
    and the rename then publishes the mixture as a state.
    """
    _activate(client, broker_dirs)
    target = tmp_path / "GAME.03.p2s"
    fake_emulator[0].state_file = target
    staged: list[str] = []
    real_replace = os.replace

    def _spy(src: Path, dst: Path) -> None:
        """Record the staging path a push published from.

        Args:
            src: The staged file being renamed.
            dst: Where it is renamed to.
        """
        staged.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(api.os, "replace", _spy)
    for content in (b"first", b"second"):
        response = client.put(
            f"{API}/session/state-file", params={"filename": "GAME.01.p2s"}, content=content
        )
        assert response.status_code == 200

    assert len(set(staged)) == 2
    assert target.read_bytes() == b"second"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize("method", ["get", "put"])
def test_the_memory_card_cannot_be_touched_while_a_session_runs(
    method: str,
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
) -> None:
    """Neither card route runs mid-session: the emulator holds the card open for the whole game.

    Capture reads a card caught mid-write, and replace corrupts it outright.
    """
    _activate(client, broker_dirs)
    params = {"emulator": "pcsx2"}

    if method == "get":
        response = client.get(f"{API}/session/memory-card", params=params)
    else:
        response = client.put(
            f"{API}/session/memory-card", params=params, content=_zip({"a": b"x"})
        )

    assert response.status_code == 409
    assert list(broker_dirs["imports"].iterdir()) == []


def test_a_stored_import_leaves_no_staging_file_behind(
    client: TestClient, broker_dirs: dict[str, Path]
) -> None:
    """A stored import leaves only the archive activate is pointed at."""
    response = client.put(f"{API}/session/imports/sess-1.zip", content=_zip({"a": b"x"}))

    assert response.status_code == 200
    assert [p.name for p in broker_dirs["imports"].iterdir()] == ["sess-1.zip"]


def test_a_refused_import_leaves_nothing_on_disk(
    client: TestClient, broker_dirs: dict[str, Path]
) -> None:
    """An import refused for not being a zip leaves nothing staged."""
    response = client.put(f"{API}/session/imports/sess-1.zip", content=b"not a zip at all")

    assert response.status_code == 422
    assert list(broker_dirs["imports"].iterdir()) == []


def test_an_oversized_import_is_refused_without_keeping_the_body(
    client: TestClient, broker_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import over the size limit is refused and nothing it sent is kept."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 16)

    response = client.put(
        f"{API}/session/imports/sess-1.zip", content=_zip({"a": b"x" * 4096})
    )

    assert response.status_code == 413
    assert list(broker_dirs["imports"].iterdir()) == []


def test_an_oversized_card_push_is_refused_without_keeping_the_body(
    client: TestClient, broker_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card over the size limit is refused and nothing it sent is kept."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 16)

    response = client.put(
        f"{API}/session/memory-card",
        params={"emulator": "pcsx2"},
        content=_zip({"a": b"x" * 4096}),
    )

    assert response.status_code == 413
    assert list(broker_dirs["imports"].iterdir()) == []
