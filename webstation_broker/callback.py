"""Exit-time pushes to the parent (RomM): the save archive, and a changed RetroAchievements login.

The broker is normally served same-origin under the parent's `SUBFOLDER`, so
activate derives the callback base URL from the request that launched the
session; an explicit `callback.base_url` in the activate payload overrides it
for split-origin deployments.
"""

import logging
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import settings
from .emulators.base import RetroAchievementsChange

log = logging.getLogger(__name__)


def redact_url(url: str) -> str:
    """Drop any `user:password@` from a URL, for logs and reports.

    A split-origin `callback.base_url` may carry credentials in its userinfo;
    they authenticate the upload but have no business in the broker's log.

    Args:
        url: The URL to redact.

    Returns:
        The URL with its userinfo replaced by `***@`, or unchanged when it has none.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=f"***@{host}"))


def public_view(callback: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the callback info that is safe to echo in reports: everything but the token.

    Any credentials in `base_url` are redacted as well (see `redact_url`).

    Args:
        callback: The session's callback dict, or None when the session has none.

    Returns:
        A copy of `callback` without its `token` key, or None when `callback` is
        empty or None.
    """
    if not callback:
        return None
    view = {k: v for k, v in callback.items() if k != "token"}
    if view.get("base_url"):
        view["base_url"] = redact_url(view["base_url"])
    return view


async def push_save_archive(
    callback: dict[str, Any], zip_bytes: bytes, filename: str, sess: dict[str, Any]
) -> dict[str, Any]:
    """POST the save archive to the callback origin as multipart form data.

    Failures are reported, never raised: exit teardown must finish regardless.

    Args:
        callback: The session's callback dict; `base_url` is required and
            `token`, when present, is sent as a bearer token.
        zip_bytes: The archive body.
        filename: The filename to attach to the multipart `archive` field.
        sess: The session the archive belongs to; its id, emulator and rom are
            sent as form fields alongside the archive.

    Returns:
        A report of the shape `{"mode": "uploaded" | "failed", "ok": bool, "url": str, ...}`,
        carrying `status_code` when the server answered and `error` when the
        upload failed.
    """
    url = callback["base_url"].rstrip("/") + settings.SAVE_UPLOAD_PATH
    shown = redact_url(url)
    headers = {}
    if callback.get("token"):
        headers["Authorization"] = f"Bearer {callback['token']}"
    rom = sess.get("rom") or {}
    data = {"session_id": sess["id"], "emulator": sess["emulator"]}
    if rom.get("id") is not None:
        data["rom_id"] = str(rom["id"])
    if rom.get("name"):
        data["rom_name"] = rom["name"]
    files = {"archive": (filename, zip_bytes, "application/zip")}
    try:
        async with httpx.AsyncClient(timeout=settings.SAVE_UPLOAD_TIMEOUT) as client:
            resp = await client.post(url, data=data, files=files, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log.warning("save upload rejected by %s: HTTP %d", shown, exc.response.status_code)
        return {
            "mode": "failed",
            "ok": False,
            "url": shown,
            "status_code": exc.response.status_code,
            "error": f"upload rejected: HTTP {exc.response.status_code}",
        }
    except Exception as exc:
        log.warning("save upload to %s failed: %s", shown, exc)
        return {"mode": "failed", "ok": False, "url": shown, "error": str(exc)}
    log.info("save upload: %d bytes to %s (HTTP %d)", len(zip_bytes), shown, resp.status_code)
    return {"mode": "uploaded", "ok": True, "url": shown, "status_code": resp.status_code}


async def push_ra_login(
    callback: dict[str, Any], sess: dict[str, Any], change: RetroAchievementsChange
) -> dict[str, Any]:
    """PUT the player's changed RetroAchievements login to the callback origin as JSON.

    The body is `{"session_id", "emulator", "user", "retroachievements"}`,
    where `user` is the session's `id` and `username` (either may be null)
    and `retroachievements` is `{"username", "token"}`, or null when the
    player logged out. Only RomM ever sees the token: the returned report and
    every log line leave it out. Failures are reported, never raised.

    Args:
        callback: The session's callback dict; `base_url` is required and
            `token`, when present, is sent as a bearer token.
        sess: The session whose player the login belongs to.
        change: The login the session ended with.

    Returns:
        A report of the shape `{"mode": "reported" | "failed", "ok": bool, "url": str,
        "change": "set" | "cleared", ...}`, carrying `status_code` when the
        server answered and `error` when the push failed.
    """
    url = callback["base_url"].rstrip("/") + settings.RA_LOGIN_PATH
    shown = redact_url(url)
    headers = {}
    if callback.get("token"):
        headers["Authorization"] = f"Bearer {callback['token']}"
    user = sess.get("user") or {}
    login = change.login
    body = {
        "session_id": sess["id"],
        "emulator": sess["emulator"],
        "user": {"id": user.get("id"), "username": user.get("username")},
        "retroachievements": (
            {"username": login.username, "token": login.token} if login is not None else None
        ),
    }
    kind = change.kind
    try:
        async with httpx.AsyncClient(timeout=settings.RA_LOGIN_TIMEOUT) as client:
            resp = await client.put(url, json=body, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log.warning("ra login report rejected by %s: HTTP %d", shown, exc.response.status_code)
        return {
            "mode": "failed",
            "ok": False,
            "url": shown,
            "change": kind,
            "status_code": exc.response.status_code,
            "error": f"report rejected: HTTP {exc.response.status_code}",
        }
    except Exception as exc:
        # httpx never puts the request body in an exception message, so the
        # token cannot ride out on `exc`.
        log.warning("ra login report to %s failed: %s", shown, exc)
        return {"mode": "failed", "ok": False, "url": shown, "change": kind, "error": str(exc)}
    log.info("ra login report: %s to %s (HTTP %d)", kind, shown, resp.status_code)
    return {"mode": "reported", "ok": True, "url": shown, "change": kind, "status_code": resp.status_code}
