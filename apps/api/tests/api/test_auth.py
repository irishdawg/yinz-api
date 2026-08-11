"""Exercises the real auth path — every other test in tests/api/ uses the
stub override in conftest.py deliberately, since they're testing routing/
locking/idempotency, not auth itself."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gotiate.main import create_app
from gotiate.persistence.player_names import InMemoryPlayerNameRepository
from gotiate.persistence.repository import InMemoryGameRepository
from gotiate.settings import settings


def test_garbage_bearer_token_is_rejected():
    # Real get_auth_user_id (no dependency_override) against a token that
    # was never signed by Supabase.
    client = TestClient(create_app(repository=InMemoryGameRepository(), player_name_repository=InMemoryPlayerNameRepository()), headers={"x-gotiate-gateway-key": settings.gotiate_gateway_secret or ""})
    response = client.post(
        "/games",
        json={"display_name": "Tedy"},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_missing_gateway_secret_gets_disguised_as_not_found():
    # 404, not 401 -- deliberately indistinguishable from a route that
    # doesn't exist. No hint that a protected API is even there.
    client = TestClient(create_app(repository=InMemoryGameRepository(), player_name_repository=InMemoryPlayerNameRepository()))  # no x-gotiate-gateway-key at all
    response = client.get("/games/some-id")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_wrong_gateway_secret_gets_disguised_as_not_found():
    client = TestClient(create_app(repository=InMemoryGameRepository(), player_name_repository=InMemoryPlayerNameRepository()), headers={"x-gotiate-gateway-key": "wrong-secret"})
    response = client.get("/games/some-id")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_gateway_rejection_matches_a_real_404_byte_for_byte():
    client = TestClient(create_app(repository=InMemoryGameRepository(), player_name_repository=InMemoryPlayerNameRepository()))
    rejected = client.get("/games/some-id")
    real_404 = TestClient(
        create_app(repository=InMemoryGameRepository(), player_name_repository=InMemoryPlayerNameRepository()), headers={"x-gotiate-gateway-key": settings.gotiate_gateway_secret or ""}
    ).get("/totally-nonexistent-path")
    assert rejected.status_code == real_404.status_code
    assert rejected.text == real_404.text


def test_successful_request_echoes_request_id():
    client = TestClient(create_app(repository=InMemoryGameRepository(), player_name_repository=InMemoryPlayerNameRepository()), headers={"x-gotiate-gateway-key": settings.gotiate_gateway_secret or ""})
    response = client.get("/health", headers={"x-request-id": "test-request-id-123"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "test-request-id-123"


def test_request_id_is_generated_when_absent():
    client = TestClient(create_app(repository=InMemoryGameRepository(), player_name_repository=InMemoryPlayerNameRepository()), headers={"x-gotiate-gateway-key": settings.gotiate_gateway_secret or ""})
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-request-id"]  # non-empty, some UUID was generated
