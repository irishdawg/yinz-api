"""GameConfig.accepters_required: at 5-6 players, a bare proposal or public
pool needs 2 distinct accepters (not just 1) before it actually executes --
below that (2-4 players), the first accept still executes immediately, same
as always. Private pools are exempt at every player count -- only the base
proposer is ever eligible to accept one. See engine._handle_accept_proposal,
_handle_accept_pool, and _resolve_proposal/_resolve_pool's settle-or-refund
handling of pending_accepters."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.errors import IllegalCommandError
from gotiate.domain.projections import PlayerAudience, project
from tests.conftest import find_swap_pair, later, make_started_game, now


def _propose(game, actor, a, b):
    events = engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": a, "entity_b": b}, actor_game_player_id=actor, expected_version=None, now=now()
    )
    return next(e.payload["proposal_id"] for e in events if e.type.value == "PROPOSAL_CREATED")


def _accept_proposal(game, proposal_id, actor):
    return engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=actor, expected_version=None, now=later()
    )


def _accept_pool(game, pool_id, actor):
    return engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=actor, expected_version=None, now=later()
    )


def test_a_4_player_bare_proposal_still_executes_on_the_first_accept():
    game = make_started_game(4)
    tedy, mortia = [p.game_player_id for p in game.players][:2]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)

    events = _accept_proposal(game, proposal_id, mortia)
    assert any(e.type.value == "SWAP_EXECUTED" for e in events)
    assert game.proposals[proposal_id].status.value == "resolved"


def test_a_5_player_bare_proposal_needs_two_accepters():
    game = make_started_game(5)
    tedy, mortia, hanky = [p.game_player_id for p in game.players][:3]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)

    first_events = _accept_proposal(game, proposal_id, mortia)
    assert not any(e.type.value == "SWAP_EXECUTED" for e in first_events)
    pledged = next(e for e in first_events if e.type.value == "PROPOSAL_ACCEPT_PLEDGED")
    assert pledged.payload == {"proposal_id": proposal_id, "accepted_count": 1, "required_count": 2}
    assert game.proposals[proposal_id].status.value == "open"
    assert set(game.proposals[proposal_id].pending_accepters) == {mortia}

    second_events = _accept_proposal(game, proposal_id, hanky)
    assert any(e.type.value == "SWAP_EXECUTED" for e in second_events)
    resolved = next(e for e in second_events if e.type.value == "PROPOSAL_RESOLVED")
    assert resolved.payload["reason"] == "executed"
    assert game.proposals[proposal_id].status.value == "resolved"


def test_the_same_player_cannot_pledge_twice():
    game = make_started_game(5)
    tedy, mortia = [p.game_player_id for p in game.players][:2]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)
    _accept_proposal(game, proposal_id, mortia)

    with pytest.raises(IllegalCommandError):
        _accept_proposal(game, proposal_id, mortia)


def test_a_pledge_that_becomes_liable_locks_committed_not_spent_until_execution():
    game = make_started_game(5)
    tedy, mortia, hanky = [p.game_player_id for p in game.players][:3]
    # mortia owns the rising entity -- her accept has liability 1.
    a, b = find_swap_pair(game, mortia, owned_should_rise=True)
    proposal_id = _propose(game, tedy, a, b)
    before = game.player_by_id(mortia).influence_available

    _accept_proposal(game, proposal_id, mortia)
    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_available == before - 1
    assert mortia_player.influence_committed == 1
    assert mortia_player.influence_spent == 0

    _accept_proposal(game, proposal_id, hanky)
    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_available == before - 1
    assert mortia_player.influence_committed == 0
    assert mortia_player.influence_spent == 1


def test_a_pledge_is_refunded_if_the_proposal_is_withdrawn_before_reaching_threshold():
    game = make_started_game(5)
    tedy, mortia = [p.game_player_id for p in game.players][:2]
    a, b = find_swap_pair(game, mortia, owned_should_rise=True)
    proposal_id = _propose(game, tedy, a, b)
    before = game.player_by_id(mortia).influence_available

    _accept_proposal(game, proposal_id, mortia)
    assert game.player_by_id(mortia).influence_available == before - 1

    engine.handle_command(
        game, command_type="WITHDRAW_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_available == before  # refunded, not spent
    assert mortia_player.influence_committed == 0
    assert mortia_player.influence_spent == 0


def test_a_zero_influence_pledge_is_free_and_never_blocks():
    game = make_started_game(5)
    tedy, mortia, hanky = [p.game_player_id for p in game.players][:3]
    a, b = find_swap_pair(game, mortia, owned_should_rise=True)  # liability 1 for mortia
    game.player_by_id(mortia).influence_available = 0
    proposal_id = _propose(game, tedy, a, b)

    events = _accept_proposal(game, proposal_id, mortia)
    assert any(e.type.value == "PROPOSAL_ACCEPT_PLEDGED" for e in events)
    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_available == 0
    assert mortia_player.influence_committed == 0  # nothing locked -- free
    assert game.proposals[proposal_id].pending_accepters == {mortia: 0}

    exec_events = _accept_proposal(game, proposal_id, hanky)
    assert any(e.type.value == "SWAP_EXECUTED" for e in exec_events)
    assert game.player_by_id(mortia).influence_spent == 0  # still never charged


def test_a_public_pool_at_5_players_needs_two_accepters():
    game = make_started_game(5)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players][:4]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)
    c, d = find_swap_pair(game, mortia, owned_should_rise=False, exclude=frozenset({a, b}))
    pool_events = engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": c, "entity_d": d, "visibility": "public"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    pool_id = next(e.payload["pool_id"] for e in pool_events if e.type.value == "PUBLIC_POOL_CREATED")

    first_events = _accept_pool(game, pool_id, hanky)
    assert not any(e.type.value == "SWAP_EXECUTED" for e in first_events)
    assert set(game.pools[pool_id].pending_accepters) == {hanky}

    second_events = _accept_pool(game, pool_id, josiah)
    assert any(e.type.value == "SWAP_EXECUTED" for e in second_events)
    assert game.pools[pool_id].status.value == "resolved"
    assert game.pools[pool_id].resolution_reason.value == "executed"


def test_a_private_pool_at_6_players_still_executes_on_the_first_accept():
    # Only the base proposer is ever eligible -- structurally exempt from
    # accepters_required regardless of player count.
    game = make_started_game(6)
    tedy, mortia = [p.game_player_id for p in game.players][:2]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)
    c, d = find_swap_pair(game, mortia, owned_should_rise=False, exclude=frozenset({a, b}))
    pool_events = engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": c, "entity_d": d, "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    pool_id = next(e.payload["pool_id"] for e in pool_events if e.type.value == "PRIVATE_POOL_CREATED")

    events = _accept_pool(game, pool_id, tedy)  # only the base proposer may accept a private pool
    assert any(e.type.value == "SWAP_EXECUTED" for e in events)
    assert game.pools[pool_id].status.value == "resolved"


def test_projection_exposes_pledge_progress():
    game = make_started_game(5)
    tedy, mortia = [p.game_player_id for p in game.players][:2]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    proposal_id = _propose(game, tedy, a, b)
    _accept_proposal(game, proposal_id, mortia)

    view = project(game, PlayerAudience(mortia))
    proposal_view = next(p for p in view["proposals"] if p["proposal_id"] == proposal_id)
    assert proposal_view["pending_accepter_ids"] == [mortia]
    assert proposal_view["accepters_required"] == 2
