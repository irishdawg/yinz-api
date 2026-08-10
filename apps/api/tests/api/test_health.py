from __future__ import annotations

from gotiate.settings import settings

from .conftest import make_client


def test_health():
    client = make_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_exempt_from_gateway_secret():
    """/health has to work without the gateway header — Render's own health
    checker doesn't send custom headers."""
    client = make_client()
    client.headers.pop("x-gotiate-gateway-key")
    response = client.get("/health")
    assert response.status_code == 200


def test_docs_enabled_in_development():
    assert settings.environment == "development"  # sanity-check the ambient test settings
    client = make_client()
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_docs_disabled_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    client = make_client()
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
