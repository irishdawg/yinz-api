"""The Move economy (cadence/economy redesign, checkpoint 2): opening a
bare negotiation is the only thing that costs a Move, never refunded;
at most one negotiation is ever active table-wide; Boost expiry and the
Haircut reveal are Move-driven; the game can now end via Move exhaustion,
distinct from (and always later than) Boost expiry."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import CloseReason, GameConfig, GamePhase, ResolutionStatus
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import later, make_started_game, now


def _propose(game, actor, entity_a, entity_b):
    return engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": entity_a, "entity_b": entity_b}, actor_game_player_id=actor, expected_version=None, now=now()
    )


def _propose_id(game, actor, entity_a, entity_b):
    events = _propose(game, actor, entity_a, entity_b)
    return next(e.payload["proposal_id"] for e in events if e.type.value == "PROPOSAL_CREATED")


def _pass(game, proposal_id, actor):
    return engine.handle_command(
        game, command_type="PASS_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=actor, expected_version=None, now=now()
    )


def _accept(game, proposal_id, actor):
    return engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=actor, expected_version=None, now=later()
    )


# --------------------------------------------------------------------------
# Opening a negotiation consumes exactly one Move, never refunded
# --------------------------------------------------------------------------


def test_propose_swap_consumes_exactly_one_move():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    before = game.player_by_id(tedy).moves_remaining
    _propose(game, tedy, e[0], e[1])
    assert game.player_by_id(tedy).moves_remaining == before - 1
    # Accepting/passing/pooling never touch moves_remaining -- only opening does.
    assert game.player_by_id(mortia).moves_remaining == before


def test_move_is_never_refunded_regardless_of_resolution_reason():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    before = game.player_by_id(tedy).moves_remaining
    proposal_id = _propose_id(game, tedy, e[0], e[1])
    # Force-resolve via SET_READY_TO_CLOSE -> close_market(MARKET_CLOSED
    # equivalent path for the still-open proposal) -- a non-execution,
    # same as the redesign's other resolution reasons.
    for actor in (tedy, mortia):
        engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=actor, expected_version=None, now=now())
    assert game.phase == GamePhase.SCORED
    assert game.proposals[proposal_id].resolution_reason.value == "market_closed"
    assert game.player_by_id(tedy).moves_remaining == before - 1  # still spent


# --------------------------------------------------------------------------
# Exactly one active bare negotiation, table-wide
# --------------------------------------------------------------------------


def test_second_propose_swap_rejected_while_one_is_active():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    _propose(game, tedy, e[0], e[1])
    assert game.active_proposal_id is not None

    with pytest.raises(IllegalCommandError):
        _propose(game, mortia, e[2], e[3])
    # Not even the same actor can open a second one.
    with pytest.raises(IllegalCommandError):
        _propose(game, tedy, e[4], e[5])


def test_propose_swap_legal_again_once_the_active_negotiation_resolves():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    _propose_id(game, tedy, e[0], e[1])
    _pass(game, next(iter(game.proposals)), mortia)  # 2p: single pass auto-expires it
    assert game.active_proposal_id is None

    new_id = _propose_id(game, mortia, e[2], e[3])
    assert game.proposals[new_id].status == ResolutionStatus.OPEN
    assert game.active_proposal_id == new_id


def test_propose_swap_rejected_with_no_moves_remaining():
    game = make_started_game(2, config=GameConfig(starting_moves=1))
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose_id(game, tedy, e[0], e[1])
    assert game.player_by_id(tedy).moves_remaining == 0
    _pass(game, proposal_id, mortia)  # resolves it, frees active_proposal_id; costs mortia nothing
    assert game.active_proposal_id is None

    with pytest.raises(IllegalCommandError):
        _propose(game, tedy, e[2], e[3])  # tedy has no Moves left


def test_withdraw_proposal_is_not_a_recognized_command():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    e = list(game.market.keys())
    proposal_id = _propose_id(game, tedy, e[0], e[1])
    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="WITHDRAW_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=tedy, expected_version=None, now=now()
        )


# --------------------------------------------------------------------------
# active_proposal_id -- set on open, cleared on every terminal resolution
# --------------------------------------------------------------------------


def test_active_proposal_id_cleared_on_execution():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose_id(game, tedy, e[0], e[1])
    assert game.active_proposal_id == proposal_id
    _accept(game, proposal_id, mortia)
    assert game.active_proposal_id is None


def test_active_proposal_id_cleared_on_all_passed_expiry():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose_id(game, tedy, e[0], e[1])
    _pass(game, proposal_id, mortia)
    assert game.active_proposal_id is None


def test_active_proposal_id_cleared_on_market_closed():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    _propose_id(game, tedy, e[0], e[1])
    assert game.active_proposal_id is not None
    for actor in (tedy, mortia):
        engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=actor, expected_version=None, now=now())
    assert game.phase == GamePhase.SCORED
    assert game.active_proposal_id is None


# --------------------------------------------------------------------------
# Boost expiry -- the first player to hit zero, not everyone
# --------------------------------------------------------------------------


def test_boosts_expire_the_instant_the_first_player_hits_zero_moves():
    game = make_started_game(3, config=GameConfig(starting_moves=1))
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    assert game.boosts_expired is False

    events = _propose(game, tedy, e[0], e[1])
    assert game.player_by_id(tedy).moves_remaining == 0
    assert game.player_by_id(mortia).moves_remaining == 1
    assert game.player_by_id(hanky).moves_remaining == 1
    assert game.boosts_expired is True
    assert any(ev.type.value == "BOOSTS_EXPIRED" for ev in events)


def test_boosts_expired_does_not_refire():
    game = make_started_game(3, config=GameConfig(starting_moves=1))
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose_id(game, tedy, e[0], e[1])
    assert game.boosts_expired is True

    accept_events = _accept(game, proposal_id, mortia)
    assert not any(ev.type.value == "BOOSTS_EXPIRED" for ev in accept_events)

    second_events = _propose(game, mortia, e[2], e[3])
    assert game.player_by_id(mortia).moves_remaining == 0
    assert not any(ev.type.value == "BOOSTS_EXPIRED" for ev in second_events)  # already expired


# --------------------------------------------------------------------------
# Haircut reveal -- 50% of the initial total Move allocation, cumulative
# --------------------------------------------------------------------------


def test_haircut_reveals_once_cumulative_moves_cross_fifty_percent():
    game = make_started_game(2, config=GameConfig(starting_moves=2))  # total allocation = 4
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    assert game.haircut_profile_revealed_at is None

    proposal_id = _propose_id(game, tedy, e[0], e[1])  # 1/4 consumed = 25%
    assert game.haircut_profile_revealed_at is None

    _pass(game, proposal_id, mortia)  # costs no Moves
    assert game.haircut_profile_revealed_at is None

    events = _propose(game, mortia, e[2], e[3])  # 2/4 consumed = 50% -- reveals
    assert game.haircut_profile_revealed_at is not None
    assert any(ev.type.value == "HAIRCUT_RISK_REVEALED" for ev in events)


def test_haircut_reveal_does_not_refire():
    game = make_started_game(2, config=GameConfig(starting_moves=2))
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    proposal_id = _propose_id(game, tedy, e[0], e[1])
    _pass(game, proposal_id, mortia)
    new_id = _propose_id(game, mortia, e[2], e[3])
    revealed_at = game.haircut_profile_revealed_at
    assert revealed_at is not None

    more_events = _accept(game, new_id, tedy)
    assert not any(ev.type.value == "HAIRCUT_RISK_REVEALED" for ev in more_events)
    assert game.haircut_profile_revealed_at == revealed_at


# --------------------------------------------------------------------------
# Move-exhaustion endgame -- distinct from, and always later than,
# Boost expiry
# --------------------------------------------------------------------------


def test_game_does_not_close_while_the_final_negotiation_is_still_open():
    game = make_started_game(3, config=GameConfig(starting_moves=1))
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())

    p1 = _propose_id(game, tedy, e[0], e[1])  # tedy: 0
    _accept(game, p1, mortia)  # accepting costs no Moves -- mortia still: 1

    p2 = _propose_id(game, mortia, e[2], e[3])  # mortia: 0 (tedy: 0, hanky: 1 -- not all zero yet)
    assert game.phase == GamePhase.NEGOTIATION
    _accept(game, p2, hanky)  # hanky still: 1

    # hanky spends her own final Move -- ALL THREE now at zero, but the
    # negotiation she just opened hasn't resolved yet.
    p3 = _propose_id(game, hanky, e[4], e[5])
    assert all(p.moves_remaining == 0 for p in game.players)
    assert game.active_proposal_id == p3
    assert game.phase == GamePhase.NEGOTIATION  # must NOT have closed yet

    # Only once that final negotiation itself resolves does the game end --
    # a player with zero Moves can still Accept, per rule 1.
    events = _accept(game, p3, tedy)
    assert game.phase == GamePhase.SCORED
    assert game.close_reason == CloseReason.MOVES_EXHAUSTED
    assert any(ev.type.value == "GAME_ENDED" for ev in events)


def test_boosts_expire_strictly_before_moves_exhausted_closes_the_game():
    # boosts_expired fires on the FIRST player to hit zero; MOVES_EXHAUSTED
    # requires EVERY player at zero AND no active negotiation -- distinct,
    # never-simultaneous (for 2+ players) instants.
    game = make_started_game(2, config=GameConfig(starting_moves=1))
    tedy, mortia = [p.game_player_id for p in game.players]
    e = list(game.market.keys())

    proposal_id = _propose_id(game, tedy, e[0], e[1])  # tedy: 0 -- boosts expire now
    assert game.boosts_expired is True
    assert game.phase == GamePhase.NEGOTIATION  # mortia still has a Move -- not exhausted

    _accept(game, proposal_id, mortia)  # mortia still: 1, active cleared
    assert game.phase == GamePhase.NEGOTIATION

    events = _propose(game, mortia, e[2], e[3])  # mortia: 0 -- now both zero, negotiation open
    assert game.phase == GamePhase.NEGOTIATION  # still open, still not closed

    final_id = next(e2.payload["proposal_id"] for e2 in events if e2.type.value == "PROPOSAL_CREATED")
    close_events = _accept(game, final_id, tedy)
    assert game.phase == GamePhase.SCORED
    assert game.close_reason == CloseReason.MOVES_EXHAUSTED
