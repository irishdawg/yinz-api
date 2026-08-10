from __future__ import annotations

from datetime import timedelta

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import GameConfig, GamePhase
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import now


def test_create_game_makes_host_seat_zero():
    game, events = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now())
    assert game.phase == GamePhase.LOBBY
    assert len(game.players) == 1
    assert game.players[0].seat == 0
    assert game.host_player_id == game.players[0].game_player_id
    assert game.join_code
    assert {e.type.value for e in events} == {"GAME_CREATED", "PLAYER_JOINED"}


def test_join_code_is_seven_characters_by_default():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now())
    assert len(game.join_code) == 7 == game.config.join_code_length


def test_join_code_length_is_configurable():
    game, _ = engine.create_game(
        actor_auth_user_id="auth-host", display_name="Tedy", now=now(), config=GameConfig(join_code_length=4)
    )
    assert len(game.join_code) == 4


def test_join_code_expires_after_configured_lifetime():
    t0 = now()
    game, _ = engine.create_game(
        actor_auth_user_id="auth-host", display_name="Tedy", now=t0, config=GameConfig(join_code_lifetime_minutes=30)
    )

    # Still within the window — joining works normally.
    engine.join_game(game, actor_auth_user_id="auth-2", display_name="Mortia", now=t0 + timedelta(minutes=29))

    # Past it — even though the game is still sitting in LOBBY, nobody's
    # started it, and the code was never rate-limited into oblivion.
    with pytest.raises(IllegalCommandError):
        engine.join_game(game, actor_auth_user_id="auth-3", display_name="Hanky", now=t0 + timedelta(minutes=31))


def test_join_game_adds_seats_and_rejects_duplicates():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now())
    player, _ = engine.join_game(game, actor_auth_user_id="auth-2", display_name="Mortia", now=now())
    assert player.seat == 1
    with pytest.raises(IllegalCommandError):
        engine.join_game(game, actor_auth_user_id="auth-2", display_name="Mortia again", now=now())


def test_join_game_rejects_after_start():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now())
    engine.join_game(game, actor_auth_user_id="auth-2", display_name="Mortia", now=now())
    engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )
    with pytest.raises(IllegalCommandError):
        engine.join_game(game, actor_auth_user_id="auth-3", display_name="Late", now=now())


def test_only_host_can_start():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now())
    player, _ = engine.join_game(game, actor_auth_user_id="auth-2", display_name="Mortia", now=now())
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="START_GAME", payload={}, actor_game_player_id=player.game_player_id, expected_version=None, now=now()
        )


def test_start_game_requires_two_to_six_players():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now())
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
        )


def test_start_game_sizes_market_and_deals_by_player_count():
    game, _ = engine.create_game(actor_auth_user_id="auth-host", display_name="Tedy", now=now())
    for i in range(1, 4):
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"P{i}", now=now())
    engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )

    assert game.phase == GamePhase.NEGOTIATION
    assert len(game.market) == game.config.market_size_by_players[4]
    assert game.max_duration_s == game.config.max_clock_seconds_by_players[4]
    for player in game.players:
        portfolio = [h for h in game.holdings.values() if h.owner_player_id == player.game_player_id and h.zone.value == "portfolio"]
        reserves = [h for h in game.holdings.values() if h.owner_player_id == player.game_player_id and h.zone.value == "reserve_unrevealed"]
        assert len(portfolio) == sum(game.config.portfolio_shape)
        assert len(reserves) == game.config.reserve_count
    assert game.waterline_entity_id in game.market
