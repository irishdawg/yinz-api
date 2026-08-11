from __future__ import annotations

import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from gotiate.api.rate_limit import limiter
from gotiate.api.routes import player_names_router, router, theme_sets_router
from gotiate.persistence.player_names import InMemoryPlayerNameRepository, PlayerNameRepository
from gotiate.persistence.repository import GameRepository, InMemoryGameRepository
from gotiate.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("gotiate")

# NOTE for local Windows dev only (Render/Linux is unaffected either way):
# psycopg's async mode needs a selector-based event loop. uvicorn hardcodes
# asyncio.ProactorEventLoop on Windows (uvicorn/loops/asyncio.py) UNLESS
# use_subprocess is true (reload or workers > 1) — a global
# set_event_loop_policy() here would have no effect, since uvicorn passes an
# explicit loop_factory to asyncio.Runner, bypassing the policy entirely.
# Confirmed empirically: `uvicorn gotiate.main:app --reload` gets a working
# SelectorEventLoop for free; the same command without --reload doesn't.
# Always run local dev with --reload (already the documented command).

_GATEWAY_HEADER = "x-gotiate-gateway-key"
_REQUEST_ID_HEADER = "x-request-id"


async def _boring_rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    # Deliberately not slowapi's default handler, which echoes the configured
    # limit ("5 per 1 hour") back to the caller — that's exactly the kind of
    # detail an unauthorized caller shouldn't get for free.
    return JSONResponse(status_code=429, content={"error": "rate_limited"})


class GatewaySecretMiddleware(BaseHTTPMiddleware):
    """The browser never calls this API directly — only Next.js's
    server-side Route Handlers do. This rejects anything that doesn't carry
    the shared secret before it reaches JWT verification or rate limiting,
    so Render's public URL is useless to anyone who isn't our own gateway.

    Fail-closed by construction: every path is protected unless explicitly
    exempted below, rather than requiring each route to opt in.
    """

    def __init__(self, app: ASGIApp, secret: str, exempt_paths: set[str]) -> None:
        super().__init__(app)
        self._secret = secret
        self._exempt_paths = exempt_paths

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._exempt_paths:
            return await call_next(request)
        # An unconfigured secret must reject everything, not accept
        # everything — compare_digest("", "") is True, which would
        # otherwise turn a missing GOTIATE_GATEWAY_SECRET into a silent
        # no-op instead of a loud failure.
        provided = request.headers.get(_GATEWAY_HEADER, "")
        if not self._secret or not secrets.compare_digest(provided, self._secret):
            # 404, not 401 — a 401 confirms "there's a protected API here."
            # This body is byte-for-byte Starlette's own default 404 (see
            # the plan doc / commit message), so a direct hit on Render's
            # public URL is indistinguishable from a genuinely undefined
            # route. Logged server-side only, never in the response.
            logger.warning("DIRECT_ORIGIN_REQUEST_REJECTED path=%s", request.url.path)
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Crude but real: a request id that survives browser -> Vercel ->
    Render -> back, plus elapsed-ms logging on this end, so a specific
    slow/broken command can be traced across both services' logs instead
    of guessed at. Runs only for traffic that already passed the gateway
    check (added before GatewaySecretMiddleware, so it's inside that
    layer) -- no reason to pay for this on traffic that's about to be
    bounced anyway.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(_REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.monotonic()
        logger.info("request_start id=%s method=%s path=%s", request_id, request.method, request.url.path)
        response = await call_next(request)
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.info(
            "request_end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response


def create_app(*, repository: GameRepository, player_name_repository: PlayerNameRepository, lifespan=None) -> FastAPI:
    """Factory rather than a bare module-level app — tests need a fresh
    rate-limit state per test rather than sharing it across the whole
    session. `repository`/`player_name_repository` are required, not
    defaulted: `.env` already has a live `GOTIATE_BACKEND_DATABASE_URL`
    loaded unconditionally for every process including test runs, so any
    design that picked in-memory-vs-Postgres by checking whether that
    value is set would make the test suite start hitting real Supabase the
    moment this landed. Requiring both explicitly removes that failure
    mode structurally: every test passes InMemoryGameRepository() and
    InMemoryPlayerNameRepository(); only the module-level `app` below
    (what `uvicorn gotiate.main:app` actually serves, never imported by
    tests) constructs and passes the real Postgres-backed ones."""
    limiter.reset()

    is_production = settings.environment != "development"
    app = FastAPI(
        title="Gotiate API",
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.player_name_repository = player_name_repository
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _boring_rate_limit_handler)

    # Middleware order matters: Starlette runs the *last*-added middleware
    # outermost (first on the request path). Added in this order so
    # GatewaySecretMiddleware runs first and rejects non-gateway traffic
    # as cheaply as possible — before it ever pays for request-id
    # generation, timing, or reaching the rate limiter/route dependencies.
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    exempt_paths = {"/health"}
    if not is_production:
        exempt_paths |= {"/docs", "/redoc", "/openapi.json"}
    app.add_middleware(GatewaySecretMiddleware, secret=settings.gotiate_gateway_secret or "", exempt_paths=exempt_paths)

    app.include_router(router)
    app.include_router(player_names_router)
    app.include_router(theme_sets_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _create_production_app() -> FastAPI:
    if not settings.gotiate_backend_database_url:
        logger.warning("GOTIATE_BACKEND_DATABASE_URL is not set — falling back to in-memory repositories. Games will not persist.")
        return create_app(repository=InMemoryGameRepository(), player_name_repository=InMemoryPlayerNameRepository())

    # Local import: only pulls in psycopg/psycopg_pool when actually needed,
    # so an in-memory-only environment (e.g. a contributor's machine
    # without a DB configured) doesn't need them installed to run the app.
    from gotiate.persistence.player_names import PostgresPlayerNameRepository
    from gotiate.persistence.postgres_repository import PostgresGameRepository, create_pool

    # Constructing the pool is synchronous (only .open() is a coroutine),
    # so both repositories can be built right away and handed to
    # create_app() like any other — the lifespan below just opens/closes
    # the one shared pool object around the app's actual runtime.
    pool = create_pool(
        settings.gotiate_backend_database_url,
        min_size=settings.gotiate_db_pool_min_size,
        max_size=settings.gotiate_db_pool_max_size,
    )
    repository = PostgresGameRepository(pool)
    player_name_repository = PostgresPlayerNameRepository(pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await pool.open()
        # Fail loudly at process start if the connection string is broken,
        # not on the first real request.
        async with pool.connection() as conn:
            await conn.execute("select 1")
        logger.info("Postgres pool opened and health-checked")
        yield
        await pool.close()

    return create_app(repository=repository, player_name_repository=player_name_repository, lifespan=lifespan)


app = _create_production_app()
