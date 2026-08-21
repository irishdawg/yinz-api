"""The Agency Principle: a player's own sibling Pool resolves differently
depending on whether THEY chose to abandon it (their own accept elsewhere)
or someone else's independent action removed the opportunity. See domain
model §01's worked example (Tedy/Mortia/Hanky/Josiah)."""

from __future__ import annotations

from gotiate.domain import engine
from tests.conftest import find_swap_pair, later, make_started_game, now


def _setup_competing_pools(game):
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]

    base_a, base_b = find_swap_pair(game, tedy, owned_should_rise=True, not_owned_by=mortia)
    engine.handle_command(
        game,
        command_type="PROPOSE_SWAP",
        payload={"entity_a": base_a, "entity_b": base_b},
        actor_game_player_id=tedy,
        expected_version=None,
        now=now(),
    )
    proposal_id = next(iter(game.proposals))

    mortia_c, mortia_d = find_swap_pair(game, mortia, owned_should_rise=True, exclude=frozenset({base_a, base_b}))
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": mortia_c, "entity_d": mortia_d, "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    hanky_c, hanky_d = find_swap_pair(
        game, hanky, owned_should_rise=True, exclude=frozenset({base_a, base_b, mortia_c, mortia_d}), not_owned_by=mortia
    )
    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": hanky_c, "entity_d": hanky_d, "visibility": "public"},
        actor_game_player_id=hanky,
        expected_version=None,
        now=now(),
    )
    hanky_pool_id = next(pid for pid, p in game.pools.items() if p.swap.initiator_player_id == hanky)
    return tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id, base_a, base_b, hanky_c, hanky_d


def test_sibling_pool_preempted_by_other_player():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id, base_a, base_b, hanky_c, hanky_d = _setup_competing_pools(game)

    # Josiah accepts Hanky's public pool — Mortia had no say in it.
    engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": hanky_pool_id}, actor_game_player_id=josiah, expected_version=None, now=later()
    )

    mortia_pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == mortia)
    assert mortia_pool.resolution_reason.value == "preempted_by_other_action"


def test_sibling_pool_invalidated_by_own_initiator_action():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id, base_a, base_b, hanky_c, hanky_d = _setup_competing_pools(game)

    # Mortia herself accepts Hanky's competing public pool — her own choice
    # abandons her own pool.
    engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": hanky_pool_id}, actor_game_player_id=mortia, expected_version=None, now=later()
    )

    mortia_pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == mortia)
    assert mortia_pool.resolution_reason.value == "invalidated_by_initiator_action"


def test_accepting_base_proposal_directly_preempts_sibling_pools():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id, base_a, base_b, hanky_c, hanky_d = _setup_competing_pools(game)

    # Josiah just accepts Tedy's bare proposal, bypassing both pools entirely.
    engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=josiah, expected_version=None, now=later()
    )

    for player_id in (mortia, hanky):
        pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == player_id)
        assert pool.resolution_reason.value == "preempted_by_other_action"
