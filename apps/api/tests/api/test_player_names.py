"""No free-text display names, ever -- create_game/join_game must reject
anything not in the curated seed pool. Uses an explicit `allowed_names` set
via conftest.make_client() rather than the default permissive fake, since
this is specifically what's under test here.

Golden names: rolled once, server-side, at the moment a real seat is
created (create_game's host, join_game's joiner) -- never by the free
-preview offer/reroll endpoints. See player_names.py's module docstring
for the reasoning."""

from __future__ import annotations

from gotiate.persistence.player_names import InMemoryPlayerNameRepository, NoAvailableNameError

from .conftest import make_client


class _FixedRandom:
    """Deterministic stand-in for the `random` module's `.random()` --
    forces roll_golden_name's coin-flip to hit or miss on demand, rather
    than relying on statistical assertions over many calls."""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


_ALWAYS_GOLDEN = _FixedRandom(0.0)  # 0.0 < 1/500, always hits
_NEVER_GOLDEN = _FixedRandom(0.999)  # never hits


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


# --- roll_golden_name / offer_name: direct repository tests ---
# HTTP can force the golden roll deterministically now (rng is
# constructor-level, see make_client), but these repository-level tests
# are still the clearest place to pin down the exact draw/exclude/pool
# -exhaustion behavior.


async def test_roll_golden_name_returns_golden_on_a_forced_hit():
    repo = InMemoryPlayerNameRepository({"Sly Fox", "Golden One"}, golden_names={"Golden One"}, rng=_ALWAYS_GOLDEN)
    assert await repo.roll_golden_name() == "Golden One"


async def test_roll_golden_name_returns_none_on_a_forced_miss():
    repo = InMemoryPlayerNameRepository({"Sly Fox", "Golden One"}, golden_names={"Golden One"}, rng=_NEVER_GOLDEN)
    assert await repo.roll_golden_name() is None


async def test_roll_golden_name_returns_none_not_an_error_when_golden_pool_is_excluded():
    # Forced hit, but the only golden name is excluded (e.g. someone else
    # in the game already has it) -- must not error, must fall through to
    # None so the caller keeps the player's ordinary name.
    repo = InMemoryPlayerNameRepository({"Sly Fox", "Golden One"}, golden_names={"Golden One"}, rng=_ALWAYS_GOLDEN)
    assert await repo.roll_golden_name(exclude={"Golden One"}) is None


async def test_offer_name_is_never_golden_even_with_a_forced_hit_rng():
    # offer_name doesn't consult the rng/golden pool at all -- it
    # structurally cannot draw gold, not just "usually doesn't."
    repo = InMemoryPlayerNameRepository({"Sly Fox", "Clever Badger", "Golden One"}, golden_names={"Golden One"}, rng=_ALWAYS_GOLDEN)
    for _ in range(20):
        name = await repo.offer_name()
        assert name != "Golden One"


async def test_offer_name_never_returns_an_excluded_name():
    repo = InMemoryPlayerNameRepository({"Sly Fox", "Clever Badger"})
    name = await repo.offer_name(exclude={"Sly Fox"})
    assert name == "Clever Badger"


async def test_offer_name_raises_when_pool_is_exhausted():
    repo = InMemoryPlayerNameRepository({"Sly Fox"})
    try:
        await repo.offer_name(exclude={"Sly Fox"})
        raise AssertionError("expected NoAvailableNameError")
    except NoAvailableNameError:
        pass


# --- offer endpoints: through the API -- never golden ---


def test_offer_initial_name_endpoint_returns_a_name():
    client = make_client(allowed_names={"Sly Fox", "Clever Badger"})
    response = client.get("/player-names/initial", headers=_auth("auth-tedy"))
    assert response.status_code == 200
    assert response.json()["name"] in {"Sly Fox", "Clever Badger"}


def test_offer_initial_name_endpoint_is_never_golden_even_with_a_forced_hit_rng():
    client = make_client(allowed_names={"Sly Fox", "Golden One"}, golden_names={"Golden One"}, rng=_ALWAYS_GOLDEN)
    response = client.get("/player-names/initial", headers=_auth("auth-tedy"))
    assert response.status_code == 200
    assert response.json() == {"name": "Sly Fox"}


def test_offer_reroll_endpoint_excludes_the_given_name():
    client = make_client(allowed_names={"Sly Fox", "Clever Badger"})
    response = client.get("/player-names/reroll", params={"exclude": "Sly Fox"}, headers=_auth("auth-tedy"))
    assert response.status_code == 200
    assert response.json() == {"name": "Clever Badger"}


def test_offer_endpoints_exclude_names_already_in_the_target_game():
    client = make_client(allowed_names={"Sly Fox", "Clever Badger"})
    created = client.post("/games", json={"display_name": "Sly Fox"}, headers=_auth("auth-tedy"))
    join_code = created.json()["join_code"]

    # A second player opening the join page: the initial offer must never
    # be the name the host already has, even without an explicit exclude.
    response = client.get("/player-names/initial", params={"join_code": join_code}, headers=_auth("auth-mortia"))
    assert response.status_code == 200
    assert response.json()["name"] == "Clever Badger"


# --- golden rolls at actual seating: through the API ---


def test_create_game_with_forced_golden_rng_seats_a_golden_host():
    client = make_client(allowed_names={"Sly Fox", "Dave"}, golden_names={"Dave"}, rng=_ALWAYS_GOLDEN)
    created = client.post("/games", json={"display_name": "Sly Fox"}, headers=_auth("auth-tedy"))
    assert created.status_code == 200
    game_id = created.json()["game_id"]

    view = client.get(f"/games/{game_id}", headers=_auth("auth-tedy")).json()
    host = view["players"][0]
    assert host["display_name"] == "Dave"
    assert host["is_golden_name"] is True


def test_create_game_with_forced_miss_rng_keeps_the_previewed_name():
    client = make_client(allowed_names={"Sly Fox", "Dave"}, golden_names={"Dave"}, rng=_NEVER_GOLDEN)
    created = client.post("/games", json={"display_name": "Sly Fox"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]

    view = client.get(f"/games/{game_id}", headers=_auth("auth-tedy")).json()
    host = view["players"][0]
    assert host["display_name"] == "Sly Fox"
    assert host["is_golden_name"] is False


def test_join_game_with_forced_golden_rng_seats_a_golden_joiner():
    # Two golden names in the pool: with an always-hit rng, the host's own
    # create_game roll already claims one ("Dave", sorted-first) -- this
    # confirms the joiner's independent roll both fires *and* excludes
    # whatever gold the host already holds, landing "Zara" instead.
    client = make_client(
        allowed_names={"Sly Fox", "Clever Badger", "Dave", "Zara"}, golden_names={"Dave", "Zara"}, rng=_ALWAYS_GOLDEN
    )
    created = client.post("/games", json={"display_name": "Sly Fox"}, headers=_auth("auth-tedy"))
    game_id = created.json()["game_id"]
    join_code = created.json()["join_code"]

    joined = client.post("/games/join", json={"join_code": join_code, "display_name": "Clever Badger"}, headers=_auth("auth-mortia"))
    assert joined.status_code == 200

    view = client.get(f"/games/{game_id}", headers=_auth("auth-tedy")).json()
    joiner = next(p for p in view["players"] if p["game_player_id"] == joined.json()["game_player_id"])
    assert joiner["display_name"] == "Zara"
    assert joiner["is_golden_name"] is True
