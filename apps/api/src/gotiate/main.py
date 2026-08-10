from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from gotiate.api.rate_limit import limiter
from gotiate.api.routes import router
from gotiate.persistence.repository import InMemoryGameRepository
from gotiate.settings import settings

_GATEWAY_HEADER = "x-gotiate-gateway-key"


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
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return await call_next(request)


def create_app() -> FastAPI:
    """Factory rather than a bare module-level app — tests need a fresh
    repository (and rate-limit state) per test rather than sharing either
    across the whole session."""
    limiter.reset()

    is_production = settings.environment != "development"
    app = FastAPI(
        title="Gotiate API",
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )
    app.state.repository = InMemoryGameRepository()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _boring_rate_limit_handler)

    # Middleware order matters: Starlette runs the *last*-added middleware
    # outermost (first on the request path), so GatewaySecretMiddleware —
    # added after SlowAPIMiddleware — rejects non-gateway traffic before it
    # ever reaches the rate limiter or route dependencies.
    app.add_middleware(SlowAPIMiddleware)
    exempt_paths = {"/health"}
    if not is_production:
        exempt_paths |= {"/docs", "/redoc", "/openapi.json"}
    app.add_middleware(GatewaySecretMiddleware, secret=settings.gotiate_gateway_secret or "", exempt_paths=exempt_paths)

    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
