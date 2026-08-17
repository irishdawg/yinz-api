"""Zero-Influence agency: a player at 0 available Influence can still
ACCEPT any deal (for free -- there's nothing left to charge), just never
originate one (PROPOSE_SWAP/CREATE_POOL still require affordability). And
the recovery mechanic: the instant every seated player hits 0 at once, the
whole table gets topped back up. See engine._handle_accept_proposal,
_handle_accept_pool, and _maybe_topup_zero_influence."""

from __future__ import annotations

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import GameConfig
from gotiate.domain.errors import IllegalCommandError
from tests.conftest import find_swap_pair, later, make_started_game, now


def _propose(game, actor, a, b):
    return engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": a, "entity_b": b}, actor_game_player_id=actor, expected_version=None, now=now()
    )


def test_zero_influence_player_cannot_originate_a_liability_one_proposal():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    game.player_by_id(tedy).influence_available = 0
    a, b = find_swap_pair(game, tedy, owned_should_rise=True)  # liability 1 for Tedy

    with pytest.raises(IllegalCommandError):
        _propose(game, tedy, a, b)


def test_zero_influence_player_can_accept_a_liability_one_proposal_for_free():
    game = make_started_game(3, config=GameConfig(starting_influence=1))
    tedy, mortia, _hanky = [p.game_player_id for p in game.players]

    # Mortia spends her one point of Influence originating her own
    # proposal -- now at 0 available, 1 committed.
    ma, mb = find_swap_pair(game, mortia, owned_should_rise=True)
    _propose(game, mortia, ma, mb)
    assert game.player_by_id(mortia).influence_available == 0

    # Tedy proposes a pair where Mortia (not Tedy) owns the rising entity --
    # accepting it would normally cost Mortia a point she doesn't have.
    ta, tb = find_swap_pair(game, mortia, owned_should_rise=True, exclude=frozenset({ma, mb}))
    _propose(game, tedy, ta, tb)
    proposal_id = next(p.proposal_id for p in game.proposals.values() if {p.swap.entity_a, p.swap.entity_b} == {ta, tb})

    events = engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=mortia, expected_version=None, now=later()
    )
    assert any(e.type.value == "SWAP_EXECUTED" for e in events)

    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_available == 0
    assert mortia_player.influence_spent == 0  # the accept was free -- nothing charged


def test_all_players_at_zero_influence_get_topped_up_and_emit_event():
    config = GameConfig(starting_influence=0)
    game, _ = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now(), config=config)
    engine.join_game(game, actor_auth_user_id="auth-1", display_name="Player 1", now=now())

    events = engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )

    assert all(p.influence_available == config.zero_influence_topup_amount for p in game.players)
    topup = next(e for e in events if e.type.value == "INFLUENCE_TOPPED_UP")
    assert topup.payload["amount"] == config.zero_influence_topup_amount


def test_topup_does_not_fire_while_any_player_still_has_influence():
    from gotiate.domain.engine import _maybe_topup_zero_influence

    game = make_started_game(2)
    game.players[0].influence_available = 0
    game.players[1].influence_available = 1

    assert _maybe_topup_zero_influence(game, now()) == []
    assert game.players[0].influence_available == 0
