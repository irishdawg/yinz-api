"""Accept-lock grace period (GameConfig.accept_lock_seconds, default 4s) --
blocks only ACCEPT_PROPOSAL and ACCEPT_POOL-on-a-public-pool, for that long
after the base proposal's own PROPOSAL_CREATED. Nothing else a proposal
supports (withdraw, pass, create-pool, private-pool-accept) is gated by it.
See engine._require_accept_unlocked."""

from __future__ import annotations

from datetime import timedelta

import pytest

from gotiate.domain import engine
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import find_swap_pair, make_started_game, now


def _propose(game, actor, a, b):
    events = engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": a, "entity_b": b}, actor_game_player_id=actor, expected_version=None, now=now()
    )
    return next(e.payload["proposal_id"] for e in events if e.type.value == "PROPOSAL_CREATED")


def test_accepting_a_bare_proposal_immediately_is_rejected():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=mortia, expected_version=None, now=now()
        )


def test_accepting_a_bare_proposal_after_the_grace_period_succeeds():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)

    events = engine.handle_command(
        game,
        command_type="ACCEPT_PROPOSAL",
        payload={"proposal_id": proposal_id},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now() + timedelta(seconds=game.config.accept_lock_seconds + 1),
    )
    assert any(e.type.value == "SWAP_EXECUTED" for e in events)


def test_withdraw_pass_and_create_pool_are_never_locked():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)

    # Pass and Create Pool, both immediately -- must not raise.
    engine.handle_command(
        game, command_type="PASS_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=mortia, expected_version=None, now=now()
    )
    c, d = find_swap_pair(game, hanky, owned_should_rise=False, exclude=frozenset({a, b}))
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": c, "entity_d": d, "visibility": "private"},
        actor_game_player_id=hanky,
        expected_version=None,
        now=now(),
    )
    # Withdraw, immediately -- must not raise.
    engine.handle_command(
        game, command_type="WITHDRAW_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    assert game.proposals[proposal_id].status.value == "resolved"


def test_accepting_a_private_pool_immediately_is_never_locked():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)
    c, d = find_swap_pair(game, mortia, owned_should_rise=False, exclude=frozenset({a, b}))
    events = engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": c, "entity_d": d, "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    pool_id = next(e.payload["pool_id"] for e in events if e.type.value == "PRIVATE_POOL_CREATED")

    # Only the base proposer may accept a private pool -- immediately, no
    # grace period at all, even though the base proposal itself is still
    # inside its own lock window.
    accept_events = engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    assert any(e.type.value == "SWAP_EXECUTED" for e in accept_events)


def test_accepting_a_public_pool_immediately_is_rejected_until_the_base_proposal_unlocks():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)
    c, d = find_swap_pair(game, mortia, owned_should_rise=False, exclude=frozenset({a, b}))
    events = engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": c, "entity_d": d, "visibility": "public"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    pool_id = next(e.payload["pool_id"] for e in events if e.type.value == "PUBLIC_POOL_CREATED")

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=hanky, expected_version=None, now=now()
        )

    accept_events = engine.handle_command(
        game,
        command_type="ACCEPT_POOL",
        payload={"pool_id": pool_id},
        actor_game_player_id=hanky,
        expected_version=None,
        now=now() + timedelta(seconds=game.config.accept_lock_seconds + 1),
    )
    assert any(e.type.value == "SWAP_EXECUTED" for e in accept_events)
