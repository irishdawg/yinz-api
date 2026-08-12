from __future__ import annotations

from datetime import timedelta

from gotiate.domain import engine
from gotiate.domain.entities import CancellationReason, GameConfig, GamePhase
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import now


def _new_lobby_game():
    game, _ = engine.create_game(
        actor_auth_user_id="auth-0",
        display_name="Host",
        now=now(),
        config=GameConfig(lobby_reminder_seconds=180, lobby_reminder_grace_seconds=60),
    )
    return game


def test_nothing_happens_before_the_reminder_deadline():
    game = _new_lobby_game()
    soon = game.created_at + timedelta(seconds=30)
    assert engine.is_time_transition_due(game, soon) is False
    assert engine.apply_due_time_transitions(game, soon) == []
    assert game.phase == GamePhase.LOBBY


def test_nothing_happens_between_reminder_and_grace_expiry():
    game = _new_lobby_game()
    mid_grace = game.lobby_reminder_deadline_at + timedelta(seconds=30)
    assert engine.is_time_transition_due(game, mid_grace) is False
    assert engine.apply_due_time_transitions(game, mid_grace) == []
    assert game.phase == GamePhase.LOBBY


def test_auto_cancels_once_reminder_and_grace_both_elapse():
    game = _new_lobby_game()
    past_grace = game.lobby_reminder_deadline_at + timedelta(seconds=61)
    assert engine.is_time_transition_due(game, past_grace) is True

    events = engine.apply_due_time_transitions(game, past_grace)

    assert game.phase == GamePhase.CANCELLED
    assert game.cancellation_reason == CancellationReason.LOBBY_TIMEOUT
    assert events[-1].type.value == "GAME_CANCELLED"
    assert events[-1].actor_game_player_id is None  # system-triggered, not the host


def test_extend_lobby_timer_prevents_auto_cancel():
    game = _new_lobby_game()
    original_deadline = game.lobby_reminder_deadline_at
    just_before_cancel = original_deadline + timedelta(seconds=59)

    engine.handle_command(
        game, command_type="EXTEND_LOBBY_TIMER", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=just_before_cancel
    )
    assert game.lobby_reminder_deadline_at > original_deadline

    # What would have been past the *original* grace window no longer is,
    # since extending pushed the deadline (and therefore the grace window
    # built on top of it) forward.
    assert engine.apply_due_time_transitions(game, original_deadline + timedelta(seconds=61)) == []
    assert game.phase == GamePhase.LOBBY


def test_extend_lobby_timer_is_host_only():
    game = _new_lobby_game()
    try:
        engine.handle_command(
            game, command_type="EXTEND_LOBBY_TIMER", payload={}, actor_game_player_id="not-the-host", expected_version=None, now=now()
        )
        raise AssertionError("expected IllegalCommandError")
    except IllegalCommandError:
        pass
