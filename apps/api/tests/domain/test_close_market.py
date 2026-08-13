from __future__ import annotations

from datetime import timedelta

from gotiate.domain import engine
from gotiate.domain.entities import GamePhase
from tests.conftest import find_swap_pair, make_started_game, now


def test_max_clock_closes_and_refunds_the_committed_open_proposal():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    entity_a, entity_b = find_swap_pair(game, tedy, owned_should_rise=True)
    before = game.player_by_id(tedy).influence_available
    engine.handle_command(
        game,
        command_type="PROPOSE_SWAP",
        payload={"entity_a": entity_a, "entity_b": entity_b},
        actor_game_player_id=tedy,
        expected_version=None,
        now=now(),
    )
    assert game.player_by_id(tedy).influence_committed == 1

    # apply_due_time_transitions is what actually detects expiry and calls
    # close_market — this is the same check every command runs ahead of
    # itself (§04/§08), exercised directly rather than via a command that
    # would itself be legitimately rejected once the market's already closed.
    past_clock = game.started_at + timedelta(seconds=game.max_duration_s + 1)
    engine.apply_due_time_transitions(game, past_clock)

    assert game.phase == GamePhase.SCORED
    assert game.close_reason.value == "TIME_EXPIRED"
    # No beneficial negotiated action happened -- the locked liability
    # refunds, it never converts to spent, per the private Influence
    # economy design (a market-closed proposal is a non-execution, same
    # as a withdrawal).
    tedy_player = game.player_by_id(tedy)
    assert tedy_player.influence_spent == 0
    assert tedy_player.influence_committed == 0
    assert tedy_player.influence_available == before


def test_ready_threshold_closes_immediately_in_same_transaction():
    game = make_started_game(2)  # threshold for 2 players is 2
    p0, p1 = [p.game_player_id for p in game.players]

    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    assert game.phase != GamePhase.SCORED

    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p1, expected_version=None, now=now())
    assert game.phase == GamePhase.SCORED
    assert game.close_reason.value == "READY_THRESHOLD"


def test_ready_to_close_is_bidirectional_until_threshold():
    game = make_started_game(4)  # threshold for 4 players is 3
    p0 = game.players[0].game_player_id

    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    assert game.player_by_id(p0).ready_to_close is True

    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": False}, actor_game_player_id=p0, expected_version=None, now=now())
    assert game.player_by_id(p0).ready_to_close is False
    assert game.phase != GamePhase.SCORED


def test_ready_to_close_noop_does_not_bump_version():
    game = make_started_game(2)
    p0 = game.players[0].game_player_id
    v_before = game.version
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": False}, actor_game_player_id=p0, expected_version=None, now=now())
    assert game.version == v_before


def test_close_market_surrenders_untouched_reserves():
    game = make_started_game(2)
    p0, p1 = [p.game_player_id for p in game.players]
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p1, expected_version=None, now=now())

    reserves = [h for h in game.holdings.values() if h.zone.value == "reserve_unrevealed"]
    assert reserves == []
    surrendered = [h for h in game.holdings.values() if h.zone.value == "surrendered_unused"]
    assert len(surrendered) == 2 * game.config.reserve_count
