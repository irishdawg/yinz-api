from __future__ import annotations

from gotiate.domain import engine
from gotiate.domain.entities import GamePhase
from tests.conftest import make_started_game, now


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
