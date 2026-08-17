"""A private Pool stays private forever unless it executes -- the instant
it does, its contents become public as part of that transaction, in both
the live projection and the event log. Declined/withdrawn/preempted
private Pools never reveal anything. See the plan's design writeup."""

from __future__ import annotations

from gotiate.domain import engine
from gotiate.domain.events import EventType
from gotiate.domain.projections import PlayerAudience, PublicAudience, project, project_events
from tests.conftest import later, make_started_game, now


def _propose_and_pool(game, proposer, pooler, *, visibility="private"):
    entities = list(game.market.keys())
    engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": entities[0], "entity_b": entities[1]}, actor_game_player_id=proposer, expected_version=None, now=now()
    )
    proposal_id = next(iter(game.proposals))
    pool_events = engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entities[2], "entity_d": entities[3], "visibility": visibility},
        actor_game_player_id=pooler,
        expected_version=None,
        now=now(),
    )
    pool_id = next(iter(game.pools))
    return proposal_id, pool_id, pool_events, entities


def test_executed_private_pool_reveals_contents_to_an_outsider_live():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    proposal_id, pool_id, _, entities = _propose_and_pool(game, tedy, mortia)

    outsider_before = next(p for p in project(game, PlayerAudience(hanky))["pools"] if p["pool_id"] == pool_id)
    assert "entity_c" not in outsider_before

    engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now())

    outsider_after = next(p for p in project(game, PlayerAudience(hanky))["pools"] if p["pool_id"] == pool_id)
    assert outsider_after["entity_c"] == entities[2]
    assert outsider_after["entity_d"] == entities[3]

    public_after = next(p for p in project(game, PublicAudience())["pools"] if p["pool_id"] == pool_id)
    assert public_after["entity_c"] == entities[2]


def test_executed_private_pool_reveals_contents_in_the_event_log():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    proposal_id, pool_id, create_events, entities = _propose_and_pool(game, tedy, mortia)
    created = [e for e in create_events if e.type == EventType.PRIVATE_POOL_CREATED]

    before = project_events(game, created, PlayerAudience(hanky))[0]
    assert "entity_c" not in before["payload"]

    engine.handle_command(game, command_type="ACCEPT_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now())

    after = project_events(game, created, PlayerAudience(hanky))[0]
    assert after["payload"]["entity_c"] == entities[2]
    assert after["payload"]["entity_d"] == entities[3]


def test_declined_private_pool_never_reveals_contents():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    proposal_id, pool_id, _, entities = _propose_and_pool(game, tedy, mortia)

    engine.handle_command(game, command_type="DECLINE_POOL", payload={"pool_id": pool_id}, actor_game_player_id=tedy, expected_version=None, now=now())

    outsider = next(p for p in project(game, PlayerAudience(hanky))["pools"] if p["pool_id"] == pool_id)
    assert "entity_c" not in outsider
    public = next(p for p in project(game, PublicAudience())["pools"] if p["pool_id"] == pool_id)
    assert "entity_c" not in public


def test_preempted_sibling_pool_never_reveals_contents():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    proposal_id, mortia_pool_id, _, entities = _propose_and_pool(game, tedy, mortia)

    # Josiah just accepts the bare base proposal, preempting Mortia's pool.
    engine.handle_command(
        game, command_type="ACCEPT_PROPOSAL", payload={"proposal_id": proposal_id}, actor_game_player_id=josiah, expected_version=None, now=later()
    )

    mortia_pool = next(p for p in game.pools.values() if p.pool_id == mortia_pool_id)
    assert mortia_pool.resolution_reason.value == "preempted_by_other_action"
    outsider = next(p for p in project(game, PlayerAudience(hanky))["pools"] if p["pool_id"] == mortia_pool_id)
    assert "entity_c" not in outsider
