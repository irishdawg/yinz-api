from __future__ import annotations

from gotiate.domain import engine
from tests.conftest import make_started_game, now


def _propose(game, actor, a, b):
    return engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": a, "entity_b": b}, actor_game_player_id=actor, expected_version=None, now=now()
    )


def test_propose_spends_immediately_no_committed_state():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    entities = list(game.market.keys())
    before = game.player_by_id(tedy).influence_available

    _propose(game, tedy, entities[0], entities[1])

    player = game.player_by_id(tedy)
    assert player.influence_available == before - 1
    assert player.influence_committed == 0
    assert player.influence_spent == 1


def test_withdraw_does_not_refund_already_spent_influence():
    game = make_started_game(2)
    tedy = game.players[0].game_player_id
    entities = list(game.market.keys())
    _propose(game, tedy, entities[0], entities[1])
    proposal_id = next(iter(game.proposals))

    engine.handle_command(
        game, command_type="WITHDRAW_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=tedy, expected_version=None, now=now()
    )

    player = game.player_by_id(tedy)
    assert player.influence_spent == 1
    assert player.influence_available == game.config.starting_influence - 1
    proposal = game.proposals[proposal_id]
    assert proposal.resolution_reason.value == "withdrawn_by_initiator"


def test_declined_private_pool_is_refunded_not_spent():
    game = make_started_game(2)
    tedy, mortia = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())
    _propose(game, tedy, entities[0], entities[1])
    proposal_id = next(iter(game.proposals))

    engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entities[2], "entity_d": entities[3], "visibility": "private"},
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
    assert mortia_player.influence_available == game.config.starting_influence
    assert game.pools[pool_id].resolution_reason.value == "declined_by_target"
