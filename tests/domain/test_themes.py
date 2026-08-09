"""Theme sets are swappable config, not hardcoded content — see themes.py
and the domain model's ThemeSet/ThemeEntityDefinition split."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import GameConfig
from gotiate.domain.errors import IllegalCommandError
from gotiate.domain.projections import PlayerAudience, project
from tests.conftest import now


def _start_with_theme(theme_set_id: str, player_count: int):
    game, _ = engine.create_game(
        actor_auth_user_id="auth-0", display_name="Host", now=now(), config=GameConfig(theme_set_id=theme_set_id)
    )
    for i in range(1, player_count):
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"Player {i}", now=now())
    events = engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )
    return game, events


def test_default_theme_set_is_the_free_companies_set():
    game, _ = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now())
    assert game.config.theme_set_id == "fictional_companies_v1"


def test_swapping_theme_set_changes_market_content():
    game, _ = _start_with_theme("dragons_v1", player_count=2)  # dragons_v1 has exactly 9 entities, matching a 2p market
    view = project(game, PlayerAudience(game.host_player_id))
    display_names = {m["display_name"] for m in view["market"]}
    assert display_names <= {
        "Emberclaw", "Frostwing", "Duskfang", "Stormscale", "Ashmaw", "Glimmertail", "Boneshard", "Verdigris", "Nightgale",
    }
    assert "DaveCo" not in display_names


def test_theme_set_too_small_for_player_count_is_rejected_cleanly():
    game, _ = engine.create_game(
        actor_auth_user_id="auth-0", display_name="Host", now=now(), config=GameConfig(theme_set_id="dragons_v1")
    )
    for i in range(1, 4):  # 4 players needs 13 entities; dragons_v1 only has 9
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"P{i}", now=now())

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
        )


def test_unknown_theme_set_is_rejected_cleanly():
    game, _ = engine.create_game(
        actor_auth_user_id="auth-0", display_name="Host", now=now(), config=GameConfig(theme_set_id="does_not_exist_v1")
    )
    engine.join_game(game, actor_auth_user_id="auth-1", display_name="P1", now=now())

    with pytest.raises(Exception):  # NotFoundError from themes.get_theme_set
        engine.handle_command(
            game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
        )


def test_locked_entity_is_always_dealt_into_the_market():
    # fictional_companies_v1 has 18 entities (daveco locked) but a 2-player
    # market only holds 9 — real sampling pressure on the swappable pool,
    # not the trivial case where everything fits regardless.
    game, _ = _start_with_theme("fictional_companies_v1", player_count=2)
    theme_keys = {m["theme_key"] for m in project(game, PlayerAudience(game.host_player_id))["market"]}
    assert "daveco" in theme_keys


def test_too_many_locked_entities_for_market_size_is_rejected_cleanly():
    game, _ = engine.create_game(
        actor_auth_user_id="auth-0",
        display_name="Host",
        now=now(),
        config=GameConfig(theme_set_id="dragons_v1", market_size_by_players={2: 0}),
    )
    engine.join_game(game, actor_auth_user_id="auth-1", display_name="P1", now=now())

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
        )


def test_market_entities_carry_a_ticker_symbol():
    game, _ = _start_with_theme("fictional_companies_v1", player_count=2)
    view = project(game, PlayerAudience(game.host_player_id))
    for entity in view["market"]:
        assert entity["ticker_symbol"]
        assert len(entity["ticker_symbol"]) <= 6
