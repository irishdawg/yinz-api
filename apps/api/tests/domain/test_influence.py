"""Private Influence economy: bare-proposal mechanics. A player's liability
for a proposal is 1 iff they own the swap's rising entity, locked at the
moment they act (propose/accept), never recomputed later. See the plan's
design writeup."""

from __future__ import annotations

from gotiate.domain import engine
from tests.conftest import find_swap_pair, later, make_started_game, now


def _propose(game, actor, a, b):
    return engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": a, "entity_b": b}, actor_game_player_id=actor, expected_version=None, now=now()
    )


def test_propose_commits_when_the_proposer_owns_the_rising_entity():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    entity_a, entity_b = find_swap_pair(game, tedy, owned_should_rise=True)
    before = game.player_by_id(tedy).influence_available

    _propose(game, tedy, entity_a, entity_b)

    player = game.player_by_id(tedy)
    assert player.influence_available == before - 1
    assert player.influence_committed == 1
    assert player.influence_spent == 0
    assert game.proposals[next(iter(game.proposals))].initiator_influence_liability == 1


def test_propose_is_free_when_the_proposer_does_not_own_the_rising_entity():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    entity_a, entity_b = find_swap_pair(game, tedy, owned_should_rise=False)
    before = game.player_by_id(tedy)
    before_available, before_committed, before_spent = before.influence_available, before.influence_committed, before.influence_spent

    _propose(game, tedy, entity_a, entity_b)

    player = game.player_by_id(tedy)
    assert player.influence_available == before_available
    assert player.influence_committed == before_committed
    assert player.influence_spent == before_spent
    assert game.proposals[next(iter(game.proposals))].initiator_influence_liability == 0


def test_propose_with_zero_liability_succeeds_even_with_no_influence_available():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    entity_a, entity_b = find_swap_pair(game, tedy, owned_should_rise=False)
    game.player_by_id(tedy).influence_available = 0

    _propose(game, tedy, entity_a, entity_b)  # must not raise

    proposal = game.proposals[next(iter(game.proposals))]
    assert proposal.initiator_influence_liability == 0


def test_propose_with_one_liability_rejected_when_unaffordable():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    entity_a, entity_b = find_swap_pair(game, tedy, owned_should_rise=True)
    game.player_by_id(tedy).influence_available = 0

    try:
        _propose(game, tedy, entity_a, entity_b)
        raised = False
    except Exception:
        raised = True
    assert raised
    assert len(game.proposals) == 0


def test_withdraw_refunds_committed_liability():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    entity_a, entity_b = find_swap_pair(game, tedy, owned_should_rise=True)
    before = game.player_by_id(tedy).influence_available
    _propose(game, tedy, entity_a, entity_b)
    proposal_id = next(iter(game.proposals))

    engine.handle_command(
        game, command_type="WITHDRAW_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    player = game.player_by_id(tedy)
    assert player.influence_spent == 0
    assert player.influence_committed == 0
    assert player.influence_available == before
    proposal = game.proposals[proposal_id]
    assert proposal.resolution_reason.value == "withdrawn_by_initiator"


def test_accept_charges_the_accepter_fresh_and_settles_the_proposer():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    entity_a, entity_b = find_swap_pair(game, tedy, owned_should_rise=True)
    tedy_before = game.player_by_id(tedy).influence_available
    _propose(game, tedy, entity_a, entity_b)
    proposal_id = next(iter(game.proposals))

    # Direction may have already been decided by which of entity_a/entity_b
    # Mortia happens to own -- just record her state before accepting and
    # confirm the delta matches whatever _liability_for would say.
    mortia_owns_rising = any(
        h.owner_player_id == mortia and h.zone.value == "portfolio" and h.entity_id == (entity_a if game.market[entity_a].position > game.market[entity_b].position else entity_b)
        for h in game.holdings.values()
    )
    mortia_before = game.player_by_id(mortia).influence_available

    engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=mortia, expected_version=None, now=later()
    )

    tedy_player = game.player_by_id(tedy)
    assert tedy_player.influence_committed == 0
    assert tedy_player.influence_spent == 1
    assert tedy_player.influence_available == tedy_before - 1

    mortia_player = game.player_by_id(mortia)
    expected_mortia_spent = 1 if mortia_owns_rising else 0
    assert mortia_player.influence_spent == expected_mortia_spent
    assert mortia_player.influence_available == mortia_before - expected_mortia_spent
    assert mortia_player.influence_committed == 0


def test_declined_private_pool_is_refunded_not_spent():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    base_a, base_b = entities[0], entities[1]
    _propose(game, tedy, base_a, base_b)
    proposal_id = next(iter(game.proposals))
    starting = game.config.starting_influence

    pool_c, pool_d = find_swap_pair(game, mortia, owned_should_rise=True, exclude=frozenset({base_a, base_b}))
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

    engine.handle_command(
        game, command_type="DECLINE_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    mortia_player = game.player_by_id(mortia)
    assert mortia_player.influence_committed == 0
    assert mortia_player.influence_spent == 0
    assert mortia_player.influence_available == starting
    assert game.pools[pool_id].resolution_reason.value == "declined_by_target"
