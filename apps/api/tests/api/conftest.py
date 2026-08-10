from __future__ import annotations

from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from gotiate.api.deps import get_auth_user_id
from gotiate.main import create_app
from gotiate.settings import settings


async def _stub_get_auth_user_id(authorization: str | None = Header(default=None)) -> str:
    """Restores the pre-real-auth behavior for route/domain tests: the
    bearer token *is* the user id, no JWT involved. Only test_auth.py
    exercises real Supabase JWT verification — every other test here is
    about routing/locking/idempotency logic that doesn't care where an id
    came from."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")
    return token


def make_client() -> TestClient:
    """TestClient wired with the auth stub above and the real gateway
    secret (GatewaySecretMiddleware is app-wide ASGI middleware, so
    dependency_overrides can't reach it — every request needs the header,
    same as a real Next.js gateway call would send)."""
    app = create_app()
    app.dependency_overrides[get_auth_user_id] = _stub_get_auth_user_id
    return TestClient(app, headers={"x-gotiate-gateway-key": settings.gotiate_gateway_secret or ""})
