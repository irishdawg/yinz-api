"""Force Swap durability (real-play feedback: Force Swap felt underpowered
-- a Boost could be undone for the price of a mere Move via a negotiated
reverse). The pair most recently Force Swapped is locked against a direct,
Move-only reverse (PROPOSE_SWAP/CREATE_POOL naming exactly that pair) until
either another Force Swap happens anywhere (a Boost undoing a Boost is
fair, same-cost play -- never blocked) or either protected entity actually
moves again through an executed proposal/pool."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.errors import IllegalCommandError
from gotiate.domain.projections import PlayerAudience, project
from tests.conftest import later, make_started_game, now


def _force_swap(game, actor, entity_a, entity_b, at=None):
    return engine.handle_command(
        game,
        command_type="USE_BOOST",
        payload={"boost_type": "force_swap", "entity_a": entity_a, "entity_b": entity_b},
        actor_game_player_id=actor,
        expected_version=None,
        now=at or later(),
    )


def _propose(game, actor, entity_a, entity_b, at=None):
    return engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": entity_a, "entity_b": entity_b}, actor_game_player_id=actor, expected_version=None, now=at or now()
    )


def test_force_swap_sets_the_protected_pair():
    game = make_started_game(3)
    tedy = game.players[0].game_player_id
    e = list(game.market.keys())
    _force_swap(game, tedy, e[0], e[1])

    assert game.protected_pair is not None
    assert {game.protected_pair.entity_a, game.protected_pair.entity_b} == {e[0], e[1]}


def test_direct_reverse_via_propose_swap_is_rejected():
    game = make_started_game(3)
    tedy, mortia = game.players[0].game_player_id, game.players[1].game_player_id
    e = list(game.market.keys())
    _force_swap(game, tedy, e[0], e[1])

    with pytest.raises(IllegalCommandError, match="locked against a direct reverse"):
        _propose(game, mortia, e[0], e[1])
    # Order-independence -- naming them the other way around is equally blocked.
    with pytest.raises(IllegalCommandError, match="locked against a direct reverse"):
        _propose(game, mortia, e[1], e[0])


def test_direct_reverse_via_create_pool_is_rejected():
    game = make_started_game(3)
    tedy, mortia, hanky = [p.game_player_id for p in game.players]
    e = list(game.market.keys())
    _force_swap(game, tedy, e[2], e[3])

    proposal_id = next(
        ev.payload["proposal_id"]
        for ev in _propose(game, mortia, e[0], e[1])
        if ev.type.value == "PROPOSAL_CREATED"
    )
    with pytest.raises(IllegalCommandError, match="locked against a direct reverse"):
        engine.handle_command(
            game,
            command_type="CREATE_POOL",
            payload={"proposal_id": proposal_id, "entity_c": e[2], "entity_d": e[3], "visibility": "private"},
            actor_game_player_id=hanky,
            expected_version=None,
            now=later(),
        )


def test_a_boost_can_undo_a_boost():
    game = make_started_game(3)
    tedy, mortia = game.players[0].game_player_id, game.players[1].game_player_id
    e = list(game.market.keys())
    _force_swap(game, tedy, e[0], e[1])
    pos_a_after_first, pos_b_after_first = game.market[e[0]].position, game.market[e[1]].position

    # Force Swap is never subject to the protected-reversal block -- another
    # player can immediately Force Swap the exact same pair right back.
    events = _force_swap(game, mortia, e[0], e[1])
    assert any(ev.type.value == "BOOST_FORCE_SWAP_USED" for ev in events)
    assert game.market[e[0]].position == pos_b_after_first
    assert game.market[e[1]].position == pos_a_after_first
    # Still protected -- now representing the new state.
    assert game.protected_pair is not None
    assert {game.protected_pair.entity_a, game.protected_pair.entity_b} == {e[0], e[1]}


def test_a_new_force_swap_displaces_the_old_protection():
    game = make_started_game(4)
    tedy = game.players[0].game_player_id
    e = list(game.market.keys())
    _force_swap(game, tedy, e[0], e[1])
    assert {game.protected_pair.entity_a, game.protected_pair.entity_b} == {e[0], e[1]}

    _force_swap(game, tedy, e[2], e[3])
    assert {game.protected_pair.entity_a, game.protected_pair.entity_b} == {e[2], e[3]}

    # The OLD pair is no longer protected -- proposing it now is legal.
    mortia = game.players[1].game_player_id
    events = _propose(game, mortia, e[0], e[1])
    assert any(ev.type.value == "PROPOSAL_CREATED" for ev in events)


def test_protection_clears_when_a_protected_entity_moves_via_a_negotiated_deal():
    game = make_started_game(4)
    tedy, mortia, hanky = [p.game_player_id for p in game.players[:3]]
    e = list(game.market.keys())
    _force_swap(game, tedy, e[0], e[1])
    assert game.protected_pair is not None

    # A negotiated deal moving e[0] against a THIRD entity (e[2]) -- legal
    # (only the exact e[0]/e[1] pair is blocked), and clears the lock.
    propose_events = _propose(game, mortia, e[0], e[2])
    proposal_id = next(ev.payload["proposal_id"] for ev in propose_events if ev.type.value == "PROPOSAL_CREATED")
    engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=hanky, expected_version=None, now=later(2)
    )

    assert game.protected_pair is None
    # The original pair is proposable again now that the lock is gone.
    events = _propose(game, mortia, e[0], e[1], at=later(3))
    assert any(ev.type.value == "PROPOSAL_CREATED" for ev in events)


def test_protection_survives_an_unrelated_negotiated_deal():
    game = make_started_game(4)
    tedy, mortia, hanky = [p.game_player_id for p in game.players[:3]]
    e = list(game.market.keys())
    _force_swap(game, tedy, e[0], e[1])

    # A negotiated deal touching neither protected entity must not clear it.
    propose_events = _propose(game, mortia, e[2], e[3])
    proposal_id = next(ev.payload["proposal_id"] for ev in propose_events if ev.type.value == "PROPOSAL_CREATED")
    engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=hanky, expected_version=None, now=later(2)
    )

    assert game.protected_pair is not None
    assert {game.protected_pair.entity_a, game.protected_pair.entity_b} == {e[0], e[1]}


def test_protected_pair_is_public_and_unconditional():
    game = make_started_game(3)
    tedy, mortia = game.players[0].game_player_id, game.players[1].game_player_id
    e = list(game.market.keys())
    _force_swap(game, tedy, e[0], e[1])

    view = project(game, PlayerAudience(mortia))
    assert view["protected_pair"] is not None
    assert {view["protected_pair"]["entity_a"], view["protected_pair"]["entity_b"]} == {e[0], e[1]}

    game.protected_pair = None
    view_after = project(game, PlayerAudience(mortia))
    assert view_after["protected_pair"] is None
