"""Private Influence economy: the pieces beyond bare-proposal mechanics
(covered in test_influence.py) -- the one-proposal-per-player cap, the
reserve-action lock while an authored negotiation is open, ACCEPT_POOL's
liability-combine rule, and the Influence-secrecy audit on event payloads.
See the plan's design writeup."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import find_swap_pair, later, make_started_game, now


def _reserve_of(game, player_id):
    return next(h for h in game.holdings.values() if h.owner_player_id == player_id and h.zone.value == "reserve_unrevealed")


def _propose(game, actor, a, b):
    return engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": a, "entity_b": b}, actor_game_player_id=actor, expected_version=None, now=now()
    )


# --------------------------------------------------------------------------
# One open bare proposal per player
# --------------------------------------------------------------------------


def test_a_second_bare_proposal_auto_withdraws_the_first():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    a1, b1 = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, a1, b1)
    first_id = next(iter(game.proposals))

    # Any other pair -- this test is about the cap, not liability.
    other = [e for e in game.market if e not in (a1, b1)]
    _propose(game, tedy, other[0], other[1])

    assert game.proposals[first_id].status.value == "resolved"
    assert game.proposals[first_id].resolution_reason.value == "withdrawn_by_initiator"
    open_proposals = [p for p in game.proposals.values() if p.status.value == "open"]
    assert len(open_proposals) == 1
    assert {open_proposals[0].swap.entity_a, open_proposals[0].swap.entity_b} == {other[0], other[1]}


def test_a_second_bare_proposal_auto_withdraws_the_first_and_its_open_pools():
    game = make_started_game(3)
    tedy, mortia, _hanky = [p.game_player_id for p in game.players]
    a1, b1 = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, a1, b1)
    first_id = next(iter(game.proposals))
    pool_c, pool_d = find_swap_pair(game, mortia, owned_should_rise=False, exclude=frozenset({a1, b1}))
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": first_id, "entity_c": pool_c, "entity_d": pool_d, "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    pool_id = next(iter(game.pools))

    other = [e for e in game.market if e not in (a1, b1)]
    _propose(game, tedy, other[0], other[1])

    assert game.pools[pool_id].status.value == "resolved"
    assert game.pools[pool_id].resolution_reason.value == "base_proposal_withdrawn"


def test_a_new_bare_proposal_rejected_for_other_reasons_leaves_the_old_one_open():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a1, b1 = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, a1, b1)
    first_id = next(iter(game.proposals))

    # A different pair, but already open for someone else -- the duplicate
    # -pair check must still see this proposed pair as taken, and reject
    # *before* ever touching Tedy's still-open first proposal.
    a2, b2 = find_swap_pair(game, mortia, owned_should_rise=False, exclude=frozenset({a1, b1}))
    _propose(game, mortia, a2, b2)

    with pytest.raises(IllegalCommandError):
        _propose(game, tedy, a2, b2)

    assert game.proposals[first_id].status.value == "open"


def test_a_second_proposal_for_the_same_pair_from_another_player_is_rejected():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, a, b)

    with pytest.raises(IllegalCommandError):
        _propose(game, mortia, a, b)
    # Order-independent -- the same pair named the other way round is still a duplicate.
    with pytest.raises(IllegalCommandError):
        _propose(game, mortia, b, a)


def test_a_second_proposal_for_the_same_pair_is_legal_once_the_first_resolves():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, a, b)
    proposal_id = next(iter(game.proposals))
    engine.handle_command(
        game, command_type="WITHDRAW_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    _propose(game, mortia, a, b)  # must not raise
    assert len(game.proposals) == 2


def test_a_second_proposal_is_legal_once_the_first_resolves():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    a1, b1 = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, a1, b1)
    proposal_id = next(iter(game.proposals))
    engine.handle_command(
        game, command_type="WITHDRAW_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    other = [e for e in game.market if e not in (a1, b1)]
    _propose(game, tedy, other[0], other[1])  # must not raise
    assert len(game.proposals) == 2


# --------------------------------------------------------------------------
# WITHDRAW_POOL settlement -- must refund like WITHDRAW_PROPOSAL, not spend
# (a self-withdrawn pool was previously charged instead of refunded; no
# non-execution resolution reason elsewhere in the engine spends).
# --------------------------------------------------------------------------


def test_withdraw_pool_refunds_committed_liability():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    base_a, base_b = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, base_a, base_b)
    proposal_id = next(iter(game.proposals))
    pool_c, pool_d = find_swap_pair(game, mortia, owned_should_rise=True, exclude=frozenset({base_a, base_b}))
    before = game.player_by_id(mortia).influence_available
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": pool_c, "entity_d": pool_d, "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    pool_id = next(iter(game.pools))
    assert game.player_by_id(mortia).influence_committed == 1

    engine.handle_command(game, command_type="WITHDRAW_POOL", payload={"pool_id": pool_id}, actor_game_player_id=mortia, expected_version=None, now=now())

    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_spent == 0
    assert mortia_player.influence_committed == 0
    assert mortia_player.influence_available == before
    assert game.pools[pool_id].resolution_reason.value == "withdrawn_by_initiator"


# --------------------------------------------------------------------------
# Reserve actions are never blocked by an open proposal/pool of your own --
# a locked liability is immune to a later holdings change by construction
# (computed once at authoring time, never recomputed), and a later swap
# crossing your own open negotiation's locked direction already voids it
# loudly via the ordinary crossing-invalidation scan regardless of who
# caused the crossing. An earlier version of this game blocked reserve
# actions in this state defensively (see GAMEPLAY.md); removed once both
# of those existing mechanisms were confirmed to already cover the actual
# risk independently -- these tests now lock in the *relaxed* behavior.
# --------------------------------------------------------------------------


def test_pick_up_reserve_allowed_while_an_authored_proposal_is_open():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, a, b)

    reserve = _reserve_of(game, tedy)
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )  # must not raise
    assert game.player_by_id(tedy).pending_pickup is not None


def test_burn_reserve_allowed_while_an_authored_pool_is_open():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    base_a, base_b = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, base_a, base_b)
    proposal_id = next(iter(game.proposals))
    pool_c, pool_d = find_swap_pair(game, mortia, owned_should_rise=False, exclude=frozenset({base_a, base_b}))
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": pool_c, "entity_d": pool_d, "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )

    reserve = _reserve_of(game, mortia)
    events = engine.handle_command(
        game,
        command_type="BURN_RESERVE_FOR_SWAP",
        payload={"reserve_holding_id": reserve.holding_id, "entity_a": base_a, "entity_b": base_b},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )  # must not raise
    assert any(e.type.value == "SWAP_EXECUTED" for e in events)


def test_accepting_someone_elses_proposal_does_not_block_reserve_actions():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=False)
    _propose(game, tedy, a, b)
    proposal_id = next(iter(game.proposals))

    engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=mortia, expected_version=None, now=later()
    )

    reserve = _reserve_of(game, mortia)
    engine.handle_command(
        game, command_type="PICK_UP_RESERVE", payload={"reserve_holding_id": reserve.holding_id}, actor_game_player_id=mortia, expected_version=None, now=now()
    )  # must not raise


# --------------------------------------------------------------------------
# ACCEPT_POOL's combine rule -- private pool, self-accept (base proposer is
# the only legal accepter, so their liability merges a *stored* base-leg
# bit with a *fresh* pool-leg bit)
# --------------------------------------------------------------------------


def _setup_private_pool(game, tedy, mortia, *, base_owned: bool, pool_owned: bool):
    base_a, base_b = find_swap_pair(game, tedy, owned_should_rise=base_owned)
    _propose(game, tedy, base_a, base_b)
    proposal_id = next(iter(game.proposals))
    # The pool's entities are chosen against *Tedy's* holdings (he'll be the
    # accepter) -- Mortia's own resulting pool.initiator_influence_liability
    # is irrelevant to what's being tested here (Tedy's combined charge).
    pool_c, pool_d = find_swap_pair(game, tedy, owned_should_rise=pool_owned, exclude=frozenset({base_a, base_b}))
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": pool_c, "entity_d": pool_d, "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    pool_id = next(iter(game.pools))
    return proposal_id, pool_id


def test_private_pool_self_accept_neither_leg_owned_is_free():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    before = game.player_by_id(tedy).influence_available
    _, pool_id = _setup_private_pool(game, tedy, mortia, base_owned=False, pool_owned=False)
    assert game.player_by_id(tedy).influence_committed == 0  # base leg wasn't liable, nothing committed

    engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now())

    tedy_player = game.player_by_id(tedy)
    assert tedy_player.influence_spent == 0
    assert tedy_player.influence_committed == 0
    assert tedy_player.influence_available == before


def test_private_pool_self_accept_only_pool_leg_owned_charges_fresh():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    before = game.player_by_id(tedy).influence_available
    _, pool_id = _setup_private_pool(game, tedy, mortia, base_owned=False, pool_owned=True)
    assert game.player_by_id(tedy).influence_committed == 0

    engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now())

    tedy_player = game.player_by_id(tedy)
    assert tedy_player.influence_committed == 0
    assert tedy_player.influence_spent == 1
    assert tedy_player.influence_available == before - 1


def test_private_pool_self_accept_only_base_leg_owned_settles_the_locked_amount():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    before = game.player_by_id(tedy).influence_available
    _, pool_id = _setup_private_pool(game, tedy, mortia, base_owned=True, pool_owned=False)
    assert game.player_by_id(tedy).influence_committed == 1  # locked at PROPOSE_SWAP time

    engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now())

    tedy_player = game.player_by_id(tedy)
    assert tedy_player.influence_committed == 0
    assert tedy_player.influence_spent == 1  # the already-committed 1, not a new charge
    assert tedy_player.influence_available == before - 1


def test_private_pool_self_accept_both_legs_owned_still_caps_at_one():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    before = game.player_by_id(tedy).influence_available
    _, pool_id = _setup_private_pool(game, tedy, mortia, base_owned=True, pool_owned=True)
    assert game.player_by_id(tedy).influence_committed == 1

    engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now())

    tedy_player = game.player_by_id(tedy)
    assert tedy_player.influence_committed == 0
    assert tedy_player.influence_spent == 1  # never 2, despite two owned legs
    assert tedy_player.influence_available == before - 1


# --------------------------------------------------------------------------
# ACCEPT_POOL's combine rule -- public pool, third-party accept (Josiah
# authored neither leg, so both bits are evaluated fresh against him)
# --------------------------------------------------------------------------


def _setup_public_pool_third_party(game, tedy, mortia, josiah, *, josiah_owns_base: bool, josiah_owns_pool: bool):
    base_a, base_b = find_swap_pair(game, josiah, owned_should_rise=josiah_owns_base)
    _propose(game, tedy, base_a, base_b)
    proposal_id = next(iter(game.proposals))
    pool_c, pool_d = find_swap_pair(game, josiah, owned_should_rise=josiah_owns_pool, exclude=frozenset({base_a, base_b}))
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": pool_c, "entity_d": pool_d, "visibility": "public"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    return next(iter(game.pools))


def test_public_pool_third_party_accept_owns_neither_is_free():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    before = game.player_by_id(josiah).influence_available
    pool_id = _setup_public_pool_third_party(game, tedy, mortia, josiah, josiah_owns_base=False, josiah_owns_pool=False)

    engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=josiah, expected_version=None, now=later())

    josiah_player = game.player_by_id(josiah)
    assert josiah_player.influence_spent == 0
    assert josiah_player.influence_available == before


@pytest.mark.parametrize("owns_base,owns_pool", [(True, False), (False, True), (True, True)])
def test_public_pool_third_party_accept_owning_either_or_both_caps_at_one(owns_base, owns_pool):
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    before = game.player_by_id(josiah).influence_available
    pool_id = _setup_public_pool_third_party(game, tedy, mortia, josiah, josiah_owns_base=owns_base, josiah_owns_pool=owns_pool)

    engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=josiah, expected_version=None, now=later())

    josiah_player = game.player_by_id(josiah)
    assert josiah_player.influence_spent == 1
    assert josiah_player.influence_available == before - 1


# --------------------------------------------------------------------------
# Influence-secrecy audit: event payloads
# --------------------------------------------------------------------------


def test_proposal_created_event_carries_no_influence_snapshot():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    a, b = find_swap_pair(game, tedy, owned_should_rise=True)
    events = _propose(game, tedy, a, b)
    created = next(e for e in events if e.type.value == "PROPOSAL_CREATED")
    assert "influence" not in created.payload


def test_pool_created_and_resolved_events_carry_no_influence_snapshot():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    a, b = find_swap_pair(game, tedy, owned_should_rise=True)
    _propose(game, tedy, a, b)
    proposal_id = next(iter(game.proposals))
    c, d = find_swap_pair(game, mortia, owned_should_rise=True, exclude=frozenset({a, b}))
    create_events = engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": c, "entity_d": d, "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    created = next(e for e in create_events if e.type.value == "PRIVATE_POOL_CREATED")
    assert "influence" not in created.payload

    pool_id = next(iter(game.pools))
    resolve_events = engine.handle_command(
        game, command_type="WITHDRAW_POOL", payload={"pool_id": pool_id}, actor_game_player_id=mortia, expected_version=None, now=now()
    )
    resolved = next(e for e in resolve_events if e.type.value == "POOL_RESOLVED")
    assert "influence" not in resolved.payload
