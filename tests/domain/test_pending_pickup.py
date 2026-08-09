from __future__ import annotations

from datetime import timedelta

import pytest

from gotiate.domain import engine
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import make_started_game, now


def _reserve_of(game, player_id):
    return next(h for h in game.holdings.values() if h.owner_player_id == player_id and h.zone.value == "reserve_unrevealed")


def test_pick_up_then_discard_in_time():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve = _reserve_of(game, tedy)

    t0 = now()
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=t0
    )
    pp = game.player_by_id(tedy).pending_pickup
    assert pp is not None
    original = pp.original_portfolio_holding_ids
    discard_id = original[0]

    engine.handle_command(
        game,
        command_type="DISCARD_HOLDING",
        payload={"pending_pickup_id": pp.pending_pickup_id, "holding_id_to_discard": discard_id},
        actor_game_player_id=tedy,
        expected_version=None,
        now=t0 + timedelta(seconds=2),
    )

    assert game.player_by_id(tedy).pending_pickup is None
    assert game.holdings[discard_id].zone.value == "discarded"
    assert game.holdings[reserve.holding_id].zone.value == "portfolio"


def test_pickup_times_out_and_surrenders_reserve_original_five_restored():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    hanky = game.players[1].game_player_id
    reserve = _reserve_of(game, tedy)

    t0 = now()
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=t0
    )
    original = game.player_by_id(tedy).pending_pickup.original_portfolio_holding_ids

    # Nobody discards. A later, unrelated command from someone else arrives
    # after the 5.0s deadline plus the 500ms transport grace.
    late = t0 + timedelta(seconds=6)
    engine.handle_command(
        game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=hanky, expected_version=None, now=late
    )

    assert game.player_by_id(tedy).pending_pickup is None
    assert game.holdings[reserve.holding_id].zone.value == "pickup_surrendered"
    assert all(game.holdings[hid].zone.value == "portfolio" for hid in original)


def test_stale_pending_pickup_id_is_rejected():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve = _reserve_of(game, tedy)
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game,
            command_type="DISCARD_HOLDING",
            payload={"pending_pickup_id": "not-the-real-one", "holding_id_to_discard": "whatever"},
            actor_game_player_id=tedy,
            expected_version=None,
            now=now(),
        )


def test_locked_player_cannot_do_anything_else():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve = _reserve_of(game, tedy)
    entities = list(game.market.keys())
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game,
            command_type="PROPOSE_SWAP",
            payload={"entity_a": entities[0], "entity_b": entities[1]},
            actor_game_player_id=tedy,
            expected_version=None,
            now=now(),
        )
