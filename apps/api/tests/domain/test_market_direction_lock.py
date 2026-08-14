"""Market-direction-reversal locking: a bare proposal or Pool leg's
"rising" entity is locked once, at authoring time, via
SwapIntent.rising_entity_id -- never recomputed. Mere magnitude movement
that preserves the same up/down relationship leaves a negotiation valid;
a crossing (the live-derived rising entity no longer matches the locked
one) voids it, LOUD, through engine._invalidate_crossed_negotiations --
the single choke point folded into _execute_swap so every trigger
(accepted bare proposal, executed Pool, Stage 5's unilateral
burn-for-swap) shares one code path. See the design writeup."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import PoolResolutionReason, ProposalResolutionReason, ResolutionStatus
from gotiate.domain.projections import PlayerAudience, PublicAudience, project
from tests.conftest import make_started_game, now


def _force_order(game, *entity_ids: str) -> None:
    """Reassigns entity_ids' own current position VALUES among
    themselves so entity_ids[0] ends up best (lowest position),
    entity_ids[-1] worst (highest) -- preserves the overall market's
    permutation validity since only these entities' existing values are
    being redistributed, not invented."""
    values = sorted(game.market[e].position for e in entity_ids)
    for entity_id, value in zip(entity_ids, values):
        game.market[entity_id].position = value


def _propose(game, actor, entity_a, entity_b):
    events = engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": entity_a, "entity_b": entity_b}, actor_game_player_id=actor, expected_version=None, now=now()
    )
    return next(e.payload["proposal_id"] for e in events if e.type.value == "PROPOSAL_CREATED")


def _pool(game, actor, proposal_id, entity_c, entity_d, visibility="public"):
    events = engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entity_c, "entity_d": entity_d, "visibility": visibility},
        actor_game_player_id=actor,
        expected_version=None,
        now=now(),
    )
    return next(e.payload["pool_id"] for e in events if e.type.value in ("PRIVATE_POOL_CREATED", "PUBLIC_POOL_CREATED"))


def _accept_proposal(game, proposal_id, actor):
    return engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=actor, expected_version=None, now=now()
    )


def _accept_pool(game, pool_id, actor):
    return engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=actor, expected_version=None, now=now()
    )


def _reserve_of(game, player_id):
    return next(h for h in game.holdings.values() if h.owner_player_id == player_id and h.zone.value == "reserve_unrevealed")


# --------------------------------------------------------------------------
# Locking + non-crossing movement leaves a negotiation valid
# --------------------------------------------------------------------------


def test_rising_entity_id_is_locked_at_proposal_creation():
    game = make_started_game(3)
    tedy, mortia, _ = [p.game_player_id for p in game.players]
    a, b, c = list(game.market.keys())[:3]
    _force_order(game, a, b, c)  # a best, b middle, c worst -- b rises against a

    proposal_id = _propose(game, tedy, a, b)
    assert game.proposals[proposal_id].swap.rising_entity_id == b

    # Magnitude-only move: mortia proposes b<->c and a third player
    # accepts it. b moves from "middle" to "worst" -- still worse than a,
    # so the a<->b relationship is UNCHANGED, not crossed.
    bc_proposal_id = _propose(game, mortia, b, c)
    # a third player accepts -- game has 3 players, use whichever isn't tedy/mortia.
    third = next(p.game_player_id for p in game.players if p.game_player_id not in (tedy, mortia))
    _accept_proposal(game, bc_proposal_id, third)

    proposal = game.proposals[proposal_id]
    assert proposal.status == ResolutionStatus.OPEN
    assert proposal.resolution_reason is None


# --------------------------------------------------------------------------
# Crossing voids -- bare proposal, cascading to attached Pools
# --------------------------------------------------------------------------


def test_crossing_voids_the_proposal_and_cascades_to_attached_pools():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    a, b, c = list(game.market.keys())[:3]
    # c best, a middle, b worst -- b rises against a (locked below).
    _force_order(game, c, a, b)

    proposal_id = _propose(game, tedy, a, b)
    assert game.proposals[proposal_id].swap.rising_entity_id == b

    d, e = [eid for eid in game.market if eid not in (a, b, c)][:2]
    pool_id = _pool(game, hanky, proposal_id, d, e)
    initiator_committed_before = game.player_by_id(hanky).influence_committed

    # An unrelated b<->c swap: b takes c's (better-than-a) position, so
    # b now sits BETTER than a -- the a<->b relationship has crossed.
    bc_proposal_id = _propose(game, mortia, b, c)
    events = _accept_proposal(game, bc_proposal_id, hanky)

    proposal = game.proposals[proposal_id]
    assert proposal.status == ResolutionStatus.RESOLVED
    assert proposal.resolution_reason == ProposalResolutionReason.VOIDED_MARKET_SWUNG

    pool = game.pools[pool_id]
    assert pool.status == ResolutionStatus.RESOLVED
    assert pool.resolution_reason == PoolResolutionReason.BASE_PROPOSAL_VOIDED

    # No Influence spent by the voided cascade -- if the pool had a
    # committed liability, it refunds to available, never to spent.
    hanky_player = game.player_by_id(hanky)
    if initiator_committed_before == 1:
        assert hanky_player.influence_committed == 0

    reasons = [ev.payload.get("reason") for ev in events if ev.type.value in ("PROPOSAL_RESOLVED", "POOL_RESOLVED")]
    assert "voided_market_swung" in reasons
    assert "base_proposal_voided" in reasons


def test_pool_only_leg_crossing_voids_just_the_pool():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    a, b = list(game.market.keys())[:2]
    _force_order(game, a, b)  # a best, b worse -- b rises against a, never crossed in this test

    proposal_id = _propose(game, tedy, a, b)

    c, d, e = [eid for eid in game.market if eid not in (a, b)][:3]
    _force_order(game, e, c, d)  # e best, c middle, d worst -- d rises against c
    pool_id = _pool(game, hanky, proposal_id, c, d)
    assert game.pools[pool_id].swap.rising_entity_id == d

    de_proposal_id = _propose(game, mortia, d, e)
    _accept_proposal(game, de_proposal_id, hanky)

    pool = game.pools[pool_id]
    assert pool.status == ResolutionStatus.RESOLVED
    assert pool.resolution_reason == PoolResolutionReason.VOIDED_MARKET_SWUNG

    # The base proposal's own direction never crossed -- untouched, still open.
    proposal = game.proposals[proposal_id]
    assert proposal.status == ResolutionStatus.OPEN
    assert proposal.resolution_reason is None


# --------------------------------------------------------------------------
# Same rule, triggered via Stage 5's unilateral path -- one shared choke point
# --------------------------------------------------------------------------


def test_burn_reserve_for_swap_also_triggers_invalidation():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b, c = list(game.market.keys())[:3]
    _force_order(game, c, a, b)  # c best, a middle, b worst -- b rises against a

    proposal_id = _propose(game, tedy, a, b)
    assert game.proposals[proposal_id].swap.rising_entity_id == b

    reserve = _reserve_of(game, mortia)
    engine.handle_command(
        game,
        command_type="BURN_RESERVE_FOR_SWAP",
        payload={"reserve_holding_id": reserve.holding_id, "entity_a": b, "entity_b": c},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )

    proposal = game.proposals[proposal_id]
    assert proposal.status == ResolutionStatus.RESOLVED
    assert proposal.resolution_reason == ProposalResolutionReason.VOIDED_MARKET_SWUNG


# --------------------------------------------------------------------------
# ACCEPT_POOL's own two sequential swaps never cross-invalidate each other
# --------------------------------------------------------------------------


def test_accept_pool_does_not_void_its_own_base_or_pool_leg():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    a, b, c, d = list(game.market.keys())[:4]
    _force_order(game, a, b)
    _force_order(game, c, d)

    proposal_id = _propose(game, tedy, a, b)
    pool_id = _pool(game, mortia, proposal_id, c, d)
    _accept_pool(game, pool_id, hanky)

    proposal = game.proposals[proposal_id]
    pool = game.pools[pool_id]
    assert proposal.resolution_reason == ProposalResolutionReason.EXECUTED
    assert pool.resolution_reason == PoolResolutionReason.EXECUTED


# --------------------------------------------------------------------------
# Public, loud -- the opposite of Pass's masking
# --------------------------------------------------------------------------


def test_voided_market_swung_is_never_masked_for_any_live_audience():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    a, b, c = list(game.market.keys())[:3]
    _force_order(game, c, a, b)

    proposal_id = _propose(game, tedy, a, b)
    bc_proposal_id = _propose(game, mortia, b, c)
    _accept_proposal(game, bc_proposal_id, hanky)

    assert game.proposals[proposal_id].resolution_reason == ProposalResolutionReason.VOIDED_MARKET_SWUNG

    public_view = project(game, PublicAudience())
    tedy_view = project(game, PlayerAudience(tedy))
    voided_public = next(p for p in public_view["proposals"] if p["proposal_id"] == proposal_id)
    voided_self = next(p for p in tedy_view["proposals"] if p["proposal_id"] == proposal_id)
    assert voided_public["resolution_reason"] == "voided_market_swung"
    assert voided_self["resolution_reason"] == "voided_market_swung"


def test_rising_entity_id_exposed_in_projection_and_pinned():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = list(game.market.keys())[:2]
    _force_order(game, a, b)

    proposal_id = _propose(game, tedy, a, b)
    view = project(game, PublicAudience())
    proposal_view = next(p for p in view["proposals"] if p["proposal_id"] == proposal_id)
    assert proposal_view["rising_entity_id"] == b
