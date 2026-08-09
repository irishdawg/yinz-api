from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from gotiate.api.rate_limit import limiter
from gotiate.api.routes import router
from gotiate.persistence.repository import InMemoryGameRepository
from gotiate.settings import settings


async def _boring_rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    # Deliberately not slowapi's default handler, which echoes the configured
    # limit ("5 per 1 hour") back to the caller — that's exactly the kind of
    # detail an unauthorized caller shouldn't get for free.
    return JSONResponse(status_code=429, content={"error": "rate_limited"})


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
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
