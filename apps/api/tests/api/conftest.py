from __future__ import annotations

from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from gotiate.api.deps import get_auth_user_id
from gotiate.main import create_app
from gotiate.persistence.player_names import InMemoryPlayerNameRepository, RandomLike
from gotiate.persistence.repository import InMemoryGameRepository
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


def make_client(
    *, allowed_names: set[str] | None = None, golden_names: set[str] | None = None, rng: RandomLike | None = None
) -> TestClient:
    """TestClient wired with the auth stub above and the real gateway
    secret (GatewaySecretMiddleware is app-wide ASGI middleware, so
    dependency_overrides can't reach it — every request needs the header,
    same as a real Next.js gateway call would send).

    `allowed_names` defaults to None, which makes InMemoryPlayerNameRepository
    permissive (any display_name is accepted) -- most tests here predate the
    seed-name requirement and use arbitrary strings like "Tedy"/"Mortia", and
    shouldn't need to know about it. Pass an explicit set (and `golden_names`,
    a subset of it) only for tests specifically about name validation/offering.

    `rng` is constructor-level on InMemoryPlayerNameRepository (not a
    per-call argument), specifically so a forced RNG can be exercised
    through real create_game/join_game HTTP calls -- routes.py calls
    roll_golden_name() with no rng argument of its own, so this is the only
    seam that reaches it from a test."""
    # Explicit repositories, always -- create_app() has no defaults
    # specifically so tests can never accidentally fall through to a real
    # Postgres connection (see main.py's create_app docstring).
    name_repo_kwargs = {"rng": rng} if rng is not None else {}
    app = create_app(
        repository=InMemoryGameRepository(),
        player_name_repository=InMemoryPlayerNameRepository(allowed_names, golden_names, **name_repo_kwargs),
    )
    app.dependency_overrides[get_auth_user_id] = _stub_get_auth_user_id
    return TestClient(app, headers={"x-gotiate-gateway-key": settings.gotiate_gateway_secret or ""})
