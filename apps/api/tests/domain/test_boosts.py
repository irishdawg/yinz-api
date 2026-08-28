"""Boosts (cadence/economy redesign, checkpoint 4): Concentrate (duplicate
an owned entity up to the x3 cap), Force Swap (an unlimited unilateral
market move), and Draw/Refresh (a private, timed decision revealing one
uniformly-random entity the player owns zero copies of, locked at
initiation and never recomputed, spent regardless of the eventual
outcome). All three require boosts_remaining >= 1, not boosts_expired,
and no active Arbitration on the table's one negotiation."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import GamePhase, Holding, HoldingZone
from gotiate.domain.errors import IllegalCommandError, StaleVersionError
from gotiate.domain.projections import PlayerAudience, PublicAudience, project, project_events
from tests.conftest import _owns, later, make_started_game, now


def _use_boost(game, actor, payload, at=None):
    return engine.handle_command(
        game, command_type="USE_BOOST", payload=payload, actor_game_player_id=actor, expected_version=None, now=at or later()
    )


def _resolve_draw(game, actor, pending_id, discard_id, at=None, expected_version=None):
    return engine.handle_command(
        game,
        command_type="RESOLVE_BOOST_DRAW",
        payload={"pending_boost_draw_id": pending_id, "holding_id_to_discard": discard_id},
        actor_game_player_id=actor,
        expected_version=expected_version,
        now=at or later(),
    )


def _decline_draw(game, actor, pending_id, at=None, expected_version=None):
    return engine.handle_command(
        game,
        command_type="DECLINE_BOOST_DRAW",
        payload={"pending_boost_draw_id": pending_id},
        actor_game_player_id=actor,
        expected_version=expected_version,
        now=at or later(),
    )


def _portfolio_holdings(game, player_id):
    return [h for h in game.holdings.values() if h.owner_player_id == player_id and h.zone == HoldingZone.PORTFOLIO]


def _own_count(game, player_id, entity_id):
    return sum(1 for h in _portfolio_holdings(game, player_id) if h.entity_id == entity_id)


# --------------------------------------------------------------------------
# Concentrate
# --------------------------------------------------------------------------


def test_concentrate_duplicates_owned_entity_and_spends_boost():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    holdings = _portfolio_holdings(game, tedy)
    keep, discard = holdings[0], holdings[1]
    boosts_before = game.players[0].boosts_remaining

    events = _use_boost(
        game, tedy, {"boost_type": "concentrate", "holding_id_to_discard": discard.holding_id, "entity_id_to_duplicate": keep.entity_id}
    )

    assert game.players[0].boosts_remaining == boosts_before - 1
    assert game.holdings[discard.holding_id].zone == HoldingZone.DISCARDED
    assert _own_count(game, tedy, keep.entity_id) >= 2
    assert any(e.type.value == "BOOST_CONCENTRATE_USED" for e in events)


def test_concentrate_blocked_over_cap():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    target_entity = next(iter(game.market))
    other_entity = next(e for e in game.market if e != target_entity)

    # Manufacture 3 copies (the cap) directly -- deterministic, doesn't
    # depend on the random starting deal producing this shape naturally.
    game.holdings.clear()
    for _ in range(3):
        h = Holding(holding_id=engine.new_id(), entity_id=target_entity, owner_player_id=tedy, zone=HoldingZone.PORTFOLIO)
        game.holdings[h.holding_id] = h
    spare = Holding(holding_id=engine.new_id(), entity_id=other_entity, owner_player_id=tedy, zone=HoldingZone.PORTFOLIO)
    game.holdings[spare.holding_id] = spare

    with pytest.raises(IllegalCommandError):
        _use_boost(game, tedy, {"boost_type": "concentrate", "holding_id_to_discard": spare.holding_id, "entity_id_to_duplicate": target_entity})


def test_concentrate_discard_and_duplicate_same_entity_nets_no_change_and_is_legal():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    target_entity = next(iter(game.market))
    game.holdings.clear()
    for _ in range(3):  # already at the cap
        h = Holding(holding_id=engine.new_id(), entity_id=target_entity, owner_player_id=tedy, zone=HoldingZone.PORTFOLIO)
        game.holdings[h.holding_id] = h
    discard_id = next(iter(game.holdings))

    # Discarding and duplicating the SAME entity nets to no change in
    # copy count -- not a cap violation just because it names the same
    # entity on both sides of the payload.
    _use_boost(game, tedy, {"boost_type": "concentrate", "holding_id_to_discard": discard_id, "entity_id_to_duplicate": target_entity})
    assert _own_count(game, tedy, target_entity) == 3


def test_concentrate_blocked_if_discard_not_owned_by_actor():
    game = make_started_game(3)
    tedy, mortia = game.players[0].game_player_id, game.players[1].game_player_id
    mortia_holding = _portfolio_holdings(game, mortia)[0]

    with pytest.raises(IllegalCommandError):
        _use_boost(
            game, tedy, {"boost_type": "concentrate", "holding_id_to_discard": mortia_holding.holding_id, "entity_id_to_duplicate": mortia_holding.entity_id}
        )


def test_concentrate_blocked_if_duplicate_entity_not_owned():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    discard = _portfolio_holdings(game, tedy)[0]
    unowned_entity = next(e for e in game.market if not _owns(game, tedy, e))

    with pytest.raises(IllegalCommandError):
        _use_boost(game, tedy, {"boost_type": "concentrate", "holding_id_to_discard": discard.holding_id, "entity_id_to_duplicate": unowned_entity})


# --------------------------------------------------------------------------
# Force Swap
# --------------------------------------------------------------------------


def test_force_swap_executes_and_spends_boost():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    e = list(game.market.keys())
    pos_a, pos_b = game.market[e[0]].position, game.market[e[1]].position
    boosts_before = game.players[0].boosts_remaining

    events = _use_boost(game, tedy, {"boost_type": "force_swap", "entity_a": e[0], "entity_b": e[1]})

    assert game.players[0].boosts_remaining == boosts_before - 1
    assert game.market[e[0]].position == pos_b
    assert game.market[e[1]].position == pos_a
    assert any(e.type.value == "SWAP_EXECUTED" for e in events)
    assert any(e.type.value == "BOOST_FORCE_SWAP_USED" for e in events)


def test_force_swap_triggers_crossing_invalidation():
    game = make_started_game(3)
    tedy, mortia = game.players[0].game_player_id, game.players[1].game_player_id
    e = list(game.market.keys())

    # mortia opens a negotiation locked on e[0]/e[1]'s current direction.
    proposal_events = engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": e[0], "entity_b": e[1]}, actor_game_player_id=mortia, expected_version=None, now=now()
    )
    proposal_id = next(ev.payload["proposal_id"] for ev in proposal_events if ev.type.value == "PROPOSAL_CREATED")

    # tedy force-swaps the exact same pair, reversing direction underneath it.
    _use_boost(game, tedy, {"boost_type": "force_swap", "entity_a": e[0], "entity_b": e[1]})

    assert game.proposals[proposal_id].resolution_reason.value == "voided_market_swung"


def test_force_swap_blocked_without_boosts_remaining():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    game.players[0].boosts_remaining = 0
    e = list(game.market.keys())

    with pytest.raises(IllegalCommandError):
        _use_boost(game, tedy, {"boost_type": "force_swap", "entity_a": e[0], "entity_b": e[1]})


# --------------------------------------------------------------------------
# Shared legality gates
# --------------------------------------------------------------------------


def test_boosts_blocked_once_expired():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    game.boosts_expired = True
    e = list(game.market.keys())

    with pytest.raises(IllegalCommandError):
        _use_boost(game, tedy, {"boost_type": "force_swap", "entity_a": e[0], "entity_b": e[1]})


def test_boosts_blocked_during_active_arbitration():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_events = engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": e[0], "entity_b": e[1]}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    proposal_id = next(ev.payload["proposal_id"] for ev in proposal_events if ev.type.value == "PROPOSAL_CREATED")
    engine.handle_command(
        game, command_type="PASS_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=mortia, expected_version=None, now=later()
    )
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": e[2], "entity_d": e[3], "visibility": "public"},
        actor_game_player_id=hanky,
        expected_version=None,
        now=later(),
    )
    engine.handle_command(
        game, command_type="CALL_ARBITRATION", payload={}, actor_game_player_id=hanky, expected_version=None, now=later(2)
    )

    with pytest.raises(IllegalCommandError):
        _use_boost(game, hanky, {"boost_type": "force_swap", "entity_a": e[2], "entity_b": e[3]}, at=later(3))


# --------------------------------------------------------------------------
# Draw / Refresh
# --------------------------------------------------------------------------


def test_draw_refresh_reveals_zero_owned_entity_and_spends_boost_immediately():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    boosts_before = game.players[0].boosts_remaining

    events = _use_boost(game, tedy, {"boost_type": "draw"})

    assert game.players[0].boosts_remaining == boosts_before - 1  # spent immediately, decision still open
    pending = game.players[0].pending_boost_draw
    assert pending is not None
    assert not _owns(game, tedy, pending.revealed_entity_id)
    assert any(e.type.value == "BOOST_DRAW_STARTED" for e in events)


def test_draw_refresh_frozen_view_served_while_pending():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    _use_boost(game, tedy, {"boost_type": "draw"})
    pending = game.players[0].pending_boost_draw

    view_before = project(game, PlayerAudience(tedy))
    # Mutate live state after the snapshot -- e.g. another player's Move
    # consumption -- the frozen view must not reflect it.
    game.players[1].moves_remaining -= 1
    view_after = project(game, PlayerAudience(tedy))

    assert view_before == view_after
    assert view_after["pending_boost_draw"]["pending_boost_draw_id"] == pending.pending_boost_draw_id
    assert view_after["pending_boost_draw"]["revealed_entity_id"] == pending.revealed_entity_id

    # A different player's own view is entirely unaffected.
    mortia = game.players[1].game_player_id
    other_view = project(game, PlayerAudience(mortia))
    assert "pending_boost_draw" not in other_view or other_view.get("pending_boost_draw") != view_after.get("pending_boost_draw")


def test_draw_refresh_resolve_completes_and_adds_entity():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    _use_boost(game, tedy, {"boost_type": "draw"})
    pending = game.players[0].pending_boost_draw
    discard_id = pending.original_portfolio_holding_ids[0]

    events = _resolve_draw(game, tedy, pending.pending_boost_draw_id, discard_id)

    assert game.players[0].pending_boost_draw is None
    assert game.holdings[discard_id].zone == HoldingZone.DISCARDED
    assert _own_count(game, tedy, pending.revealed_entity_id) == 1
    assert any(e.type.value == "BOOST_DRAW_COMPLETED" for e in events)


def test_draw_refresh_decline_never_adds_entity_boost_still_spent():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    boosts_before = game.players[0].boosts_remaining
    _use_boost(game, tedy, {"boost_type": "draw"})
    pending = game.players[0].pending_boost_draw
    original_ids = set(pending.original_portfolio_holding_ids)

    events = _decline_draw(game, tedy, pending.pending_boost_draw_id)

    assert game.players[0].pending_boost_draw is None
    assert game.players[0].boosts_remaining == boosts_before - 1  # never refunded
    remaining_ids = {h.holding_id for h in _portfolio_holdings(game, tedy)}
    assert remaining_ids == original_ids  # untouched -- nothing discarded, nothing added
    assert any(e.type.value == "BOOST_DRAW_FAILED" and e.payload["reason"] == "declined_by_player" for e in events)


def test_draw_refresh_eligibility_locked_at_initiation_not_recomputed_after_discard():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    _use_boost(game, tedy, {"boost_type": "draw"})
    pending = game.players[0].pending_boost_draw
    revealed = pending.revealed_entity_id

    # Discard one of the original holdings -- if this happens to be the
    # player's only copy of some entity, that entity drops to zero-owned
    # for the FIRST TIME only as a side effect of this very discard. The
    # drawn entity must still be exactly what was locked in before any of
    # this happened, never silently redrawn against the post-discard set.
    discard_id = pending.original_portfolio_holding_ids[0]
    _resolve_draw(game, tedy, pending.pending_boost_draw_id, discard_id)

    added = [h for h in _portfolio_holdings(game, tedy) if h.entity_id == revealed]
    assert len(added) == 1


def test_draw_refresh_resolve_rejects_discard_outside_original_set():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    _use_boost(game, tedy, {"boost_type": "draw"})
    pending = game.players[0].pending_boost_draw

    with pytest.raises(IllegalCommandError):
        _resolve_draw(game, tedy, pending.pending_boost_draw_id, "not-a-real-holding-id")


def test_draw_refresh_timeout_fails_without_refund():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    boosts_before = game.players[0].boosts_remaining
    _use_boost(game, tedy, {"boost_type": "draw"}, at=now())
    deadline = game.players[0].pending_boost_draw.decision_deadline_at

    # No explicit resolve/decline -- a later command from ANYONE runs
    # apply_due_time_transitions first, which should notice the deadline
    # has passed and force the timeout.
    engine.handle_command(
        game, command_type="SET_READY_TO_CLOSE", payload={"ready": False}, actor_game_player_id=game.players[1].game_player_id,
        expected_version=None, now=deadline + engine.timedelta(seconds=1),
    )

    assert game.players[0].pending_boost_draw is None
    assert game.players[0].boosts_remaining == boosts_before - 1


def test_draw_refresh_market_close_force_fails_pending():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    _use_boost(game, tedy, {"boost_type": "draw"})
    assert game.players[0].pending_boost_draw is not None

    for p in game.players:
        engine.handle_command(
            game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p.game_player_id, expected_version=None, now=later()
        )

    assert game.phase == GamePhase.SCORED
    assert game.players[0].pending_boost_draw is None


def test_pending_boost_draw_blocks_other_commands_for_that_player():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    _use_boost(game, tedy, {"boost_type": "draw"})
    e = list(game.market.keys())

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="PROPOSE_SWAP", payload={"entity_a": e[0], "entity_b": e[1]}, actor_game_player_id=tedy, expected_version=None, now=later()
        )
    with pytest.raises(IllegalCommandError):
        _use_boost(game, tedy, {"boost_type": "force_swap", "entity_a": e[2], "entity_b": e[3]})


def test_boost_draw_resolution_commands_are_version_exempt():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    _use_boost(game, tedy, {"boost_type": "draw"})
    pending = game.players[0].pending_boost_draw
    discard_id = pending.original_portfolio_holding_ids[0]

    # A deliberately stale expected_version must NOT raise
    # StaleVersionError -- the frozen client can't know the live version.
    _resolve_draw(game, tedy, pending.pending_boost_draw_id, discard_id, expected_version=game.version + 999)


def test_draw_refresh_no_eligible_entities_raises():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    # Manufacture ownership of every market entity so nothing is eligible.
    game.holdings.clear()
    for entity_id in game.market:
        h = Holding(holding_id=engine.new_id(), entity_id=entity_id, owner_player_id=tedy, zone=HoldingZone.PORTFOLIO)
        game.holdings[h.holding_id] = h

    with pytest.raises(IllegalCommandError):
        _use_boost(game, tedy, {"boost_type": "draw"})


# --------------------------------------------------------------------------
# Visibility
# --------------------------------------------------------------------------


def test_boost_events_actor_only_live():
    game = make_started_game(3)
    tedy, mortia = game.players[0].game_player_id, game.players[1].game_player_id
    holdings = _portfolio_holdings(game, tedy)
    events = _use_boost(
        game, tedy, {"boost_type": "concentrate", "holding_id_to_discard": holdings[1].holding_id, "entity_id_to_duplicate": holdings[0].entity_id}
    )

    actor_views = project_events(game, events, PlayerAudience(tedy))
    assert any(v["type"] == "BOOST_CONCENTRATE_USED" for v in actor_views)

    other_views = project_events(game, events, PlayerAudience(mortia))
    assert not any(v["type"] == "BOOST_CONCENTRATE_USED" for v in other_views)

    public_views = project_events(game, events, PublicAudience())
    assert not any(v["type"] == "BOOST_CONCENTRATE_USED" for v in public_views)
