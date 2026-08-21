"""PASS_PROPOSAL — a permanent, per-player exit from an open proposal that
removes that player from the active negotiating set.

NOTE (cadence/economy redesign, checkpoint 2): Pass is now fully public.
The old anonymity model (proposer-only aggregate count, live masking of
auto-expiry to look like an ordinary withdrawal, the proposal disappearing
from a passer's own view) is gone entirely -- a Pass is a visible,
intentional narrowing of who's still deciding, and passed_player_ids is
public to everyone. A passed player still SEES the proposal (and any Pool
on it), just can no longer act on it."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import GamePhase, ProposalResolutionReason, ResolutionStatus
from gotiate.domain.errors import IllegalCommandError
from gotiate.domain.projections import PlayerAudience, PublicAudience, ReplayAudience, project, project_events
from tests.conftest import later, make_started_game, now


def _pass(game, proposal_id, actor):
    return engine.handle_command(
        game, command_type="PASS_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=actor, expected_version=None, now=now()
    )


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


# --------------------------------------------------------------------------
# Legality
# --------------------------------------------------------------------------


def test_proposer_cannot_pass_own_proposal():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    with pytest.raises(IllegalCommandError):
        _pass(game, proposal_id, tedy)


def test_passing_twice_is_rejected():
    game = make_started_game(3)
    tedy, mortia, _ = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pass(game, proposal_id, mortia)
    with pytest.raises(IllegalCommandError):
        _pass(game, proposal_id, mortia)


def test_passing_with_an_open_pool_is_rejected_until_withdrawn():
    game = make_started_game(3)
    tedy, mortia, _ = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    pool_id = _pool(game, mortia, proposal_id, entities[2], entities[3])

    with pytest.raises(IllegalCommandError):
        _pass(game, proposal_id, mortia)

    engine.handle_command(game, command_type="WITHDRAW_POOL", payload={"pool_id": pool_id}, actor_game_player_id=mortia, expected_version=None, now=now())
    _pass(game, proposal_id, mortia)  # legal now
    assert mortia in game.proposals[proposal_id].passed_player_ids


def test_passing_is_unaffected_by_other_players_open_pools():
    # The ordering rule is scoped to the passer's OWN Pool only -- other
    # players' Pools, public or private, never block a Pass.
    game = make_started_game(4)
    tedy, mortia, hanky, _ = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pool(game, mortia, proposal_id, entities[2], entities[3])

    _pass(game, proposal_id, hanky)  # hanky holds no pool of his own
    assert hanky in game.proposals[proposal_id].passed_player_ids


def test_passed_player_cannot_accept_pool_or_accept_pool():
    game = make_started_game(4)
    tedy, mortia, hanky, _ = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pass(game, proposal_id, mortia)

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=mortia, expected_version=None, now=now()
        )
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game,
            command_type="CREATE_POOL",
            payload={"proposal_id": proposal_id, "entity_c": entities[2], "entity_d": entities[3], "visibility": "public"},
            actor_game_player_id=mortia,
            expected_version=None,
            now=now(),
        )

    pool_id = _pool(game, hanky, proposal_id, entities[2], entities[3])
    with pytest.raises(IllegalCommandError):
        engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=mortia, expected_version=None, now=now())


def test_proposer_can_propose_again_immediately_after_auto_expiry():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pass(game, proposal_id, mortia)
    # EXPIRED_ALL_PASSED resolution clears game.active_proposal_id (the
    # single choke point in engine._resolve_proposal), so the single
    # -active-negotiation gate no longer blocks a fresh PROPOSE_SWAP.
    assert game.active_proposal_id is None
    new_id = _propose(game, tedy, entities[2], entities[3])
    assert new_id != proposal_id
    assert game.proposals[new_id].status == ResolutionStatus.OPEN


# --------------------------------------------------------------------------
# Live-view visibility -- narrowing is visible, not an omission
# --------------------------------------------------------------------------


def test_passed_proposal_and_its_pools_stay_visible_to_the_passer():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pool(game, hanky, proposal_id, entities[2], entities[3])
    _pass(game, proposal_id, mortia)

    mortia_view = project(game, PlayerAudience(mortia))
    assert len(mortia_view["proposals"]) == 1
    assert len(mortia_view["pools"]) == 1
    assert mortia in mortia_view["proposals"][0]["passed_player_ids"]

    josiah_view = project(game, PlayerAudience(josiah))
    assert len(josiah_view["proposals"]) == 1
    assert len(josiah_view["pools"]) == 1


def test_passed_player_ids_visible_to_everyone():
    game = make_started_game(4)
    tedy, mortia, hanky, _ = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pass(game, proposal_id, mortia)

    for audience in (PlayerAudience(tedy), PlayerAudience(hanky), PlayerAudience(mortia), PublicAudience()):
        view = project(game, audience)
        proj = next(p for p in view["proposals"] if p["proposal_id"] == proposal_id)
        assert proj["passed_player_ids"] == [mortia]


# --------------------------------------------------------------------------
# Auto-expiry
# --------------------------------------------------------------------------


def test_two_player_game_expires_on_a_single_pass():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pass(game, proposal_id, mortia)
    proposal = game.proposals[proposal_id]
    assert proposal.status == ResolutionStatus.RESOLVED
    assert proposal.resolution_reason == ProposalResolutionReason.EXPIRED_ALL_PASSED


def test_auto_expires_only_once_every_other_player_has_passed():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])

    _pass(game, proposal_id, mortia)
    assert game.proposals[proposal_id].status == ResolutionStatus.OPEN
    _pass(game, proposal_id, hanky)
    assert game.proposals[proposal_id].status == ResolutionStatus.OPEN
    events = _pass(game, proposal_id, josiah)  # last non-proposer

    proposal = game.proposals[proposal_id]
    assert proposal.status == ResolutionStatus.RESOLVED
    assert proposal.resolution_reason == ProposalResolutionReason.EXPIRED_ALL_PASSED
    assert game.phase == GamePhase.NEGOTIATION  # no game-level effect
    assert any(e.type.value == "PROPOSAL_RESOLVED" for e in events)


# --------------------------------------------------------------------------
# Fully public, unmasked -- the opposite of the old anonymity model
# --------------------------------------------------------------------------


def test_expired_all_passed_is_visible_unmasked_to_every_live_audience():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pass(game, proposal_id, mortia)
    _pass(game, proposal_id, hanky)

    proposal = game.proposals[proposal_id]
    assert proposal.resolution_reason == ProposalResolutionReason.EXPIRED_ALL_PASSED

    for audience in (PublicAudience(), PlayerAudience(tedy), PlayerAudience(mortia)):
        view = project(game, audience)
        proj = next(p for p in view["proposals"] if p["proposal_id"] == proposal_id)
        assert proj["resolution_reason"] == "expired_all_passed"

    # ReplayAudience sees the same true value -- no longer a distinct
    # reveal, since there's nothing left to unmask.
    for actor in (tedy, mortia, hanky):
        engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=actor, expected_version=None, now=now())
    assert game.phase == GamePhase.SCORED
    replay_view = project(game, ReplayAudience())
    proj = next(p for p in replay_view["proposals"] if p["proposal_id"] == proposal_id)
    assert proj["resolution_reason"] == "expired_all_passed"


def test_expired_all_passed_unmasked_in_event_log_too():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pass(game, proposal_id, mortia)
    events = _pass(game, proposal_id, hanky)

    resolved_events = [e for e in events if e.type.value == "PROPOSAL_RESOLVED"]
    assert resolved_events
    assert resolved_events[0].payload["reason"] == "expired_all_passed"

    public_views = project_events(game, resolved_events, PublicAudience())
    assert public_views[0]["payload"]["reason"] == "expired_all_passed"

    replay_views = project_events(game, resolved_events, ReplayAudience())
    assert replay_views[0]["payload"]["reason"] == "expired_all_passed"


def test_proposal_passed_event_is_public():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    events = _pass(game, proposal_id, mortia)
    passed_events = [e for e in events if e.type.value == "PROPOSAL_PASSED"]
    assert passed_events

    for audience in (PlayerAudience(mortia), PlayerAudience(tedy), PlayerAudience(hanky), PublicAudience()):
        views = project_events(game, passed_events, audience)
        assert views
        assert views[0]["actor_game_player_id"] == mortia


# --------------------------------------------------------------------------
# Explicit non-goal: Pass never filters the event log / activity ticker
# --------------------------------------------------------------------------


def test_passer_still_sees_the_proposal_execute_in_their_own_event_log():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    proposal_id = _propose(game, tedy, entities[0], entities[1])
    _pass(game, proposal_id, mortia)  # mortia is out

    accept_events = engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=hanky, expected_version=None, now=later()
    )
    assert any(e.type.value == "SWAP_EXECUTED" for e in accept_events)

    mortia_views = project_events(game, accept_events, PlayerAudience(mortia))
    seen_types = {v["type"] for v in mortia_views}
    assert "SWAP_EXECUTED" in seen_types
    assert "PROPOSAL_RESOLVED" in seen_types
