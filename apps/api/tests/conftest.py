from __future__ import annotations

from datetime import datetime, timezone

from gotiate.domain import engine
from gotiate.domain.entities import Game


def now() -> datetime:
    return datetime.now(timezone.utc)


def make_started_game(player_count: int = 4) -> Game:
    game, _ = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now())
    for i in range(1, player_count):
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"Player {i}", now=now())
    engine.handle_command(
        game,
        command_type="START_GAME",
        payload={},
        actor_game_player_id=game.host_player_id,
        expected_version=None,
        now=now(),
    )
    return game
