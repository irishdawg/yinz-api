"""MAKE_POOL_PUBLIC -- private -> public, one-way, initiator-only. No
coverage existed for this command at all before this file (found while
investigating a "the Make Public button doesn't work" playtest report --
verified directly here that the command itself is correct; the report
turned out to be a frontend issue, see page.tsx's stopPropagation fix)."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.errors import IllegalCommandError
from gotiate.domain.events import EventType
from gotiate.domain.projections import PlayerAudience, project
from tests.conftest import make_started_game, now


def _propose_and_pool(game, proposer, pooler, *, visibility="private"):
    entities = list(game.market)
    engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": entities[0], "entity_b": entities[1]}, actor_game_player_id=proposer, expected_version=None, now=now()
    )
    proposal_id = next(iter(game.proposals))
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entities[2], "entity_d": entities[3], "visibility": visibility},
        actor_game_player_id=pooler,
        expected_version=None,
        now=now(),
    )
    pool_id = next(iter(game.pools))
    return proposal_id, pool_id, entities


def test_make_pool_public_flips_visibility_for_the_initiator():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    _, pool_id, _ = _propose_and_pool(game, tedy, mortia)
    assert game.pools[pool_id].visibility.value == "private"

    engine.handle_command(game, command_type="MAKE_POOL_PUBLIC", payload={"pool_id": pool_id}, actor_game_player_id=mortia, expected_version=None, now=now())

    assert game.pools[pool_id].visibility.value == "public"


def test_make_pool_public_emits_pool_made_public_event():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    _, pool_id, _ = _propose_and_pool(game, tedy, mortia)

    events = engine.handle_command(game, command_type="MAKE_POOL_PUBLIC", payload={"pool_id": pool_id}, actor_game_player_id=mortia, expected_version=None, now=now())

    assert len(events) == 1
    assert events[0].type is EventType.POOL_MADE_PUBLIC
    assert events[0].actor_game_player_id == mortia
    assert events[0].payload == {"pool_id": pool_id}


def test_make_pool_public_reveals_contents_live_to_an_outsider():
    # Distinct from execution-time reveal (test_pool_reveal_on_execution.py)
    # -- this pool never executes, but visibility alone is enough to make
    # its contents publicly visible immediately, per _project_pool's
    # can_see_contents check.
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    _, pool_id, entities = _propose_and_pool(game, tedy, mortia)

    outsider_before = next(p for p in project(game, PlayerAudience(hanky))["pools"] if p["pool_id"] == pool_id)
    assert "entity_c" not in outsider_before

    engine.handle_command(game, command_type="MAKE_POOL_PUBLIC", payload={"pool_id": pool_id}, actor_game_player_id=mortia, expected_version=None, now=now())

    outsider_after = next(p for p in project(game, PlayerAudience(hanky))["pools"] if p["pool_id"] == pool_id)
    assert outsider_after["entity_c"] == entities[2]
    assert outsider_after["entity_d"] == entities[3]


def test_make_pool_public_rejects_non_initiator():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    _, pool_id, _ = _propose_and_pool(game, tedy, mortia)

    with pytest.raises(IllegalCommandError):
        engine.handle_command(game, command_type="MAKE_POOL_PUBLIC", payload={"pool_id": pool_id}, actor_game_player_id=hanky, expected_version=None, now=now())
    with pytest.raises(IllegalCommandError):
        # Not even the base proposer -- only the pool's own initiator.
        engine.handle_command(game, command_type="MAKE_POOL_PUBLIC", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now())


def test_make_pool_public_rejects_an_already_public_pool():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    _, pool_id, _ = _propose_and_pool(game, tedy, mortia, visibility="public")

    with pytest.raises(IllegalCommandError):
        engine.handle_command(game, command_type="MAKE_POOL_PUBLIC", payload={"pool_id": pool_id}, actor_game_player_id=mortia, expected_version=None, now=now())
