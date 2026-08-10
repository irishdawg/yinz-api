"""The Agency Principle: a player pays for commitments they voluntarily make
or end; they aren't charged when another player's independent action removes
the opportunity. This is the trickiest piece of the whole engine — see
domain model §01's worked example (Tedy/Mortia/Hanky/Josiah)."""

from __future__ import annotations

from gotiate.domain import engine
from tests.conftest import make_started_game, now


def _setup_competing_pools(game):
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())

    engine.handle_command(
        game,
        command_type="PROPOSE_SWAP",
        payload={"entity_a": entities[0], "entity_b": entities[1]},
        actor_game_player_id=tedy,
        expected_version=None,
        now=now(),
    )
    proposal_id = next(iter(game.proposals))

    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entities[2], "entity_d": entities[3], "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entities[4], "entity_d": entities[5], "visibility": "public"},
        actor_game_player_id=hanky,
        expected_version=None,
        now=now(),
    )
    hanky_pool_id = next(pid for pid, p in game.pools.items() if p.swap.initiator_player_id == hanky)
    return tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id


def test_sibling_pool_preempted_by_other_player_is_refunded():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id = _setup_competing_pools(game)
    starting = game.config.starting_influence

    # Josiah accepts Hanky's public pool — Mortia had no say in it.
    engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": hanky_pool_id}, actor_game_player_id=josiah, expected_version=None, now=now()
    )

    mortia_pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == mortia)
    assert mortia_pool.resolution_reason.value == "preempted_by_other_action"
    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_committed == 0
    assert mortia_player.influence_spent == 0
    assert mortia_player.influence_available == starting  # refunded in full


def test_sibling_pool_invalidated_by_own_initiator_action_is_spent():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id = _setup_competing_pools(game)
    starting = game.config.starting_influence

    # Mortia herself accepts Hanky's competing public pool — her own choice
    # abandons her own pool, which is economically a self-withdrawal.
    engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": hanky_pool_id}, actor_game_player_id=mortia, expected_version=None, now=now()
    )

    mortia_pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == mortia)
    assert mortia_pool.resolution_reason.value == "invalidated_by_initiator_action"
    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_committed == 0
    assert mortia_player.influence_spent == 1
    assert mortia_player.influence_available == starting - 1  # never refunded


def test_accepting_base_proposal_directly_preempts_sibling_pools():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id = _setup_competing_pools(game)
    starting = game.config.starting_influence

    # Josiah just accepts Tedy's bare proposal, bypassing both pools entirely.
    engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=josiah, expected_version=None, now=now()
    )

    for player_id in (mortia, hanky):
        pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == player_id)
        assert pool.resolution_reason.value == "preempted_by_other_action"
        player = game.player_by_id(player_id)
        assert player.influence_available == starting
        assert player.influence_committed == 0
