"""Exercises the real auth path — every other test in tests/api/ uses the
stub override in conftest.py deliberately, since they're testing routing/
locking/idempotency, not auth itself."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gotiate.main import create_app
from gotiate.settings import settings


def test_garbage_bearer_token_is_rejected():
    # Real get_auth_user_id (no dependency_override) against a token that
    # was never signed by Supabase.
    client = TestClient(create_app(), headers={"x-gotiate-gateway-key": settings.gotiate_gateway_secret or ""})
    response = client.post(
        "/games",
        json={"display_name": "Tedy"},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_missing_gateway_secret_is_rejected():
    client = TestClient(create_app())  # no x-gotiate-gateway-key at all
    response = client.get("/games/some-id")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_wrong_gateway_secret_is_rejected():
    client = TestClient(create_app(), headers={"x-gotiate-gateway-key": "wrong-secret"})
    response = client.get("/games/some-id")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
