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
from .emulators import retroarch, retroarch_cores
from .emulators.base import reap_orphan
from .emulators.ppsspp import sweep_stale_extractions as sweep_ppsspp_extractions
from .emulators.rpcs3 import sweep_stale_extractions as sweep_rpcs3_extractions
from .emulators.scummvm import sweep_stale_extractions as sweep_scummvm_extractions
from .emulators.shadps4 import sweep_stale_extractions as sweep_shadps4_extractions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [broker] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

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

    Loads the RetroArch core catalog: the bundled one merged with a cache from
    a previous refresh, so a cache another process wrote is used right away.
    When `RETROARCH_CORE_INFO_REFRESH` is on, a background task then keeps that
    cache current; startup itself never waits on the network for it.

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
    await anyio.to_thread.run_sync(
        retroarch_cores.load_startup_catalog, retroarch.RA_DATA_DIR, retroarch.CORES_DIR, retroarch.PLATFORMS
    )
    async with anyio.create_task_group() as tg:
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
