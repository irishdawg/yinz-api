"""PASS_POOL -- public, permanent, and independently scoped per Pool."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import PoolResolutionReason, ResolutionStatus
from gotiate.domain.errors import IllegalCommandError
from gotiate.domain.events import EventType
from gotiate.domain.projections import PlayerAudience, project
from tests.conftest import make_started_game, now


def _propose_and_pool(game, proposer: str, pooler: str, *, visibility: str) -> tuple[str, str]:
    entities = list(game.market)
    engine.handle_command(
        game,
        command_type="PROPOSE_SWAP",
        payload={"entity_a": entities[0], "entity_b": entities[1]},
        actor_game_player_id=proposer,
        expected_version=None,
        now=now(),
    )
    proposal_id = game.active_proposal_id
    assert proposal_id is not None
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entities[2], "entity_d": entities[3], "visibility": visibility},
        actor_game_player_id=pooler,
        expected_version=None,
        now=now(),
    )
    return proposal_id, next(iter(game.pools))


def _pass_pool(game, pool_id: str, actor: str):
    return engine.handle_command(
        game,
        command_type="PASS_POOL",
        payload={"pool_id": pool_id},
        actor_game_player_id=actor,
        expected_version=None,
        now=now(),
    )


def test_base_proposer_passes_private_pool_and_it_expires_immediately():
    game = make_started_game(3)
    proposer, pooler, _ = [p.game_player_id for p in game.players]
    proposal_id, pool_id = _propose_and_pool(game, proposer, pooler, visibility="private")

    events = _pass_pool(game, pool_id, proposer)

    pool = game.pools[pool_id]
    assert pool.passed_player_ids == {proposer}
    assert pool.status is ResolutionStatus.RESOLVED
    assert pool.resolution_reason is PoolResolutionReason.EXPIRED_ALL_PASSED
    assert [event.type for event in events] == [EventType.POOL_PASSED, EventType.POOL_RESOLVED]
    assert game.proposals[proposal_id].status is ResolutionStatus.OPEN


def test_public_pool_expires_only_after_every_eligible_accepter_passes():
    game = make_started_game(4)
    proposer, pooler, third, fourth = [p.game_player_id for p in game.players]
    _, pool_id = _propose_and_pool(game, proposer, pooler, visibility="public")

    assert [event.type for event in _pass_pool(game, pool_id, third)] == [EventType.POOL_PASSED]
    assert [event.type for event in _pass_pool(game, pool_id, proposer)] == [EventType.POOL_PASSED]
    assert game.pools[pool_id].status is ResolutionStatus.OPEN

    events = _pass_pool(game, pool_id, fourth)
    assert [event.type for event in events] == [EventType.POOL_PASSED, EventType.POOL_RESOLVED]
    assert game.pools[pool_id].resolution_reason is PoolResolutionReason.EXPIRED_ALL_PASSED


def test_pool_pass_is_public_and_only_blocks_that_pool():
    game = make_started_game(4)
    proposer, pooler, passer, observer = [p.game_player_id for p in game.players]
    proposal_id, pool_id = _propose_and_pool(game, proposer, pooler, visibility="public")

    _pass_pool(game, pool_id, passer)

    for audience_id in (proposer, pooler, passer, observer):
        pool_view = next(p for p in project(game, PlayerAudience(audience_id))["pools"] if p["pool_id"] == pool_id)
        assert pool_view["passed_player_ids"] == [passer]

    with pytest.raises(IllegalCommandError, match="passed this Pool"):
        engine.handle_command(
            game,
            command_type="ACCEPT_POOL",
            payload={"pool_id": pool_id},
            actor_game_player_id=passer,
            expected_version=None,
            now=now(),
        )

    # Passing this Pool does not leave the base negotiation.
    engine.handle_command(
        game,
        command_type="ACCEPT_PROPOSAL",
        payload={"proposal_id": proposal_id},
        actor_game_player_id=passer,
        expected_version=None,
        now=now(),
    )


def test_pool_initiator_and_private_outsider_cannot_pass():
    game = make_started_game(3)
    proposer, pooler, outsider = [p.game_player_id for p in game.players]
    _, pool_id = _propose_and_pool(game, proposer, pooler, visibility="private")

    with pytest.raises(IllegalCommandError, match="own Pool"):
        _pass_pool(game, pool_id, pooler)
    with pytest.raises(IllegalCommandError, match="base proposer"):
        _pass_pool(game, pool_id, outsider)


def test_passing_base_can_remove_player_from_public_pool_accepter_set():
    game = make_started_game(4)
    proposer, pooler, third, fourth = [p.game_player_id for p in game.players]
    proposal_id, pool_id = _propose_and_pool(game, proposer, pooler, visibility="public")
    _pass_pool(game, pool_id, proposer)
    _pass_pool(game, pool_id, third)

    # Fourth has not passed this Pool, but leaving the base negotiation
    # makes them ineligible to accept it. No eligible accepter remains.
    events = engine.handle_command(
        game,
        command_type="PASS_PROPOSAL",
        payload={"proposal_id": proposal_id},
        actor_game_player_id=fourth,
        expected_version=None,
        now=now(),
    )
    assert EventType.PROPOSAL_PASSED in [event.type for event in events]
    assert EventType.POOL_RESOLVED in [event.type for event in events]
    assert game.pools[pool_id].resolution_reason is PoolResolutionReason.EXPIRED_ALL_PASSED


def test_passes_on_one_public_pool_do_not_affect_a_sibling_pool():
    game = make_started_game(4)
    proposer, first_pooler, second_pooler, fourth = [p.game_player_id for p in game.players]
    proposal_id, first_pool_id = _propose_and_pool(game, proposer, first_pooler, visibility="public")
    entities = list(game.market)
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entities[4], "entity_d": entities[5], "visibility": "public"},
        actor_game_player_id=second_pooler,
        expected_version=None,
        now=now(),
    )
    second_pool_id = next(pool_id for pool_id in game.pools if pool_id != first_pool_id)

    for actor in (proposer, second_pooler, fourth):
        _pass_pool(game, first_pool_id, actor)

    assert game.pools[first_pool_id].status is ResolutionStatus.RESOLVED
    assert game.pools[second_pool_id].status is ResolutionStatus.OPEN
    assert game.pools[second_pool_id].passed_player_ids == set()


def test_pool_pass_is_locked_once_arbitration_starts():
    game = make_started_game(4)
    proposer, pooler, third, fourth = [p.game_player_id for p in game.players]
    proposal_id, pool_id = _propose_and_pool(game, proposer, pooler, visibility="public")
    for actor in (third, fourth):
        engine.handle_command(
            game,
            command_type="PASS_PROPOSAL",
            payload={"proposal_id": proposal_id},
            actor_game_player_id=actor,
            expected_version=None,
            now=now(),
        )
    engine.handle_command(
        game,
        command_type="CALL_ARBITRATION",
        payload={},
        actor_game_player_id=proposer,
        expected_version=None,
        now=now(),
    )

    with pytest.raises(IllegalCommandError, match="arbitration"):
        _pass_pool(game, pool_id, proposer)
