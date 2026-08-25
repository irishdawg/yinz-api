"""KICK_PLAYER -- host-only, LOBBY-only moderation escape hatch (e.g. an
offensive typed display name; typed names have no content-moderation
filter by design, see CURRENT_WORK.md). Removes the target's GamePlayer
entirely so the same auth_user_id is immediately free to rejoin under a
different name."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import GamePhase
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import now


def _new_lobby(player_count: int = 4):
    game, _ = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now())
    joiners = []
    for i in range(1, player_count):
        player, _ = engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"Player {i}", now=now())
        joiners.append(player)
    return game, joiners


def _kick(game, actor, target_id, now_=None):
    return engine.handle_command(
        game, command_type="KICK_PLAYER", payload={"game_player_id": target_id}, actor_game_player_id=actor, expected_version=None, now=now_ or now()
    )


def test_host_can_kick_a_non_host_player():
    game, joiners = _new_lobby(4)
    target = joiners[1]  # seat 2
    events = _kick(game, game.host_player_id, target.game_player_id)

    assert target.game_player_id not in [p.game_player_id for p in game.players]
    assert len(game.players) == 3
    assert any(e.type.value == "PLAYER_KICKED" and e.payload["removed_player_id"] == target.game_player_id for e in events)


def test_kick_compacts_seats_so_a_gap_never_survives():
    game, joiners = _new_lobby(4)
    # Seats: host=0, joiners[0]=1, joiners[1]=2, joiners[2]=3. Kick seat 1.
    _kick(game, game.host_player_id, joiners[0].game_player_id)

    seats = sorted(p.seat for p in game.players)
    assert seats == [0, 1, 2]
    # The host's own seat is untouched (it was already 0, below the gap).
    assert game.player_by_id(game.host_player_id).seat == 0

    # A fresh join must land on the now-correct next seat (3), not collide
    # with an existing occupant -- this is exactly what compaction protects.
    new_player, _ = engine.join_game(game, actor_auth_user_id="auth-new", display_name="Newcomer", now=now())
    assert new_player.seat == 3
    assert len({p.seat for p in game.players}) == len(game.players)  # still all-unique


def test_non_host_cannot_kick_anyone():
    game, joiners = _new_lobby(3)
    with pytest.raises(IllegalCommandError):
        _kick(game, joiners[0].game_player_id, joiners[1].game_player_id)


def test_host_cannot_kick_themselves():
    game, _ = _new_lobby(2)
    with pytest.raises(IllegalCommandError):
        _kick(game, game.host_player_id, game.host_player_id)


def test_cannot_kick_a_nonexistent_player():
    game, _ = _new_lobby(2)
    with pytest.raises(IllegalCommandError):
        _kick(game, game.host_player_id, "not-a-real-player-id")


def test_kick_illegal_outside_lobby():
    game, _ = _new_lobby(2)
    engine.handle_command(game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now())
    assert game.phase == GamePhase.NEGOTIATION
    someone = next(p for p in game.players if p.game_player_id != game.host_player_id)
    with pytest.raises(IllegalCommandError):
        _kick(game, game.host_player_id, someone.game_player_id)


def test_kicked_players_auth_user_id_can_immediately_rejoin():
    game, joiners = _new_lobby(3)
    target = joiners[0]
    kicked_auth_id = target.auth_user_id
    _kick(game, game.host_player_id, target.game_player_id)

    assert game.player_by_auth_id(kicked_auth_id) is None
    rejoined, _ = engine.join_game(game, actor_auth_user_id=kicked_auth_id, display_name="A Better Name", now=now())
    assert rejoined.display_name == "A Better Name"
    assert rejoined.game_player_id != target.game_player_id  # a genuinely new seat, not the old one reanimated


def test_player_kicked_event_is_public():
    from gotiate.domain.projections import PublicAudience, project_events

    game, joiners = _new_lobby(3)
    target = joiners[0]
    events = _kick(game, game.host_player_id, target.game_player_id)

    views = project_events(game, events, PublicAudience())
    assert any(v["type"] == "PLAYER_KICKED" and v["payload"]["removed_display_name"] == target.display_name for v in views)
