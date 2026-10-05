"""FastAPI application factory.

The app is mounted under `settings.PREFIX` (default `/streaming`) so the same
paths work behind nginx, a reverse proxy, or uvicorn directly. Outside dev
mode the built frontend is served as static files at the prefix root; in dev
mode vite serves the frontend instead.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from . import api, room, settings
from .emulators import retroarch, retroarch_cores, rom_cache
from .emulators.base import reap_orphan
from .emulators.ppsspp import sweep_stale_extractions as sweep_ppsspp_extractions
from .emulators.retroarch import sweep_stale_extractions as sweep_retroarch_extractions
from .emulators.rpcs3 import sweep_stale_extractions as sweep_rpcs3_extractions
from .emulators.scummvm import sweep_stale_extractions as sweep_scummvm_extractions
from .emulators.shadps4 import sweep_stale_extractions as sweep_shadps4_extractions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [broker] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# httpx logs every request line, full URL and all, at INFO: that would put
# a callback base_url's credentials in the log. The broker logs each of its
# own requests already, redacted (see `callback.redact_url`).
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

_NO_AUTH_BANNER = (
    "\n"
    + "!" * 72
    + "\n"
    "!! BROKER_SECRET IS NOT SET - REFUSING TO START\n"
    "!!\n"
    "!! Every session-lifecycle endpoint (activate, save/load state,\n"
    "!! swap-disc, state-file, memory-card, exports, imports) would be\n"
    "!! reachable by ANY caller that can reach this port, with no auth.\n"
    "!!\n"
    "!! Set BROKER_SECRET to a strong random value before starting the\n"
    "!! broker, or set BROKER_DEV_MODE=true to explicitly run without\n"
    "!! authentication (local development only).\n" + "!" * 72
)


def enforce_auth_config() -> None:
    """Refuse to build an app that would serve every endpoint unauthenticated.

    Enforced here rather than in the console-script wrapper alone, because
    uvicorn and gunicorn are routinely pointed straight at `create_app` and
    would otherwise skip the gate entirely.

    Raises:
        SystemExit: When `BROKER_SECRET` is unset and dev mode is not explicitly
            enabled.
    """
    if not settings.BROKER_SECRET and not settings.DEV_MODE:
        log.critical(_NO_AUTH_BANNER)
        raise SystemExit(1)
    if not settings.BROKER_SECRET:
        log.warning(
            "BROKER_DEV_MODE is set, so the broker is starting with BROKER_SECRET "
            "unset: every session-lifecycle endpoint is unauthenticated. Do not "
            "expose this broker beyond local development."
        )


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Reap an emulator orphaned by a previous broker process before serving.

    A fresh broker holds no session, so an emulator recorded by the process
    that came before is playing to nobody. Killing it here rather than at the
    next activate is what keeps it killable at all: exit answers 409 without a
    session, so otherwise the only way out is launching another game.

    Also sweeps the shadPS4, RPCS3, ScummVM and PPSSPP extraction scratch
    dirs left behind by a crashed broker process, before any new extraction
    can be in flight.

    Trims the ROM cache, when it is enabled, to the limits now in force and
    drops a copy a crashed broker left half written.

    Loads the RetroArch core catalog: the bundled one merged with a cache from
    a previous refresh, so a cache another process wrote is used right away.
    When `RETROARCH_CORE_INFO_REFRESH` is on, a background task then keeps that
    cache current; startup itself never waits on the network for it.

    Starts the emulator watch, which logs an ERROR when a session's emulator
    exits on its own, unless `EMULATOR_WATCH_INTERVAL` is 0.

    Args:
        _app: The application being started; unused.

    Yields:
        Nothing; control passes to the running application once the orphan is reaped.
    """
    await anyio.to_thread.run_sync(reap_orphan)
    await anyio.to_thread.run_sync(sweep_shadps4_extractions)
    await anyio.to_thread.run_sync(sweep_rpcs3_extractions)
    await anyio.to_thread.run_sync(sweep_scummvm_extractions)
    await anyio.to_thread.run_sync(sweep_ppsspp_extractions)
    await anyio.to_thread.run_sync(sweep_retroarch_extractions)
    await anyio.to_thread.run_sync(rom_cache.startup)
    await anyio.to_thread.run_sync(
        retroarch_cores.load_startup_catalog, retroarch.RA_DATA_DIR, retroarch.CORES_DIR, retroarch.PLATFORMS
    )
    async with anyio.create_task_group() as tg:
        if settings.EMULATOR_WATCH_INTERVAL > 0:
            tg.start_soon(api.watch_emulator_forever, settings.EMULATOR_WATCH_INTERVAL)
        if settings.RETROARCH_CORE_INFO_REFRESH:
            # In the background: startup never waits on the network.
            tg.start_soon(
                retroarch_cores.refresh_forever,
                retroarch.RA_DATA_DIR,
                retroarch.CORES_DIR,
                retroarch.PLATFORMS,
            )
        yield
        tg.cancel_scope.cancel()


