"""End-to-end through the actual HTTP layer, not just the domain engine —
exercises routes.py's locking, receipts, and error handling."""

from __future__ import annotations

from datetime import timedelta

from gotiate.api.routes import _sync_due_time_transitions
from gotiate.domain import engine
from gotiate.domain.entities import GamePhase
from gotiate.persistence.repository import InMemoryGameRepository

from tests.conftest import now as domain_now

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


def test_create_game_accepts_a_real_theme_set_id():
    from gotiate.domain import themes

    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy", "theme_set_id": "dragons_v1"}, headers=_auth("auth-tedy"))
    assert created.status_code == 200
    game_id = created.json()["game_id"]

    client.post("/games/join", json={"join_code": created.json()["join_code"], "display_name": "Mortia"}, headers=_auth("auth-mortia"))
    client.post(f"/games/{game_id}/commands", json={"command_id": "cmd-1", "type": "START_GAME", "payload": {}}, headers=_auth("auth-tedy"))

    view = client.get(f"/games/{game_id}", headers=_auth("auth-tedy"))
    dragon_names = {e.display_name for e in themes.get_theme_set("dragons_v1").entities}
    assert {m["display_name"] for m in view.json()["market"]} <= dragon_names


def test_create_game_rejects_unknown_theme_set_id():
    client = make_client()
    response = client.post("/games", json={"display_name": "Tedy", "theme_set_id": "does_not_exist_v1"}, headers=_auth("auth-tedy"))
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "unknown_theme_set"


def test_list_theme_sets_returns_the_real_sets():
    client = make_client()
    response = client.get("/theme-sets", headers=_auth("auth-tedy"))
    assert response.status_code == 200
    ids = {t["theme_set_id"] for t in response.json()}
    assert {"fictional_companies_v1", "dragons_v1", "cats_v1"} <= ids


def test_host_can_cancel_a_lobby_game_and_then_create_a_new_one():
    client = make_client()
    first = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = first.json()["game_id"]

    cancelled = client.post(
        f"/games/{game_id}/commands", json={"command_id": "cmd-1", "type": "CANCEL_GAME", "payload": {}}, headers=_auth("auth-tedy")
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["phase"] == "CANCELLED"

    second = client.post("/games", json={"display_name": "Tedy again"}, headers=_auth("auth-tedy"))
    assert second.status_code == 200


def test_cancel_is_rejected_once_negotiation_has_started():
    # Once real gameplay is underway, the host loses the unilateral power
    # to nuke it for everyone else -- the negotiation clock is the only
    # thing that ends an abandoned in-progress game from here on.
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    join_code = created.json()["join_code"]
    client.post("/games/join", json={"join_code": join_code, "display_name": "Mortia"}, headers=_auth("auth-mortia"))
    client.post(f"/games/{game_id}/commands", json={"command_id": "cmd-1", "type": "START_GAME", "payload": {}}, headers=_auth("auth-tedy"))

    response = client.post(
        f"/games/{game_id}/commands", json={"command_id": "cmd-2", "type": "CANCEL_GAME", "payload": {}}, headers=_auth("auth-tedy")
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "illegal_command"


def test_extend_lobby_timer_pushes_the_deadline_out():
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    before = client.get(f"/games/{game_id}", headers=_auth("auth-tedy")).json()["lobby_reminder_deadline_at"]

    response = client.post(
        f"/games/{game_id}/commands", json={"command_id": "cmd-1", "type": "EXTEND_LOBBY_TIMER", "payload": {}}, headers=_auth("auth-tedy")
    )
    assert response.status_code == 200
    assert response.json()["lobby_reminder_deadline_at"] > before


def test_only_the_host_can_extend_the_lobby_timer():
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    join_code = created.json()["join_code"]
    client.post("/games/join", json={"join_code": join_code, "display_name": "Mortia"}, headers=_auth("auth-mortia"))

    response = client.post(
        f"/games/{game_id}/commands", json={"command_id": "cmd-1", "type": "EXTEND_LOBBY_TIMER", "payload": {}}, headers=_auth("auth-mortia")
    )
    assert response.status_code == 409


def test_only_the_host_can_cancel():
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    join_code = created.json()["join_code"]
    client.post("/games/join", json={"join_code": join_code, "display_name": "Mortia"}, headers=_auth("auth-mortia"))

    response = client.post(
        f"/games/{game_id}/commands", json={"command_id": "cmd-1", "type": "CANCEL_GAME", "payload": {}}, headers=_auth("auth-mortia")
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "illegal_command"


def test_cancelling_an_already_cancelled_game_is_rejected():
    client = make_client()
    created = client.post("/games", json={"display_name": "Tedy"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    client.post(f"/games/{game_id}/commands", json={"command_id": "cmd-1", "type": "CANCEL_GAME", "payload": {}}, headers=_auth("auth-tedy"))

    response = client.post(
        f"/games/{game_id}/commands", json={"command_id": "cmd-2", "type": "CANCEL_GAME", "payload": {}}, headers=_auth("auth-tedy")
    )
    assert response.status_code == 409


async def test_a_plain_read_alone_triggers_the_lobby_auto_cancel():
    # The whole point of the lobby-timeout feature: once the host goes
    # quiet, nobody is left to submit a command, so a GET has to be able
    # to trigger the transition on its own -- see
    # routes._sync_due_time_transitions.
    repo = InMemoryGameRepository()
    game, events = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=domain_now())
    await repo.create(game)
    await repo.append_events(events)

    past_grace = game.lobby_reminder_deadline_at + timedelta(seconds=game.config.lobby_reminder_grace_seconds + 1)
    synced = await _sync_due_time_transitions(repo, game, past_grace)

    assert synced.phase == GamePhase.CANCELLED
    reloaded = await repo.get(game.id)
    assert reloaded.phase == GamePhase.CANCELLED


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
