"""Market Correction -- the 2-player-only, non-automatic anti-stagnation
mechanic. See the design writeup. Covers targeting (top-two, doubled
-holding protection, destination search), the pure severity formula,
inactivity-driven offer/expiry/cooldown timing, the two distinct
invalidation mechanisms (any negotiated deal -> market_resumed
unconditionally; a crossing burn or a DISCARD_HOLDING ownership change
-> invalidated), and the two review-caught ordering bugs (self
-invalidation across the correction's own two legs; a negotiated deal
that also happens to cross a locked move still resolving as
market_resumed, never invalidated).

Portfolios are set up directly via _set_portfolio rather than fighting
the random deal -- targeting/severity/destination-search all need exact
control over positions and ownership that a random 2-player deal (now
market_size=11, still frequently near-saturated -- see the Market
Correction market-size writeup) can't reliably provide."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import Holding, HoldingZone, MarketCorrectionResolutionReason, ResolutionStatus
from gotiate.domain.events import EventType
from gotiate.domain.projections import PlayerAudience, PublicAudience, ReplayAudience, project, project_events
from tests.conftest import later, make_started_game, now


def _set_portfolio(game, player_id: str, entity_ids: list[str]) -> None:
    """Replaces player_id's entire PORTFOLIO-zone holdings with fresh
    ones for exactly entity_ids (a repeated entity_id creates a
    doubled/anchor holding) -- full control over targeting/severity
    scenarios without depending on the random deal."""
    stale = [hid for hid, h in game.holdings.items() if h.owner_player_id == player_id and h.zone == HoldingZone.PORTFOLIO]
    for hid in stale:
        del game.holdings[hid]
    for entity_id in entity_ids:
        h = Holding(holding_id=engine.new_id(), entity_id=entity_id, owner_player_id=player_id, zone=HoldingZone.PORTFOLIO, revealed_to_owner=True)
        game.holdings[h.holding_id] = h


def _entities_by_position(game) -> list[str]:
    """entities[i] sits at position i+1."""
    return sorted(game.market, key=lambda eid: game.market[eid].position)


def _reserve_of(game, player_id: str) -> Holding:
    return next(h for h in game.holdings.values() if h.owner_player_id == player_id and h.zone == HoldingZone.RESERVE_UNREVEALED)


def _propose(game, actor, entity_a, entity_b):
    events = engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": entity_a, "entity_b": entity_b}, actor_game_player_id=actor, expected_version=None, now=now()
    )
    return next(e.payload["proposal_id"] for e in events if e.type is EventType.PROPOSAL_CREATED)


def _accept_proposal(game, proposal_id, actor):
    return engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=actor, expected_version=None, now=later()
    )


def _burn(game, actor, entity_a, entity_b):
    reserve = _reserve_of(game, actor)
    return engine.handle_command(
        game,
        command_type="BURN_RESERVE_FOR_SWAP",
        payload={"reserve_holding_id": reserve.holding_id, "entity_a": entity_a, "entity_b": entity_b},
        actor_game_player_id=actor,
        expected_version=None,
        now=now(),
    )


def _trigger(game, actor, correction_id):
    return engine.handle_command(
        game,
        command_type="TRIGGER_MARKET_CORRECTION",
        payload={"correction_id": correction_id},
        actor_game_player_id=actor,
        expected_version=None,
        now=now(),
    )


def _move_for(correction, player_id):
    return next(m for m in correction.moves if m.target_player_id == player_id)


# --------------------------------------------------------------------------
# Targeting: top-two selection, doubled-holding protection
# --------------------------------------------------------------------------


def test_targets_only_the_players_top_two_distinct_owned_entities():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    # host: positions 1, 2, 5, 8 (four distinct) -- top-two by position are 1, 2.
    _set_portfolio(game, host, [e[0], e[1], e[4], e[7]])
    # guest: positions 3, 4, 9, 10 -- top-two are 3, 4.
    _set_portfolio(game, guest, [e[2], e[3], e[8], e[9]])

    for seed in range(20):
        correction = engine._construct_market_correction(game, now(), random.Random(seed))
        assert correction is not None
        host_source = _move_for(correction, host).swap.entity_a
        guest_source = _move_for(correction, guest).swap.entity_a
        assert host_source in (e[0], e[1])
        assert guest_source in (e[2], e[3])


def test_singly_owned_alternative_excludes_the_doubled_holding():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    # host's best position (1) is doubled; position 2 is singly owned.
    # Only position 2 should ever be eligible.
    _set_portfolio(game, host, [e[0], e[0], e[1]])
    _set_portfolio(game, guest, [e[4], e[5]])  # positions 5, 6 -- plenty of its own forward room

    sources = set()
    for seed in range(20):
        correction = engine._construct_market_correction(game, now(), random.Random(seed))
        if correction is None:
            continue
        sources.add(_move_for(correction, host).swap.entity_a)
    assert sources == {e[1]}, "the doubled position-1 holding must never be targeted while a singly-owned alternative exists"


def test_a_mutually_owned_entity_is_never_eligible_for_either_players_move():
    # Real playtest catch: without this exclusion, a shared holding could
    # be the OTHER player's move target -- landing a second, uninvited
    # hit on top of your own already-independently-targeted move, when
    # the mechanic is supposed to be exactly one downward move per player.
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    # Both own e[0] (position 1) -- host's own top-two would otherwise be
    # [e[0], e[1]], guest's [e[0], e[2]], but e[0] must never be eligible
    # for *either* move since it's shared.
    _set_portfolio(game, host, [e[0], e[1]])
    _set_portfolio(game, guest, [e[0], e[2]])

    saw_a_correction = False
    for seed in range(20):
        correction = engine._construct_market_correction(game, now(), random.Random(seed))
        if correction is None:
            continue
        saw_a_correction = True
        host_source = _move_for(correction, host).swap.entity_a
        guest_source = _move_for(correction, guest).swap.entity_a
        assert host_source == e[1]
        assert guest_source == e[2]
    assert saw_a_correction


def test_both_doubled_makes_either_eligible():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    # Both of host's top-two are doubled -- the exception applies, either
    # is now eligible. Plenty of forward room for either to succeed.
    _set_portfolio(game, host, [e[0], e[0], e[1], e[1]])
    _set_portfolio(game, guest, [e[4], e[5]])  # positions 5, 6 -- plenty of its own forward room

    sources = set()
    for seed in range(30):
        correction = engine._construct_market_correction(game, now(), random.Random(seed))
        assert correction is not None
        sources.add(_move_for(correction, host).swap.entity_a)
    assert sources == {e[0], e[1]}, "once both top-two are doubled, both must be reachable across enough random draws"


# --------------------------------------------------------------------------
# Destination search: preference order, redraw fallback, reduce-to-None
# --------------------------------------------------------------------------


def test_destination_search_prefers_the_nearest_available_position_to_the_exact_target():
    game = make_started_game(2)
    e = _entities_by_position(game)
    # source at position 2, target_displacement=3 -> exact target = position 5.
    # Only positions 4 and 6 are available -- both distance 1 from the
    # exact target, but the ascending scan must land on the lower (4) one.
    unavailable = set(e) - {e[3], e[5]}  # keep positions 4 (idx3) and 6 (idx5) open
    destination = engine._find_correction_destination(game, source_position=2, target_displacement=3, unavailable=unavailable)
    assert destination == e[3]


def test_destination_search_falls_back_to_nearest_when_exact_target_is_owned():
    game = make_started_game(2)
    e = _entities_by_position(game)
    # source at position 2, target=3 -> exact target = position 5, but it's
    # unavailable; only position 6 is open forward of the source.
    unavailable = set(e) - {e[5]}
    destination = engine._find_correction_destination(game, source_position=2, target_displacement=3, unavailable=unavailable)
    assert destination == e[5]


def test_redraws_to_the_other_eligible_candidate_when_the_first_has_no_destination():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    # host owns position 3 (plenty of forward room) and position 10 (its
    # only forward slot, position 11, is owned by guest -- structurally
    # dead). Regardless of which of the two gets tried first, only
    # position 3 can ever succeed.
    _set_portfolio(game, host, [e[2], e[9]])
    _set_portfolio(game, guest, [e[0], e[10]])

    for seed in range(20):
        correction = engine._construct_market_correction(game, now(), random.Random(seed))
        assert correction is not None
        assert _move_for(correction, host).swap.entity_a == e[2]


def test_construction_returns_none_when_the_only_eligible_candidates_have_no_destination():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    # host's top-two are the last two positions in the market -- position
    # 10's only forward slot is position 11, which host itself owns;
    # position 11 has no forward slot at all. No destination exists for
    # either candidate, regardless of guest's own setup.
    _set_portfolio(game, host, [e[9], e[10]])
    _set_portfolio(game, guest, [e[0], e[1]])

    assert engine._construct_one_correction_move(game, host, target_displacement=5, unavailable_destinations=set(), rng=random.Random(0)) is None
    for seed in range(10):
        assert engine._construct_market_correction(game, now(), random.Random(seed)) is None


# --------------------------------------------------------------------------
# Severity: the pure gap -> displacement formula
# --------------------------------------------------------------------------


def test_tied_projected_value_gives_both_players_equal_target_displacement():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    _set_portfolio(game, host, [e[0], e[3]])  # positions 1, 4 -- value 11 + 8 = 19
    _set_portfolio(game, guest, [e[1], e[2]])  # positions 2, 3 -- value 10 + 9 = 19

    _leader, _trailer, target_displacement = engine._market_correction_target_displacements(game)
    assert target_displacement[host] == target_displacement[guest]


def test_a_large_gap_saturates_the_spread_and_never_exceeds_max_spread():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    _set_portfolio(game, host, [e[0], e[1], e[2], e[3]])  # positions 1-4 -- big value
    _set_portfolio(game, guest, [e[4], e[5]])  # positions 5, 6 -- small value

    market_size = len(game.market)
    config = game.config
    leader_id, trailer_id, target_displacement = engine._market_correction_target_displacements(game)
    max_spread = config.market_correction_max_spread_fraction * market_size

    # A gap this large is already >= gap_saturation_fraction * market_size,
    # so the spread must be pinned at exactly max_spread, not still climbing.
    base = config.market_correction_base_displacement_fraction * market_size
    expected_leader = engine._clamp_displacement(base - max_spread / 2, market_size)
    expected_trailer = engine._clamp_displacement(base + max_spread / 2, market_size)
    assert target_displacement[leader_id] == expected_leader
    assert target_displacement[trailer_id] == expected_trailer
    assert 1 <= target_displacement[leader_id] <= market_size - 1
    assert 1 <= target_displacement[trailer_id] <= market_size - 1


def test_leader_and_trailer_are_assigned_by_projected_value_not_seat_order():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    _set_portfolio(game, host, [e[8]])  # position 9 -- low value
    _set_portfolio(game, guest, [e[0]])  # position 1 -- high value

    leader_id, trailer_id, _ = engine._market_correction_target_displacements(game)
    assert leader_id == guest
    assert trailer_id == host


# --------------------------------------------------------------------------
# Trigger timing: stagnation threshold, offer expiry, post-resolution cooldown
# --------------------------------------------------------------------------


def _make_construction_succeeding_game():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    _set_portfolio(game, host, [e[0], e[3]])
    _set_portfolio(game, guest, [e[1], e[2]])
    return game, host, guest


def test_not_eligible_before_the_stagnation_threshold():
    game, _host, _guest = _make_construction_succeeding_game()
    check_at = game.last_negotiated_execution_at + timedelta(seconds=game.config.market_correction_stagnation_seconds - 1)
    engine.apply_due_time_transitions(game, check_at)
    assert game.pending_market_correction is None


def test_eligible_exactly_at_the_stagnation_boundary():
    game, host, guest = _make_construction_succeeding_game()
    check_at = game.last_negotiated_execution_at + timedelta(seconds=game.config.market_correction_stagnation_seconds)
    events = engine.apply_due_time_transitions(game, check_at)
    assert game.pending_market_correction is not None
    assert any(e.type is EventType.MARKET_CORRECTION_OFFERED for e in events)


def test_expires_at_offer_seconds_and_does_not_push_the_cooldown():
    """EXPIRED means nothing changed -- the market is exactly as stagnant
    as when this was first offered, so cooldown is left untouched rather
    than getting a fresh market_correction_cooldown_seconds tacked on.
    Previously this pushed cooldown forward unconditionally, which is
    what made the real trigger cadence drift away from a clean '90s since
    the last deal' reading -- see _resolve_market_correction's docstring."""
    game, host, guest = _make_construction_succeeding_game()
    cooldown_before = game.market_correction_cooldown_until
    offer_at = game.last_negotiated_execution_at + timedelta(seconds=game.config.market_correction_stagnation_seconds)
    engine.apply_due_time_transitions(game, offer_at)
    correction = game.pending_market_correction
    assert correction is not None

    expire_at = correction.expires_at
    events = engine.apply_due_time_transitions(game, expire_at)
    assert game.pending_market_correction is None
    resolved = next(e for e in events if e.type is EventType.MARKET_CORRECTION_RESOLVED)
    assert resolved.payload["reason"] == MarketCorrectionResolutionReason.EXPIRED.value
    assert game.market_correction_cooldown_until == cooldown_before


def test_expired_correction_re_offers_immediately_if_still_stagnant():
    """The market is still just as stagnant right after an EXPIRED
    resolution -- with cooldown untouched, the very next check (a
    resolve-then-offer split across two ticks, same as one expiry
    resolving and a fresh offer landing on the next ~1s poll in
    production) re-offers, no extra delay tacked on."""
    game, _host, _guest = _make_construction_succeeding_game()
    offer_at = game.last_negotiated_execution_at + timedelta(seconds=game.config.market_correction_stagnation_seconds)
    engine.apply_due_time_transitions(game, offer_at)
    correction = game.pending_market_correction
    expire_at = correction.expires_at

    engine.apply_due_time_transitions(game, expire_at)
    assert game.pending_market_correction is None

    events = engine.apply_due_time_transitions(game, expire_at + timedelta(seconds=1))
    assert game.pending_market_correction is not None
    assert any(e.type is EventType.MARKET_CORRECTION_OFFERED for e in events)


def test_triggered_and_market_resumed_do_push_the_cooldown():
    """Unlike EXPIRED/INVALIDATED, both of these represent genuinely fresh
    activity -- a correction actually firing, or a real negotiated deal
    landing -- so the market does earn a real breather before the next
    one can be offered."""
    game, host, guest = _make_construction_succeeding_game()
    offer_at = game.last_negotiated_execution_at + timedelta(seconds=game.config.market_correction_stagnation_seconds)
    engine.apply_due_time_transitions(game, offer_at)
    correction = game.pending_market_correction
    assert correction is not None

    trigger_at = offer_at + timedelta(seconds=1)
    events = engine.handle_command(
        game,
        command_type="TRIGGER_MARKET_CORRECTION",
        payload={"correction_id": correction.correction_id},
        actor_game_player_id=host,
        expected_version=None,
        now=trigger_at,
    )
    resolved = next(e for e in events if e.type is EventType.MARKET_CORRECTION_RESOLVED)
    assert resolved.payload["reason"] == MarketCorrectionResolutionReason.TRIGGERED.value
    assert game.market_correction_cooldown_until == trigger_at + timedelta(seconds=game.config.market_correction_cooldown_seconds)


def test_a_failed_construction_does_not_push_the_cooldown():
    game = make_started_game(2)
    host, guest = [p.game_player_id for p in game.players]
    e = _entities_by_position(game)
    # Guaranteed-fail geometry (see test_construction_returns_none_...).
    _set_portfolio(game, host, [e[9], e[10]])
    _set_portfolio(game, guest, [e[0], e[1]])

    cooldown_before = game.market_correction_cooldown_until
    check_at = game.last_negotiated_execution_at + timedelta(seconds=game.config.market_correction_stagnation_seconds)
    events = engine.apply_due_time_transitions(game, check_at)
    assert game.pending_market_correction is None
    assert not any(e.type is EventType.MARKET_CORRECTION_OFFERED for e in events)
    assert game.market_correction_cooldown_until == cooldown_before, "a failed construction must not sit out a cooldown -- the next tick should retry immediately"


# --------------------------------------------------------------------------
# Invalidation: two distinct mechanisms, never blended
# --------------------------------------------------------------------------


def _offer_now(game):
    correction = engine._construct_market_correction(game, now(), random.Random(0))
    assert correction is not None
    game.pending_market_correction = correction
    return correction


def test_any_negotiated_deal_resolves_market_resumed_even_with_no_entity_overlap():
    game, host, guest = _make_construction_succeeding_game()
    correction = _offer_now(game)
    used = {m.swap.entity_a for m in correction.moves} | {m.swap.entity_b for m in correction.moves}

    free = [eid for eid in game.market if eid not in used]
    assert len(free) >= 2, "test setup needs two entities untouched by either corrective move"
    disjoint_a, disjoint_b = free[0], free[1]

    proposal_id = _propose(game, guest, disjoint_a, disjoint_b)
    events = _accept_proposal(game, proposal_id, host)

    assert game.pending_market_correction is None
    resolved = next(e for e in events if e.type is EventType.MARKET_CORRECTION_RESOLVED)
    assert resolved.payload["reason"] == MarketCorrectionResolutionReason.MARKET_RESUMED.value


def test_a_negotiated_deal_that_also_crosses_a_locked_move_still_resolves_market_resumed():
    """The ordering regression, explicitly requested on review: a deal
    that happens to touch the same pair as a pending correction's own
    move must never be misclassified as invalidated just because its
    _execute_swap would otherwise see the crossing."""
    game, host, guest = _make_construction_succeeding_game()
    correction = _offer_now(game)
    move = correction.moves[0]
    source, destination = move.swap.entity_a, move.swap.entity_b

    # Proposing and accepting exactly this pair swaps them fully --
    # source takes destination's old (better) position, which is the
    # literal opposite of the correction's own locked direction for it.
    proposal_id = _propose(game, guest, source, destination)
    events = _accept_proposal(game, proposal_id, host)

    assert game.pending_market_correction is None
    resolved = next(e for e in events if e.type is EventType.MARKET_CORRECTION_RESOLVED)
    assert resolved.payload["reason"] == MarketCorrectionResolutionReason.MARKET_RESUMED.value


def test_a_crossing_burn_resolves_invalidated_not_market_resumed():
    game, host, guest = _make_construction_succeeding_game()
    correction = _offer_now(game)
    move = correction.moves[0]
    source, destination = move.swap.entity_a, move.swap.entity_b

    burner = guest if move.target_player_id == host else host
    events = _burn(game, burner, source, destination)

    assert game.pending_market_correction is None
    resolved = next(e for e in events if e.type is EventType.MARKET_CORRECTION_RESOLVED)
    assert resolved.payload["reason"] == MarketCorrectionResolutionReason.INVALIDATED.value


def test_a_non_crossing_burn_leaves_the_correction_untouched():
    game, host, guest = _make_construction_succeeding_game()
    correction = _offer_now(game)
    used = {m.swap.entity_a for m in correction.moves} | {m.swap.entity_b for m in correction.moves}
    free = [eid for eid in game.market if eid not in used]
    assert len(free) >= 2

    events = _burn(game, host, free[0], free[1])

    assert game.pending_market_correction is not None
    assert game.pending_market_correction.correction_id == correction.correction_id
    assert not any(e.type is EventType.MARKET_CORRECTION_RESOLVED for e in events)


def test_discard_holding_that_changes_source_ownership_resolves_invalidated():
    game, host, guest = _make_construction_succeeding_game()
    correction = _offer_now(game)
    move = correction.moves[0]
    owner = move.target_player_id
    source_entity = move.swap.entity_a

    source_holding = next(
        h for h in game.holdings.values() if h.owner_player_id == owner and h.zone == HoldingZone.PORTFOLIO and h.entity_id == source_entity
    )

    reserve = _reserve_of(game, owner)
    pickup_events = engine.handle_command(
        game,
        command_type="PICK_UP_RESERVE",
        payload={"reserve_holding_id": reserve.holding_id},
        actor_game_player_id=owner,
        expected_version=None,
        now=now(),
    )
    pending_pickup_id = next(e.payload["pending_pickup_id"] for e in pickup_events if e.type is EventType.PICKUP_STARTED)

    events = engine.handle_command(
        game,
        command_type="DISCARD_HOLDING",
        payload={"pending_pickup_id": pending_pickup_id, "holding_id_to_discard": source_holding.holding_id},
        actor_game_player_id=owner,
        expected_version=None,
        now=now(),
    )

    assert game.pending_market_correction is None
    resolved = next(e for e in events if e.type is EventType.MARKET_CORRECTION_RESOLVED)
    assert resolved.payload["reason"] == MarketCorrectionResolutionReason.INVALIDATED.value


# --------------------------------------------------------------------------
# Trigger execution: self-invalidation regression, no spurious support-marker
# source events, event visibility
# --------------------------------------------------------------------------


def test_trigger_executes_both_legs_and_resolves_triggered_not_invalidated():
    """The self-invalidation regression, explicitly requested on review:
    without the clear-before-execute fix, the first leg's own success
    makes _execute_swap's invalidation scan see the correction as
    'crossed' before the second leg ever runs."""
    game, host, _guest = _make_construction_succeeding_game()
    correction = _offer_now(game)
    positions_before = {m.swap.entity_a: game.market[m.swap.entity_a].position for m in correction.moves}
    destinations_before = {m.swap.entity_b: game.market[m.swap.entity_b].position for m in correction.moves}

    events = _trigger(game, host, correction.correction_id)

    assert game.pending_market_correction is None
    swap_events = [e for e in events if e.type is EventType.SWAP_EXECUTED]
    resolved_events = [e for e in events if e.type is EventType.MARKET_CORRECTION_RESOLVED]
    assert len(swap_events) == 2
    assert len(resolved_events) == 1
    assert resolved_events[0].payload["reason"] == MarketCorrectionResolutionReason.TRIGGERED.value
    # No PROPOSAL_RESOLVED/POOL_RESOLVED ever fires for a correction's own
    # moves -- the exact thing that keeps computeSupportMarkers from
    # ever crediting them as negotiated support.
    assert not any(e.type in (EventType.PROPOSAL_RESOLVED, EventType.POOL_RESOLVED) for e in events)

    for move in correction.moves:
        source, destination = move.swap.entity_a, move.swap.entity_b
        assert game.market[source].position == destinations_before[destination]
        assert game.market[destination].position == positions_before[source]


def test_trigger_rejects_a_wrong_or_missing_correction_id():
    game, host, guest = _make_construction_succeeding_game()
    correction = _offer_now(game)
    with pytest.raises(Exception):
        _trigger(game, host, "not-the-real-id")
    assert game.pending_market_correction is not None
    assert game.pending_market_correction.correction_id == correction.correction_id


def test_either_player_may_trigger_with_no_veto():
    game, host, guest = _make_construction_succeeding_game()
    correction = _offer_now(game)
    move = correction.moves[0]
    triggerer = guest if move.target_player_id == host else host
    events = _trigger(game, triggerer, correction.correction_id)
    assert any(e.type is EventType.MARKET_CORRECTION_RESOLVED and e.payload["reason"] == "triggered" for e in events)


# --------------------------------------------------------------------------
# Visibility: minimal live pending block, moves redacted unless triggered
# --------------------------------------------------------------------------


def test_pending_correction_exposes_only_id_and_expiry_never_moves():
    game, host, guest = _make_construction_succeeding_game()
    _offer_now(game)
    view = project(game, PublicAudience())
    pending = view["pending_market_correction"]
    assert set(pending.keys()) == {"correction_id", "expires_at"}


def test_resolved_moves_are_redacted_live_unless_triggered():
    game, host, guest = _make_construction_succeeding_game()
    correction = _offer_now(game)

    expired_event = engine._resolve_market_correction(game, correction, MarketCorrectionResolutionReason.EXPIRED, None, now())
    public_views = project_events(game, [expired_event], PublicAudience())
    assert "moves" not in public_views[0]["payload"]
    replay_views = project_events(game, [expired_event], ReplayAudience())
    assert "moves" in replay_views[0]["payload"]

    game.pending_market_correction = correction
    triggered_event = engine._resolve_market_correction(game, correction, MarketCorrectionResolutionReason.TRIGGERED, host, now())
    triggered_public_views = project_events(game, [triggered_event], PublicAudience())
    assert "moves" in triggered_public_views[0]["payload"]