ROOM_CSP = "; ".join(
    (
        "default-src 'self'",
        # The media and socket workers and the audio worklets are built from
        # blob: URLs (room.js); nothing else runs that the page did not ship.
        "script-src 'self' blob:",
        "worker-src 'self' blob:",
        "connect-src 'self'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "font-src 'self' data:",
        # Inline style= attributes in markup room.js builds; no inline <style>.
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",
        # The selkies stream, served under the same prefix.
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    )
)
"""The room page's Content-Security-Policy, minus `frame-ancestors` (see `_page_csp_headers`)."""


def _page_csp_headers() -> dict[str, str]:
    """Build the CSP headers for an HTML page.

    `frame-ancestors` is always enforced when configured: browsers ignore it
    in a report-only policy, so it cannot wait for `BROKER_CSP_ENFORCE`.

    Returns:
        The header names and values to set; empty of `frame-ancestors` when
        `BROKER_FRAME_ANCESTORS` is unset.
    """
    ancestors = f"frame-ancestors {settings.FRAME_ANCESTORS}" if settings.FRAME_ANCESTORS else ""
    if settings.CSP_ENFORCE:
        return {"Content-Security-Policy": "; ".join(filter(None, (ROOM_CSP, ancestors)))}
    headers = {"Content-Security-Policy-Report-Only": ROOM_CSP}
    if ancestors:
        headers["Content-Security-Policy"] = ancestors
    return headers


def create_app() -> FastAPI:
    """Build the broker application, mounted under the configured prefix.

    The lifespan belongs to whichever app is actually served. Starlette never
    hands the lifespan scope to a mounted sub-app, so a startup hook on the
    inner app would silently never run behind a prefix.

    Returns:
        The application to serve: a bare root app with the broker mounted at
        `settings.PREFIX` when a prefix is set, otherwise the broker app itself.

    Raises:
        SystemExit: When the broker would come up with no authentication at all;
            see `enforce_auth_config`.
    """
    enforce_auth_config()
    prefixed = bool(settings.PREFIX)
    inner = FastAPI(
        title="webstation-broker", lifespan=None if prefixed else _lifespan
    )
    inner.include_router(api.router)
    inner.include_router(api.secret_router)
    inner.include_router(room.router)

    @inner.middleware("http")
    async def _no_referrer(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Session/viewer tokens travel as ?token= query params (WS auth has no
        # header alternative, and the room is iframe-embedded); a leaked
        # Referer header would hand a live token to whatever the page links
        # out to.
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # API answers can carry a live seat token (context's userToken) and
        # sit under a URL that carries one too; keep both out of any cache.
        if request.url.path.startswith(f"{settings.PREFIX}/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if response.headers.get("content-type", "").startswith("text/html"):
            for name, value in _page_csp_headers().items():
                response.headers[name] = value
        return response

    if not settings.DEV_MODE:
        if settings.FRONTEND_DIST.is_dir():
            inner.mount(
                "/",
                StaticFiles(directory=settings.FRONTEND_DIST, html=True),
                name="frontend",
            )
            log.info("serving the frontend from %s", settings.FRONTEND_DIST)
        else:
            log.error(
                "frontend dist %s is not a directory: the room UI is not being "
                "served, so every page under %s/ answers 404. Set "
                "BROKER_FRONTEND_DIST, or BROKER_DEV_MODE=true to let vite serve it.",
                settings.FRONTEND_DIST,
                settings.PREFIX,
            )

    if prefixed:
        root = FastAPI(lifespan=_lifespan)
        root.mount(settings.PREFIX, inner)
        return root
    return inner
