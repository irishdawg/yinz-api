"""expected_player_count is a soft hint, never a cap — see decision context
in the conversation, not a locked domain-model section yet. It never blocks
JOIN_GAME or START_GAME; it exists so the UI can show progress and so the
host can revise it (SET_EXPECTED_PLAYER_COUNT) if someone declines."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import GamePhase
from gotiate.domain.errors import IllegalCommandError
from gotiate.domain.projections import PlayerAudience, project
from tests.conftest import now


def test_expected_player_count_defaults_to_none():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now())
    assert game.expected_player_count is None


def test_expected_player_count_set_at_creation():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=4)
    assert game.expected_player_count == 4
    view = project(game, PlayerAudience(game.host_player_id))
    assert view["expected_player_count"] == 4


def test_expected_player_count_out_of_range_rejected_at_creation():
    with pytest.raises(IllegalCommandError):
        engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=1)
    with pytest.raises(IllegalCommandError):
        engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=7)


def test_it_never_blocks_joining_or_starting_below_or_above_the_declared_count():
    # Declared 4, only 3 show up (one declined) — nothing stops the host.
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=4)
    for i in range(1, 3):
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"P{i}", now=now())
    assert len(game.players) == 3

    events = engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )
    assert game.phase == GamePhase.NEGOTIATION
    assert events  # actually did something, wasn't silently rejected


def test_host_can_revise_expected_player_count_down_after_a_decline():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=4)
    engine.join_game(game, actor_auth_user_id="auth-1", display_name="Mortia", now=now())
    engine.join_game(game, actor_auth_user_id="auth-2", display_name="Hanky", now=now())

    engine.handle_command(
        game,
        command_type="SET_EXPECTED_PLAYER_COUNT",
        payload={"expected_player_count": 3},
        actor_game_player_id=game.host_player_id,
        expected_version=None,
        now=now(),
    )
    assert game.expected_player_count == 3


def test_host_can_clear_expected_player_count_back_to_none():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=4)
    engine.handle_command(
        game,
        command_type="SET_EXPECTED_PLAYER_COUNT",
        payload={"expected_player_count": None},
        actor_game_player_id=game.host_player_id,
        expected_version=None,
        now=now(),
    )
    assert game.expected_player_count is None


def test_only_host_can_revise_expected_player_count():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=4)
    player, _ = engine.join_game(game, actor_auth_user_id="auth-1", display_name="Mortia", now=now())

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game,
            command_type="SET_EXPECTED_PLAYER_COUNT",
            payload={"expected_player_count": 3},
            actor_game_player_id=player.game_player_id,
            expected_version=None,
            now=now(),
        )


def test_cannot_revise_below_already_joined_count():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=4)
    for i in range(1, 4):  # brings total to 4
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"P{i}", now=now())

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game,
            command_type="SET_EXPECTED_PLAYER_COUNT",
            payload={"expected_player_count": 2},
            actor_game_player_id=game.host_player_id,
            expected_version=None,
            now=now(),
        )


def test_cannot_revise_after_game_has_started():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=2)
    engine.join_game(game, actor_auth_user_id="auth-1", display_name="Mortia", now=now())
    engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game,
            command_type="SET_EXPECTED_PLAYER_COUNT",
            payload={"expected_player_count": 3},
            actor_game_player_id=game.host_player_id,
            expected_version=None,
            now=now(),
        )


def test_revising_to_the_same_value_is_a_noop():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now(), expected_player_count=4)
    v_before = game.version
    engine.handle_command(
        game,
        command_type="SET_EXPECTED_PLAYER_COUNT",
        payload={"expected_player_count": 4},
        actor_game_player_id=game.host_player_id,
        expected_version=None,
        now=now(),
    )
    assert game.version == v_before
