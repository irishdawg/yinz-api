"""GET /games/{id}/events through the real HTTP layer -- routes.py's
resolution of the caller to an audience, not just project_events() in
isolation (already covered thoroughly in tests/domain/test_event_visibility.py)."""

from __future__ import annotations

from .conftest import make_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _start_two_player_game(client):
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    join_code = created.json()["join_code"]
    client.post("/games/join", json={"join_code": join_code, "display_name": "Mortia"}, headers=_auth("auth-mortia"))
    client.post(f"/games/{game_id}/commands", json={"command_id": "cmd-start", "type": "START_GAME", "payload": {}}, headers=_auth("auth-tedy"))
    return game_id


def test_events_endpoint_returns_public_events_to_a_seated_player():
    client = make_client()
    game_id = _start_two_player_game(client)

    response = client.get(f"/games/{game_id}/events", headers=_auth("auth-tedy"))
    assert response.status_code == 200
    types = {e["type"] for e in response.json()}
    assert "GAME_STARTED" in types
    assert "GAME_CREATED" in types
    # SERVER_ONLY, never live -- confirms the route wires real visibility,
    # not just "everything in the ledger."
    assert "WATERLINE_SELECTED" not in types


def test_events_endpoint_redacts_private_pool_contents_for_non_insider():
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    join_code = created.json()["join_code"]
    client.post("/games/join", json={"join_code": join_code, "display_name": "Mortia"}, headers=_auth("auth-mortia"))
    client.post("/games/join", json={"join_code": join_code, "display_name": "Hanky"}, headers=_auth("auth-hanky"))
    client.post("/games/join", json={"join_code": join_code, "display_name": "Josiah"}, headers=_auth("auth-josiah"))
    client.post(f"/games/{game_id}/commands", json={"command_id": "cmd-start", "type": "START_GAME", "payload": {}}, headers=_auth("auth-tedy"))

    view = client.get(f"/games/{game_id}", headers=_auth("auth-tedy")).json()
    entities = [m["entity_id"] for m in view["market"]]

    propose = client.post(
        f"/games/{game_id}/commands",
        json={"command_id": "cmd-propose", "type": "PROPOSE_SWAP", "payload": {"entity_a": entities[0], "entity_b": entities[1]}},
        headers=_auth("auth-tedy"),
    )
    assert propose.status_code == 200
    proposal_id = next(iter(propose.json()["proposals"]))["proposal_id"]

    pool = client.post(
        f"/games/{game_id}/commands",
        json={
            "command_id": "cmd-pool",
            "type": "CREATE_POOL",
            "payload": {"proposal_id": proposal_id, "entity_c": entities[2], "entity_d": entities[3], "visibility": "private"},
        },
        headers=_auth("auth-mortia"),
    )
    assert pool.status_code == 200

    outsider_events = client.get(f"/games/{game_id}/events", headers=_auth("auth-hanky")).json()
    pool_event = next(e for e in outsider_events if e["type"] == "PRIVATE_POOL_CREATED")
    assert "entity_c" not in pool_event["payload"]

    insider_events = client.get(f"/games/{game_id}/events", headers=_auth("auth-mortia")).json()
    insider_pool_event = next(e for e in insider_events if e["type"] == "PRIVATE_POOL_CREATED")
    assert insider_pool_event["payload"]["entity_c"] == entities[2]


def test_events_endpoint_requires_auth():
    client = make_client()
    game_id = _start_two_player_game(client)
    response = client.get(f"/games/{game_id}/events")
    assert response.status_code == 401


def test_events_endpoint_404s_for_unknown_game():
    client = make_client()
    response = client.get("/games/does-not-exist/events", headers=_auth("auth-tedy"))
    assert response.status_code == 404
