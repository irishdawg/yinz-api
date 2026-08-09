from __future__ import annotations

from fastapi.testclient import TestClient

from gotiate.main import create_app
from gotiate.settings import settings


def test_health():
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_enabled_in_development():
    assert settings.environment == "development"  # sanity-check the ambient test settings
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_docs_disabled_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
