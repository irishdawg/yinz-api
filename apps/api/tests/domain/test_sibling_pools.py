"""The Agency Principle: a player pays for commitments they voluntarily make
or end; they aren't charged when another player's independent action removes
the opportunity. This is the trickiest piece of the whole engine — see
domain model §01's worked example (Tedy/Mortia/Hanky/Josiah)."""

from __future__ import annotations

from gotiate.domain import engine
from gotiate.domain.entities import HoldingZone
from tests.conftest import find_swap_pair, later, make_started_game, now


def _owns(game, player_id, entity_id):
    return any(h.owner_player_id == player_id and h.zone == HoldingZone.PORTFOLIO and h.entity_id == entity_id for h in game.holdings.values())


def _rising(game, entity_a, entity_b):
    return entity_a if game.market[entity_a].position > game.market[entity_b].position else entity_b


def _fresh_liability(game, player_id, swap_entity_a, swap_entity_b):
    return 1 if _owns(game, player_id, _rising(game, swap_entity_a, swap_entity_b)) else 0


def _setup_competing_pools(game):
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]

    # not_owned_by=mortia on the base proposal and Hanky's pool: it's not
    # a hard guarantee (entities can be dealt to more than one player), so
    # tests that need Mortia's fresh accept-time charge to be exactly 0
    # recompute it dynamically instead of assuming isolation.
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

    # Both siblings' liability is pinned to 1 (owned_should_rise=True) --
    # these tests are specifically about the spent-vs-refunded distinction,
    # which is only observable when there's something committed to settle.
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


def test_sibling_pool_preempted_by_other_player_is_refunded():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id, base_a, base_b, hanky_c, hanky_d = _setup_competing_pools(game)
    starting = game.config.starting_influence

    # Josiah accepts Hanky's public pool — Mortia had no say in it.
    engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": hanky_pool_id}, actor_game_player_id=josiah, expected_version=None, now=later()
    )

    mortia_pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == mortia)
    assert mortia_pool.resolution_reason.value == "preempted_by_other_action"
    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_committed == 0
    assert mortia_player.influence_spent == 0
    assert mortia_player.influence_available == starting  # refunded in full


def test_sibling_pool_invalidated_by_own_initiator_action_is_spent():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id, base_a, base_b, hanky_c, hanky_d = _setup_competing_pools(game)
    starting = game.config.starting_influence
    # Accepting Hanky's pool is its own affirmative action for Mortia, with
    # its own fresh liability (usually 0 here since _setup_competing_pools
    # asks for base_a/b and hanky_c/d not owned by Mortia, but that's a
    # preference, not a guarantee -- entities can be dealt to more than one
    # player, so compute what it actually is rather than assuming 0).
    mortia_accept_liability = max(_fresh_liability(game, mortia, base_a, base_b), _fresh_liability(game, mortia, hanky_c, hanky_d))

    # Mortia herself accepts Hanky's competing public pool — her own choice
    # abandons her own pool, which is economically a self-withdrawal.
    engine.handle_command(
        game, command_type="ACCEPT_POOL", payload={"pool_id": hanky_pool_id}, actor_game_player_id=mortia, expected_version=None, now=later()
    )

    mortia_pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == mortia)
    assert mortia_pool.resolution_reason.value == "invalidated_by_initiator_action"
    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_committed == 0
    # 1 from her own invalidated pool being settled as spent (never
    # refunded -- her own choice abandoned it), plus whatever her
    # independent fresh accept-time liability on Hanky's pool happens to be.
    assert mortia_player.influence_spent == 1 + mortia_accept_liability
    assert mortia_player.influence_available == starting - 1 - mortia_accept_liability


def test_accepting_base_proposal_directly_preempts_sibling_pools():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah, proposal_id, hanky_pool_id, base_a, base_b, hanky_c, hanky_d = _setup_competing_pools(game)
    starting = game.config.starting_influence

    # Josiah just accepts Tedy's bare proposal, bypassing both pools entirely.
    engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=josiah, expected_version=None, now=later()
    )

    for player_id in (mortia, hanky):
        pool = next(p for p in game.pools.values() if p.swap.initiator_player_id == player_id)
        assert pool.resolution_reason.value == "preempted_by_other_action"
        player = game.player_by_id(player_id)
        assert player.influence_available == starting
        assert player.influence_committed == 0
