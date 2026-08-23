"""NEGOTIATION-phase abandonment backstop -- a real-play gap found once the
gameplay clock was removed: a game with nobody left to act on it could
previously sit open forever, and a returning player's own auto-redirect
back into their one active game trapped them there with no way out.
Structurally identical to the pre-existing LOBBY reminder/grace
auto-cancel: once `negotiation_abandonment_seconds` passes with no command
successfully handled for the game, it force-closes via the ordinary
close_market() path with CloseReason.ABANDONED. Deliberately NOT a
revived gameplay clock -- nobody sees a countdown, nobody races it, it
never affects strategy."""

from __future__ import annotations

from datetime import timedelta

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import CloseReason, GameConfig, GamePhase
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import make_started_game, now


def _short_abandonment_game(player_count: int = 3, seconds: float = 5.0):
    return make_started_game(player_count, config=GameConfig(negotiation_abandonment_seconds=seconds))


def test_does_not_abandon_close_before_the_threshold():
    game = _short_abandonment_game(seconds=10.0)
    t = game.last_activity_at + timedelta(seconds=2)
    events = engine.apply_due_time_transitions(game, t)
    assert game.phase == GamePhase.NEGOTIATION
    assert events == []


def test_abandon_closes_once_threshold_passes_with_no_activity():
    game = _short_abandonment_game(seconds=5.0)
    t = game.last_activity_at + timedelta(seconds=10)
    events = engine.apply_due_time_transitions(game, t)
    assert game.phase == GamePhase.SCORED
    assert game.close_reason is CloseReason.ABANDONED
    assert any(e.type.value == "MARKET_CLOSED" and e.payload["reason"] == "ABANDONED" for e in events)
    assert any(e.type.value == "GAME_ENDED" for e in events)


def test_is_time_transition_due_mirrors_apply_due_time_transitions():
    game = _short_abandonment_game(seconds=5.0)
    just_before = game.last_activity_at + timedelta(seconds=4)
    just_after = game.last_activity_at + timedelta(seconds=6)
    assert engine.is_time_transition_due(game, just_before) is False
    assert engine.is_time_transition_due(game, just_after) is True


def test_a_successful_command_resets_the_abandonment_clock():
    game = _short_abandonment_game(seconds=10.0)
    t1 = game.last_activity_at + timedelta(seconds=7)
    engine.handle_command(
        game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=game.host_player_id, expected_version=None, now=t1
    )
    assert game.last_activity_at == t1

    # Total elapsed since game start is now past the 10s threshold, but
    # only ~3s have passed since the last real activity -- must not be
    # treated as abandoned.
    t2 = t1 + timedelta(seconds=5)
    events = engine.apply_due_time_transitions(game, t2)
    assert game.phase == GamePhase.NEGOTIATION
    assert events == []


def test_a_rejected_command_does_not_reset_the_abandonment_clock():
    game = _short_abandonment_game(seconds=5.0)
    started_at = game.last_activity_at
    t1 = started_at + timedelta(seconds=3)
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game,
            command_type="ACCEPT_PROPOSAL",
            payload={"proposal_id": "not-a-real-proposal"},
            actor_game_player_id=game.host_player_id,
            expected_version=None,
            now=t1,
        )
    assert game.last_activity_at == started_at  # unchanged -- only genuine action counts


def test_abandonment_preempts_a_pending_arbitration():
    game = _short_abandonment_game(player_count=3, seconds=5.0)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())

    propose_events = engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": e[0], "entity_b": e[1]}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    proposal_id = next(ev.payload["proposal_id"] for ev in propose_events if ev.type.value == "PROPOSAL_CREATED")
    engine.handle_command(
        game, command_type="PASS_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=mortia, expected_version=None, now=game.last_activity_at
    )
    engine.handle_command(
        game, command_type="CALL_ARBITRATION", payload={}, actor_game_player_id=hanky, expected_version=None, now=game.last_activity_at
    )
    assert game.proposals[proposal_id].pending_arbitration is not None

    t = game.last_activity_at + timedelta(seconds=10)
    events = engine.apply_due_time_transitions(game, t)

    assert game.phase == GamePhase.SCORED
    assert game.close_reason is CloseReason.ABANDONED
    assert game.proposals[proposal_id].pending_arbitration is None
    arb_resolved = next(ev for ev in events if ev.type.value == "ARBITRATION_RESOLVED")
    assert arb_resolved.payload["reason"] == "market_closed"
