"""No free-text display names, ever -- create_game/join_game must reject
anything not in the curated seed pool. Uses an explicit `allowed_names` set
via conftest.make_client() rather than the default permissive fake, since
this is specifically what's under test here."""

from __future__ import annotations

from .conftest import make_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_create_game_rejects_a_name_outside_the_seed_pool():
    client = make_client(allowed_names={"Sly Fox"})
    response = client.post("/games", json={"display_name": "MyRealName"}, headers=_auth("auth-tedy"))
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_display_name"


def test_create_game_accepts_a_seeded_name():
    client = make_client(allowed_names={"Sly Fox"})
    response = client.post("/games", json={"display_name": "Sly Fox"}, headers=_auth("auth-tedy"))
    assert response.status_code == 200


def test_join_game_rejects_a_name_outside_the_seed_pool():
    client = make_client(allowed_names={"Sly Fox", "Clever Badger"})
    created = client.post("/games", json={"display_name": "Sly Fox"}, headers=_auth("auth-tedy"))
    join_code = created.json()["join_code"]

    response = client.post("/games/join", json={"join_code": join_code, "display_name": "NotInThePool"}, headers=_auth("auth-mortia"))
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_display_name"


def test_join_game_rejects_a_name_already_taken_in_this_game():
    client = make_client(allowed_names={"Sly Fox", "Clever Badger"})
    created = client.post("/games", json={"display_name": "Sly Fox"}, headers=_auth("auth-tedy"))
    join_code = created.json()["join_code"]

    # A different game_player trying to reuse the host's exact name.
    response = client.post("/games/join", json={"join_code": join_code, "display_name": "Sly Fox"}, headers=_auth("auth-mortia"))
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "name_taken"


def test_join_game_accepts_a_different_seeded_name():
    client = make_client(allowed_names={"Sly Fox", "Clever Badger"})
    created = client.post("/games", json={"display_name": "Sly Fox"}, headers=_auth("auth-tedy"))
    join_code = created.json()["join_code"]

    response = client.post("/games/join", json={"join_code": join_code, "display_name": "Clever Badger"}, headers=_auth("auth-mortia"))
    assert response.status_code == 200


def test_existing_tests_arbitrary_names_still_work_with_default_permissive_pool():
    # No allowed_names passed -- InMemoryPlayerNameRepository defaults to
    # permissive, matching every pre-existing test in this suite.
    client = make_client()
    response = client.post("/games", json={"display_name": "Whatever I Want"}, headers=_auth("auth-tedy"))
    assert response.status_code == 200
