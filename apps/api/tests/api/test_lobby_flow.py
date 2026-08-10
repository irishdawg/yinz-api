"""End-to-end through the actual HTTP layer, not just the domain engine —
exercises routes.py's locking, receipts, and error handling."""

from __future__ import annotations

from .conftest import make_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_create_join_start_flow():
    client = make_client()

    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    assert created.status_code == 200
    game_id = created.json()["game_id"]
    join_code = created.json()["join_code"]

    joined = client.post("/games/join", json={"join_code": join_code, "display_name": "Mortia"}, headers=_auth("auth-mortia"))
    assert joined.status_code == 200
    assert joined.json()["game_id"] == game_id

    started = client.post(
        f"/games/{game_id}/commands",
        json={"command_id": "cmd-1", "type": "START_GAME", "payload": {}},
        headers=_auth("auth-tedy"),
    )
    assert started.status_code == 200
    assert started.json()["phase"] == "NEGOTIATION"

    view = client.get(f"/games/{game_id}", headers=_auth("auth-tedy"))
    assert view.status_code == 200
    assert len(view.json()["holdings"]) > 0


def test_duplicate_command_id_is_idempotent():
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    join_code = created.json()["join_code"]
    client.post("/games/join", json={"join_code": join_code, "display_name": "Mortia"}, headers=_auth("auth-mortia"))

    body = {"command_id": "cmd-1", "type": "START_GAME", "payload": {}}
    first = client.post(f"/games/{game_id}/commands", json=body, headers=_auth("auth-tedy"))
    second = client.post(f"/games/{game_id}/commands", json=body, headers=_auth("auth-tedy"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "applied"


def test_only_seated_players_can_submit_commands():
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]

    response = client.post(
        f"/games/{game_id}/commands",
        json={"command_id": "cmd-1", "type": "SET_READY_TO_CLOSE", "payload": {"ready": True}},
        headers=_auth("auth-stranger"),
    )
    assert response.status_code == 403


def test_create_game_accepts_expected_player_count():
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy", "expected_player_count": 4}, headers=_auth("auth-tedy"))
    assert created.status_code == 200
    game_id = created.json()["game_id"]

    view = client.get(f"/games/{game_id}", headers=_auth("auth-tedy"))
    assert view.json()["expected_player_count"] == 4


def test_create_game_rejects_out_of_range_expected_player_count():
    client = make_client()
    response = client.post("/games", json={"display_name": "Tedy", "expected_player_count": 1}, headers=_auth("auth-tedy"))
    assert response.status_code == 422  # Pydantic schema validation, not a domain error


def test_cannot_create_second_active_game_while_hosting_one():
    client = make_client()
    first = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    assert first.status_code == 200

    second = client.post("/games", json={"display_name": "Tedy again"}, headers=_auth("auth-tedy"))
    assert second.status_code == 409
    assert second.json()["detail"]["error_code"] == "active_game_exists"


def test_create_game_is_rate_limited_per_ip():
    client = make_client()
    # Different hosts each time so the one-active-game-per-host rule (tested
    # above) doesn't shadow what's actually being tested here.
    for i in range(5):
        response = client.post("/games", json={"display_name": f"P{i}"}, headers=_auth(f"auth-rl-{i}"))
        assert response.status_code == 200

    sixth = client.post("/games", json={"display_name": "P5"}, headers=_auth("auth-rl-5"))
    assert sixth.status_code == 429
    assert sixth.json() == {"error": "rate_limited"}  # boring on purpose — no limit numbers echoed back


def test_join_game_is_rate_limited_per_ip():
    client = make_client()
    for _ in range(10):
        response = client.post("/games/join", json={"join_code": "NOSUCH1", "display_name": "X"}, headers=_auth("auth-prober"))
        assert response.status_code == 404

    eleventh = client.post("/games/join", json={"join_code": "NOSUCH1", "display_name": "X"}, headers=_auth("auth-prober"))
    assert eleventh.status_code == 429
