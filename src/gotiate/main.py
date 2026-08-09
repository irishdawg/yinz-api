from __future__ import annotations

from fastapi import FastAPI

from gotiate.api.routes import router
from gotiate.persistence.repository import InMemoryGameRepository


def create_app() -> FastAPI:
    """Factory rather than a bare module-level app — tests need a fresh
    repository per test rather than sharing one across the whole session."""
    app = FastAPI(title="Gotiate API")
    app.state.repository = InMemoryGameRepository()
    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
