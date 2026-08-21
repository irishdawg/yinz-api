"""Market-direction-reversal locking: a bare proposal or Pool leg's
"rising" entity is locked once, at authoring time, via
SwapIntent.rising_entity_id -- never recomputed. A crossing (the
live-derived rising entity no longer matches the locked one) voids it,
LOUD, through engine._invalidate_crossed_negotiations -- the single choke
point folded into _execute_swap so every trigger shares one code path.

NOTE (cadence/economy redesign, checkpoint 2): the single-active-negotiation
constraint plus Pool-entities-disjoint-from-base-entities (enforced at
CREATE_POOL) together make PROPOSAL-vs-PROPOSAL and POOL-vs-BASE crossing
structurally unreachable from the live command surface now -- there is
never a second open proposal to collide with, and nothing can move the
base proposal's own two entities except its own execution (excluded from
the scan by construction). What remains reachable is POOL-vs-sibling-POOL
crossing: two different pools attached to the same base proposal, where
accepting one moves entities the OTHER (still-open) pool's own lock
depends on. That's what's tested below. Proposal-vs-anything crossing
becomes reachable again once checkpoint 4's Force Swap Boost lands (a
genuinely unilateral move that doesn't conclude the open negotiation) --
this file should gain a Force-Swap-crosses-the-active-proposal test then,
mirroring the old burn-reserve test removed in checkpoint 1."""

from __future__ import annotations

from gotiate.domain import engine
from gotiate.domain.entities import PoolResolutionReason, ProposalResolutionReason, ResolutionStatus
from gotiate.domain.projections import PlayerAudience, PublicAudience, project
from tests.conftest import later, make_started_game, now


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
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=actor, expected_version=None, now=later()
    )


def _accept_pool(game, pool_id, actor):
    return engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=actor, expected_version=None, now=later()
    )


# --------------------------------------------------------------------------
# Crossing voids a sibling Pool, loud, never masked
# --------------------------------------------------------------------------


def test_sibling_pool_crossing_voids_it_with_the_louder_reason():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    a, b = list(game.market.keys())[:2]
    proposal_id = _propose(game, tedy, a, b)

    c, d, e = [eid for eid in game.market if eid not in (a, b)][:3]
    _force_order(game, e, c, d)  # e best, c middle, d worst

    # mortia's pool (c, d): d worse than c -- locked rising = d.
    pool1_id = _pool(game, mortia, proposal_id, c, d, visibility="private")
    assert game.pools[pool1_id].swap.rising_entity_id == d

    # hanky's pool (d, e), public: d worse than e -- locked rising = d too
    # (an independent lock on a different pair, coincidence not collision).
    pool2_id = _pool(game, hanky, proposal_id, d, e, visibility="public")
    assert game.pools[pool2_id].swap.rising_entity_id == d

    # tedy (base proposer) accepts hanky's public pool2: (d, e) trade
    # positions -- d jumps to e's old (best) position, now BETTER than c.
    # mortia's pool1 locked "d worse than c"; live now says the opposite --
    # crossed. This happens during pool2's own _execute_swap, BEFORE
    # resolve_sibling_pools would otherwise have force-resolved pool1 as
    # the generic PREEMPTED_BY_OTHER_ACTION -- crossing gets there first
    # and wins with the louder, more specific reason.
    events = _accept_pool(game, pool2_id, tedy)

    pool1 = game.pools[pool1_id]
    assert pool1.status == ResolutionStatus.RESOLVED
    assert pool1.resolution_reason == PoolResolutionReason.VOIDED_MARKET_SWUNG

    pool2 = game.pools[pool2_id]
    assert pool2.resolution_reason == PoolResolutionReason.EXECUTED
    proposal = game.proposals[proposal_id]
    assert proposal.resolution_reason == ProposalResolutionReason.EXECUTED

    reasons = [ev.payload.get("reason") for ev in events if ev.type.value == "POOL_RESOLVED"]
    assert "voided_market_swung" in reasons

    # Loud, never masked -- unlike Pass's EXPIRED_ALL_PASSED, visible
    # identically to every live audience.
    public_view = project(game, PublicAudience())
    tedy_view = project(game, PlayerAudience(tedy))
    voided_public = next(p for p in public_view["pools"] if p["pool_id"] == pool1_id)
    voided_self = next(p for p in tedy_view["pools"] if p["pool_id"] == pool1_id)
    assert voided_public["resolution_reason"] == "voided_market_swung"
    assert voided_self["resolution_reason"] == "voided_market_swung"


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


def test_rising_entity_id_exposed_in_projection_and_pinned():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = list(game.market.keys())[:2]
    _force_order(game, a, b)

    proposal_id = _propose(game, tedy, a, b)
    view = project(game, PublicAudience())
    proposal_view = next(p for p in view["proposals"] if p["proposal_id"] == proposal_id)
    assert proposal_view["rising_entity_id"] == b
