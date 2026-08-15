from __future__ import annotations

from datetime import timedelta

import pytest

from gotiate.domain import engine
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import make_started_game, now


def _reserve_of(game, player_id):
    return next(h for h in game.holdings.values() if h.owner_player_id == player_id and h.zone.value == "reserve_unrevealed")


def test_pick_up_reserve_caches_a_real_view_not_an_empty_one():
    # Regression: _handle_pick_up_reserve used to attach pending_pickup to
    # the player *before* computing cached_view -- project() short-circuits
    # to player.pending_pickup.cached_view the instant that field is set,
    # so the computation was reading its own still-empty default back into
    # itself, permanently freezing every pickup at `{}`. Caught by
    # test_holding_secrecy.py actually asserting on the cached view's
    # contents for the first time; pinned here explicitly too.
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve = _reserve_of(game, tedy)

    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    cached_view = game.player_by_id(tedy).pending_pickup.cached_view
    assert cached_view != {}
    assert cached_view["game_id"] == game.id
    assert len(cached_view["holdings"]) > 0


def test_cached_view_carries_the_decision_deadline_and_revealed_entity():
    # Stage 5's frozen countdown needs a deadline the client can render --
    # this can't be computed through the normal _project_player path
    # (project() never calls it again for this player until the pickup
    # resolves), so it's injected directly into the cached snapshot at
    # PICK_UP_RESERVE time. See the Stage 5 design writeup.
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve = _reserve_of(game, tedy)

    t0 = now()
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=t0
    )
    pp = game.player_by_id(tedy).pending_pickup
    cached_view = pp.cached_view
    assert cached_view["pending_pickup"] == {
        "pending_pickup_id": pp.pending_pickup_id,
        "reserve_holding_id": reserve.holding_id,
        "revealed_entity_id": reserve.entity_id,
        "decision_deadline_at": pp.decision_deadline_at,
    }
    # Reads verbatim on a simulated reload, same as everything else in the
    # frozen snapshot -- project() keeps returning this exact dict.
    from gotiate.domain.projections import PlayerAudience, project

    reread = project(game, PlayerAudience(tedy))
    assert reread["pending_pickup"] == cached_view["pending_pickup"]


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
    # after the 12.0s deadline plus the 500ms transport grace.
    late = t0 + timedelta(seconds=13)
    engine.handle_command(
        game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=hanky, expected_version=None, now=late
    )

    assert game.player_by_id(tedy).pending_pickup is None
    assert game.holdings[reserve.holding_id].zone.value == "pickup_surrendered"
    assert all(game.holdings[hid].zone.value == "portfolio" for hid in original)


def test_decline_pickup_keeps_original_five_and_surrenders_reserve():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    reserve = _reserve_of(game, tedy)

    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    pp = game.player_by_id(tedy).pending_pickup
    original = pp.original_portfolio_holding_ids

    engine.handle_command(
        game,
        command_type="DECLINE_PICKUP",
        payload={"pending_pickup_id": pp.pending_pickup_id},
        actor_game_player_id=tedy,
        expected_version=None,
        now=now(),
    )

    assert game.player_by_id(tedy).pending_pickup is None
    assert game.holdings[reserve.holding_id].zone.value == "pickup_surrendered"
    assert all(game.holdings[hid].zone.value == "portfolio" for hid in original)


def test_decline_pickup_rejected_without_a_matching_open_pending_pickup():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="DECLINE_PICKUP", payload={"pending_pickup_id": "not-real"}, actor_game_player_id=tedy, expected_version=None, now=now()
        )

    reserve = _reserve_of(game, tedy)
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    pp = game.player_by_id(tedy).pending_pickup
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="DECLINE_PICKUP", payload={"pending_pickup_id": "wrong-id"}, actor_game_player_id=tedy, expected_version=None, now=now()
        )
    assert game.player_by_id(tedy).pending_pickup is not None
    assert game.player_by_id(tedy).pending_pickup.pending_pickup_id == pp.pending_pickup_id


def test_decline_pickup_is_exempt_from_the_stale_version_check():
    # Same exemption DISCARD_HOLDING already gets, for the same reason --
    # a frozen client can't know the live version, since the rest of the
    # game keeps moving during the lock.
    game = make_started_game(3)
    tedy, mortia, _ = [p.game_player_id for p in game.players]
    reserve = _reserve_of(game, tedy)
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    stale_version = game.version
    entities = list(game.market.keys())
    engine.handle_command(
        game,
        command_type="PROPOSE_SWAP",
        payload={"entity_a": entities[0], "entity_b": entities[1]},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    assert game.version != stale_version

    pp = game.player_by_id(tedy).pending_pickup
    engine.handle_command(
        game,
        command_type="DECLINE_PICKUP",
        payload={"pending_pickup_id": pp.pending_pickup_id},
        actor_game_player_id=tedy,
        expected_version=stale_version,  # deliberately stale -- must not raise
        now=now(),
    )
    assert game.player_by_id(tedy).pending_pickup is None


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
