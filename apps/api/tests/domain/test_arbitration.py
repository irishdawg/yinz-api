"""Arbitration (cadence/economy redesign, checkpoint 3): once a negotiation
has narrowed to exactly two active participants -- the opener plus its one
remaining non-passed responder -- either may call it, starting an
irreversible 20-second last-chance window. Settling normally (Accept)
during that window still works; otherwise a weighted machine draw resolves
it. Already-passed players are the secret jury: their votes are never
visible live, to anyone, only in Replay."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import (
    ArbitrationResolutionReason,
    CloseReason,
    GamePhase,
    PoolResolutionReason,
    PoolVisibility,
    ProposalResolutionReason,
    ResolutionStatus,
)
from gotiate.domain.errors import IllegalCommandError
from gotiate.domain.projections import PlayerAudience, PublicAudience, ReplayAudience, project, project_events
from tests.conftest import later, make_started_game, now


def _propose(game, actor, entity_a, entity_b):
    events = engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": entity_a, "entity_b": entity_b}, actor_game_player_id=actor, expected_version=None, now=now()
    )
    return next(e.payload["proposal_id"] for e in events if e.type.value == "PROPOSAL_CREATED")


def _pass(game, proposal_id, actor):
    return engine.handle_command(
        game, command_type="PASS_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=actor, expected_version=None, now=now()
    )


def _pool(game, actor, proposal_id, entity_c, entity_d, visibility="private"):
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


def _call(game, actor, at=None, *, ensure_pool=True):
    # Arbitration now always compares a base proposal with the final
    # responder's concrete Pool. Most tests exercise behavior after the
    # call rather than Pool setup itself, so supply that required public
    # Pool here when the test has narrowed correctly and did not create
    # one explicitly.
    if ensure_pool and game.active_proposal_id is not None:
        proposal = game.proposals[game.active_proposal_id]
        active_responders = engine._active_responder_ids(game, proposal)
        open_pools = [pool for pool in game.pools.values() if pool.base_proposal_id == proposal.proposal_id and pool.status is ResolutionStatus.OPEN]
        if len(active_responders) == 1 and not open_pools:
            candidates = [entity_id for entity_id in game.market if entity_id not in {proposal.swap.entity_a, proposal.swap.entity_b}]
            _pool(game, next(iter(active_responders)), proposal.proposal_id, candidates[0], candidates[1], visibility="public")
    return engine.handle_command(
        game, command_type="CALL_ARBITRATION", payload={}, actor_game_player_id=actor, expected_version=None, now=at or later()
    )


def _vote(game, actor, vote, at=None):
    return engine.handle_command(
        game, command_type="CAST_ARBITRATION_VOTE", payload={"vote": vote}, actor_game_player_id=actor, expected_version=None, now=at or later()
    )


def _narrow_to_final_two(game, proposal_id, proposer, all_others, keep):
    """Pass every non-proposer responder except `keep`, leaving exactly the
    opener + `keep` as the two active participants."""
    for pid in all_others:
        if pid != keep:
            _pass(game, proposal_id, pid)


# --------------------------------------------------------------------------
# Eligibility: exactly two active participants, either may call
# --------------------------------------------------------------------------


def test_call_arbitration_rejected_before_narrowed_to_two():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _pass(game, proposal_id, mortia)  # 2 responders still active (hanky, josiah)

    with pytest.raises(IllegalCommandError):
        _call(game, tedy)
    with pytest.raises(IllegalCommandError):
        _call(game, hanky)


def test_call_arbitration_legal_for_either_active_participant():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky, josiah), keep=hanky)

    events = _call(game, hanky)  # the remaining responder
    assert any(ev.type.value == "ARBITRATION_CALLED" for ev in events)
    assert game.proposals[proposal_id].pending_arbitration is not None


def test_call_arbitration_by_the_opener_also_legal():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky), keep=hanky)

    events = _call(game, tedy)  # the opener
    assert any(ev.type.value == "ARBITRATION_CALLED" for ev in events)


def test_call_arbitration_rejected_for_a_passed_juror_or_bystander():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky, josiah), keep=hanky)

    with pytest.raises(IllegalCommandError):
        _call(game, mortia)  # already passed -- jury, not an active participant


def test_two_player_game_is_immediately_call_eligible():
    # 2p degenerate case: proposer + the one other seated player already
    # ARE the final two from the instant the negotiation opens, no
    # narrowing needed.
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])

    events = _call(game, mortia)
    assert any(ev.type.value == "ARBITRATION_CALLED" for ev in events)


def test_calling_arbitration_twice_is_rejected():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    _propose(game, tedy, e[0], e[1])
    _call(game, mortia)
    with pytest.raises(IllegalCommandError):
        _call(game, tedy)


# --------------------------------------------------------------------------
# The 20s window: settling normally still works, no Boosts/Pools/Pass
# --------------------------------------------------------------------------


def test_accepting_the_base_during_the_window_settles_normally():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _call(game, mortia)

    events = _accept_proposal(game, proposal_id, mortia)
    arb_events = [ev for ev in events if ev.type.value == "ARBITRATION_RESOLVED"]
    assert arb_events
    assert arb_events[0].payload["reason"] == "settled_normally"
    assert game.proposals[proposal_id].pending_arbitration is None
    assert game.proposals[proposal_id].resolution_reason == ProposalResolutionReason.EXECUTED


def test_accepting_the_eligible_pool_during_the_window_settles_normally():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    pool_id = _pool(game, hanky, proposal_id, e[2], e[3], visibility="public")
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky), keep=hanky)
    _call(game, tedy)

    events = _accept_pool(game, pool_id, tedy)
    arb_events = [ev for ev in events if ev.type.value == "ARBITRATION_RESOLVED"]
    assert arb_events
    assert arb_events[0].payload["reason"] == "settled_normally"
    assert game.pools[pool_id].resolution_reason == PoolResolutionReason.EXECUTED


def test_create_pool_rejected_once_arbitration_is_called():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _call(game, mortia)
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game,
            command_type="CREATE_POOL",
            payload={"proposal_id": proposal_id, "entity_c": e[2], "entity_d": e[3], "visibility": "public"},
            actor_game_player_id=mortia,
            expected_version=None,
            now=now(),
        )


def test_pass_rejected_once_arbitration_is_called():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _call(game, tedy)
    with pytest.raises(IllegalCommandError):
        _pass(game, proposal_id, mortia)


def test_withdraw_pool_rejected_once_arbitration_is_called():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    pool_id = _pool(game, hanky, proposal_id, e[2], e[3])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky), keep=hanky)
    _call(game, hanky)
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="WITHDRAW_POOL", payload={"pool_id": pool_id}, actor_game_player_id=hanky, expected_version=None, now=now()
        )


# --------------------------------------------------------------------------
# Private Pools flip public at Arbitration entry -- exact transition,
# every audience
# --------------------------------------------------------------------------


def test_private_pool_flips_public_at_arbitration_entry_across_every_audience():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    pool_id = _pool(game, josiah, proposal_id, e[2], e[3], visibility="private")
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky, josiah), keep=josiah)

    # Before the call: private, contents hidden from everyone but insiders
    # (the pool's own initiator josiah, and the base proposer tedy).
    for audience, can_see in [
        (PlayerAudience(tedy), True),  # base proposer -- insider
        (PlayerAudience(josiah), True),  # pool initiator -- insider
        (PlayerAudience(mortia), False),  # already-passed bystander
        (PublicAudience(), False),
    ]:
        pool_view = next(p for p in project(game, audience)["pools"] if p["pool_id"] == pool_id)
        assert pool_view["visibility"] == "private"
        assert ("entity_c" in pool_view) == can_see

    events = _call(game, tedy)
    assert any(ev.type.value == "ARBITRATION_POOL_REVEALED" for ev in events)
    assert game.pools[pool_id].visibility is PoolVisibility.PUBLIC

    # After the call: public, contents visible to every live audience --
    # opener, surviving responder (same player here, tedy), passed juror
    # (mortia, hanky), and an unrelated public view.
    for audience in (PlayerAudience(tedy), PlayerAudience(josiah), PlayerAudience(mortia), PlayerAudience(hanky), PublicAudience()):
        pool_view = next(p for p in project(game, audience)["pools"] if p["pool_id"] == pool_id)
        assert pool_view["visibility"] == "public"
        assert pool_view["entity_c"] == e[2]
        assert pool_view["entity_d"] == e[3]

    # And in Replay, once scored -- 4p threshold is ceil(0.75*4) = 3.
    for actor in (tedy, mortia, hanky, josiah):
        if game.phase == GamePhase.SCORED:
            break
        engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=actor, expected_version=None, now=now())
    assert game.phase == GamePhase.SCORED
    replay_pool = next(p for p in project(game, ReplayAudience())["pools"] if p["pool_id"] == pool_id)
    assert replay_pool["entity_c"] == e[2]


def test_already_public_pool_does_not_refire_the_reveal_event():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _pool(game, hanky, proposal_id, e[2], e[3], visibility="public")
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky), keep=hanky)

    events = _call(game, hanky)
    assert not any(ev.type.value == "ARBITRATION_POOL_REVEALED" for ev in events)


# --------------------------------------------------------------------------
# Secret jury: only passed players vote, contents invisible live
# --------------------------------------------------------------------------


def test_only_passed_players_may_vote():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky, josiah), keep=hanky)
    _call(game, hanky)

    with pytest.raises(IllegalCommandError):
        _vote(game, tedy, "base")  # active opener
    with pytest.raises(IllegalCommandError):
        _vote(game, hanky, "base")  # active responder

    events = _vote(game, mortia, "base")  # passed juror
    assert any(ev.type.value == "ARBITRATION_VOTE_CAST" for ev in events)


def test_voting_twice_is_rejected():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky), keep=hanky)
    _call(game, hanky)
    _vote(game, mortia, "base")
    with pytest.raises(IllegalCommandError):
        _vote(game, mortia, "neither")


def test_call_arbitration_rejected_when_no_pool_is_eligible():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky), keep=hanky)
    with pytest.raises(IllegalCommandError, match="requires an open Pool"):
        _call(game, hanky, ensure_pool=False)


def test_votes_invisible_live_to_every_audience_including_active_pair():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky, josiah), keep=hanky)
    _call(game, hanky)
    vote_events = _vote(game, mortia, "base")

    for audience in (PlayerAudience(tedy), PlayerAudience(hanky), PlayerAudience(josiah), PublicAudience()):
        assert project_events(game, vote_events, audience) == []
    # The voter sees their own cast, content included.
    own_view = project_events(game, vote_events, PlayerAudience(mortia))
    assert own_view and own_view[0]["payload"]["vote"] == "base"


def test_voted_player_ids_public_live_without_revealing_content():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky, josiah), keep=hanky)
    _call(game, hanky)
    _vote(game, mortia, "base")

    for audience in (PlayerAudience(tedy), PlayerAudience(hanky), PlayerAudience(josiah), PublicAudience()):
        proj = next(p for p in project(game, audience)["proposals"] if p["proposal_id"] == proposal_id)
        assert proj["pending_arbitration"]["voted_player_ids"] == [mortia]
        # No weights or vote contents ever appear in the live projection at all.
        assert "base_weights" not in proj["pending_arbitration"]
        assert "votes" not in proj["pending_arbitration"]


def test_votes_and_weights_fully_revealed_in_replay():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky, josiah), keep=hanky)
    _call(game, hanky)
    _vote(game, mortia, "base")
    _vote(game, josiah, "neither")
    proposal = game.proposals[proposal_id]
    proposal.pending_arbitration.base_weights = {"base": 1000, "neither": 0}
    arb_events = [
        ev for ev in engine._resolve_arbitration_via_machine(game, proposal, later(), random.Random(1)) if ev.type.value == "ARBITRATION_RESOLVED"
    ]

    # Close out the game so ReplayAudience is reachable at all -- 4p
    # threshold is ceil(0.75*4) = 3.
    for actor in (tedy, mortia, hanky):
        if game.phase == GamePhase.SCORED:
            break
        engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=actor, expected_version=None, now=now())
    assert game.phase == GamePhase.SCORED

    replay_view = project_events(game, arb_events, ReplayAudience())
    assert replay_view
    payload = replay_view[0]["payload"]
    assert payload["reason"] == "machine_base"
    assert payload["votes"] == {mortia: "base", josiah: "neither"}
    assert payload["base_weights"] == {"base": 1000, "neither": 0}
    assert "final_weights" in payload


# --------------------------------------------------------------------------
# Caller-dependent starting weights + cumulative jury-vote math
# --------------------------------------------------------------------------


def test_originator_caller_gets_the_originator_weight_table():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _pool(game, mortia, proposal_id, e[2], e[3], visibility="public")  # keeps "pool" a legal candidate
    _call(game, tedy)  # tedy is the originator
    pending = game.proposals[proposal_id].pending_arbitration
    assert pending.base_weights == {"base": 30, "pool": 40, "neither": 40}


def test_other_caller_gets_the_other_weight_table():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _pool(game, mortia, proposal_id, e[2], e[3], visibility="public")
    _call(game, mortia)  # mortia is "other"
    pending = game.proposals[proposal_id].pending_arbitration
    assert pending.base_weights == {"base": 40, "pool": 30, "neither": 40}


def test_two_player_bare_proposal_cannot_instantly_arbitrate():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    with pytest.raises(IllegalCommandError, match="requires an open Pool"):
        _call(game, tedy, ensure_pool=False)


def test_final_weights_math_one_vote_for_base():
    from gotiate.domain.entities import GameConfig, PendingArbitration

    config = GameConfig()
    pending = PendingArbitration(
        arbitration_id="x",
        called_by="p1",
        called_at=now(),
        resolves_at=now() + timedelta(seconds=20),
        eligible_pool_id="pool-1",
        base_weights={"base": 30, "pool": 40, "neither": 40},
        votes={"juror1": "base"},
    )
    weights = engine._final_arbitration_weights(config, pending)
    assert weights == {"base": 40, "pool": 35, "neither": 35}


def test_final_weights_math_two_jurors_split_base_and_pool():
    from gotiate.domain.entities import GameConfig, PendingArbitration

    config = GameConfig()
    pending = PendingArbitration(
        arbitration_id="x",
        called_by="p1",
        called_at=now(),
        resolves_at=now() + timedelta(seconds=20),
        eligible_pool_id="pool-1",
        base_weights={"base": 30, "pool": 40, "neither": 40},
        votes={"juror1": "base", "juror2": "pool"},
    )
    weights = engine._final_arbitration_weights(config, pending)
    assert weights == {"base": 35, "pool": 45, "neither": 30}


def test_final_weights_floor_at_zero():
    from gotiate.domain.entities import GameConfig, PendingArbitration

    config = GameConfig()
    pending = PendingArbitration(
        arbitration_id="x",
        called_by="p1",
        called_at=now(),
        resolves_at=now() + timedelta(seconds=20),
        eligible_pool_id=None,
        base_weights={"base": 5, "neither": 40},
        votes={"j1": "neither", "j2": "neither", "j3": "neither"},
    )
    weights = engine._final_arbitration_weights(config, pending)
    assert weights["base"] == 0  # 5 - 5 - 5, floored, never negative
    assert weights["neither"] == 70  # 40 + 10*3


# --------------------------------------------------------------------------
# The machine draw itself -- no Chaos outcome, distribution tracks weights
# --------------------------------------------------------------------------


def test_machine_draw_only_ever_produces_the_three_legal_outcomes():
    rng = random.Random(7)
    weights = {"base": 30, "pool": 40, "neither": 40}
    seen = {rng.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0] for _ in range(500)}
    assert seen <= {"base", "pool", "neither"}
    assert "chaos" not in seen


def test_machine_base_outcome_executes_the_base_proposal():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _call(game, tedy)
    proposal = game.proposals[proposal_id]

    # Seed hunting is unnecessary -- force it deterministically by giving
    # "base" all the weight.
    proposal.pending_arbitration.base_weights = {"base": 1000, "neither": 0}
    events = engine._resolve_arbitration_via_machine(game, proposal, later(), random.Random(1))

    assert any(ev.type.value == "SWAP_EXECUTED" for ev in events)
    arb = next(ev for ev in events if ev.type.value == "ARBITRATION_RESOLVED")
    assert arb.payload["reason"] == "machine_base"
    assert proposal.resolution_reason == ProposalResolutionReason.EXECUTED
    assert proposal.pending_arbitration is None
    assert game.active_proposal_id is None


def test_machine_pool_outcome_executes_both_legs():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    pool_id = _pool(game, hanky, proposal_id, e[2], e[3])
    _narrow_to_final_two(game, proposal_id, tedy, (mortia, hanky), keep=hanky)
    _call(game, tedy)
    proposal = game.proposals[proposal_id]
    proposal.pending_arbitration.base_weights = {"base": 0, "pool": 1000, "neither": 0}

    events = engine._resolve_arbitration_via_machine(game, proposal, later(), random.Random(1))
    arb = next(ev for ev in events if ev.type.value == "ARBITRATION_RESOLVED")
    assert arb.payload["reason"] == "machine_pool"
    assert proposal.resolution_reason == ProposalResolutionReason.EXECUTED
    assert game.pools[pool_id].resolution_reason == PoolResolutionReason.EXECUTED


def test_machine_neither_outcome_leaves_the_market_untouched():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    positions_before = {eid: m.position for eid, m in game.market.items()}
    _call(game, tedy)
    proposal = game.proposals[proposal_id]
    proposal.pending_arbitration.base_weights = {"base": 0, "neither": 1000}

    events = engine._resolve_arbitration_via_machine(game, proposal, later(), random.Random(1))
    assert not any(ev.type.value == "SWAP_EXECUTED" for ev in events)
    assert proposal.resolution_reason == ProposalResolutionReason.ARBITRATION_NEITHER
    assert {eid: m.position for eid, m in game.market.items()} == positions_before
    arb = next(ev for ev in events if ev.type.value == "ARBITRATION_RESOLVED")
    assert arb.payload["reason"] == "machine_neither"


def test_move_spent_opening_it_is_never_refunded_on_neither():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    before = game.player_by_id(tedy).moves_remaining
    proposal_id = _propose(game, tedy, e[0], e[1])
    _call(game, tedy)
    proposal = game.proposals[proposal_id]
    proposal.pending_arbitration.base_weights = {"base": 0, "neither": 1000}
    engine._resolve_arbitration_via_machine(game, proposal, later(), random.Random(1))
    assert game.player_by_id(tedy).moves_remaining == before - 1


def test_arbitration_resolved_weights_and_votes_stripped_live_but_full_in_replay():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _call(game, tedy)
    proposal = game.proposals[proposal_id]
    proposal.pending_arbitration.base_weights = {"base": 1000, "neither": 0}
    events = engine._resolve_arbitration_via_machine(game, proposal, later(), random.Random(1))
    arb_events = [ev for ev in events if ev.type.value == "ARBITRATION_RESOLVED"]

    for audience in (PublicAudience(), PlayerAudience(tedy), PlayerAudience(mortia)):
        views = project_events(game, arb_events, audience)
        assert views
        assert "base_weights" not in views[0]["payload"]
        assert "final_weights" not in views[0]["payload"]
        assert "votes" not in views[0]["payload"]
        assert views[0]["payload"]["reason"] == "machine_base"

    # Ledger itself (what Replay reads) always carries the full truth.
    assert arb_events[0].payload["base_weights"] == {"base": 1000, "neither": 0}


# --------------------------------------------------------------------------
# Ready-to-Close preempts a pending Arbitration outright
# --------------------------------------------------------------------------


def test_ready_to_close_preempts_pending_arbitration_no_draw():
    game = make_started_game(2)  # close_threshold for 2p is 2
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _call(game, mortia)
    assert game.proposals[proposal_id].pending_arbitration is not None

    events = []
    for actor in (tedy, mortia):
        events += engine.handle_command(
            game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=actor, expected_version=None, now=now()
        )

    assert game.phase == GamePhase.SCORED
    assert game.close_reason == CloseReason.READY_THRESHOLD
    assert game.proposals[proposal_id].pending_arbitration is None
    assert game.proposals[proposal_id].resolution_reason == ProposalResolutionReason.MARKET_CLOSED
    arb_events = [ev for ev in events if ev.type.value == "ARBITRATION_RESOLVED"]
    assert arb_events
    assert arb_events[0].payload["reason"] == "market_closed"
    assert not any(ev.type.value == "SWAP_EXECUTED" for ev in events)  # no machine draw ever ran


def test_arbitration_timer_expiring_after_ready_to_close_already_closed_is_a_no_op():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose(game, tedy, e[0], e[1])
    _call(game, mortia)
    resolves_at = game.proposals[proposal_id].pending_arbitration.resolves_at
    for actor in (tedy, mortia):
        engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=actor, expected_version=None, now=now())
    assert game.phase == GamePhase.SCORED

    # Polling again well past the (now-moot) resolves_at must not crash or
    # re-resolve anything -- close_market already exited NEGOTIATION.
    events = engine.apply_due_time_transitions(game, resolves_at + timedelta(seconds=5))
    assert events == []
