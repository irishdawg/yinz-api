"""A reserve's identity is secret from its own owner until the mechanic
actually reveals it (Pick Up) -- enforced in project(), not left to the
frontend to avoid rendering. See projections._UNREVEALED_TO_OWNER_ZONES."""

from __future__ import annotations

from gotiate.domain import engine
from gotiate.domain.projections import PlayerAudience, project
from tests.conftest import make_started_game, now


def _reserves_of(game, player_id):
    return [h for h in game.holdings.values() if h.owner_player_id == player_id and h.zone.value == "reserve_unrevealed"]


def _holding_view(view, holding_id):
    return next(h for h in view["holdings"] if h["holding_id"] == holding_id)


def test_unrevealed_reserve_entity_id_is_hidden_from_its_own_owner():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve = _reserves_of(game, tedy)[0]

    view = project(game, PlayerAudience(tedy))
    hv = _holding_view(view, reserve.holding_id)

    assert hv["entity_id"] is None
    assert hv["display_name"] is None
    assert hv["ticker_symbol"] is None
    assert hv["zone"] == "reserve_unrevealed"


def test_portfolio_holdings_are_always_shown():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    portfolio_holding = next(h for h in game.holdings.values() if h.owner_player_id == tedy and h.zone.value == "portfolio")

    view = project(game, PlayerAudience(tedy))
    hv = _holding_view(view, portfolio_holding.holding_id)

    assert hv["entity_id"] == portfolio_holding.entity_id
    assert hv["display_name"] is not None


def test_pick_up_reveals_that_one_reserve_but_not_the_other():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve_a, reserve_b = _reserves_of(game, tedy)[:2]

    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve_a.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    # Player is locked -- project() returns the frozen cached_view, which
    # was itself computed via the same reveal logic.
    view = project(game, PlayerAudience(tedy))
    revealed = _holding_view(view, reserve_a.holding_id)
    still_hidden = _holding_view(view, reserve_b.holding_id)

    assert revealed["entity_id"] == reserve_a.entity_id
    assert still_hidden["entity_id"] is None


def test_discarded_and_pickup_surrendered_stay_revealed_once_seen():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve = _reserves_of(game, tedy)[0]

    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    pp = game.player_by_id(tedy).pending_pickup
    discard_id = pp.original_portfolio_holding_ids[0]
    engine.handle_command(
        game,
        command_type="DISCARD_HOLDING",
        payload={"pending_pickup_id": pp.pending_pickup_id, "holding_id_to_discard": discard_id},
        actor_game_player_id=tedy,
        expected_version=None,
        now=now(),
    )

    view = project(game, PlayerAudience(tedy))
    discarded_view = _holding_view(view, discard_id)
    swapped_in_view = _holding_view(view, reserve.holding_id)

    assert discarded_view["entity_id"] is not None  # already known, discarding doesn't re-hide it
    assert swapped_in_view["entity_id"] == reserve.entity_id
    assert swapped_in_view["zone"] == "portfolio"


def test_everything_revealed_once_scored_regardless_of_zone():
    game = make_started_game(2)
    p0, p1 = [p.game_player_id for p in game.players]
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p1, expected_version=None, now=now())

    view = project(game, PlayerAudience(p0))
    for h in view["holdings"]:
        assert h["entity_id"] is not None
