"""project_events()'s visibility matrix -- a security boundary, not a
formatting convenience. EVENT_VISIBILITY must be exhaustive: an EventType
with no explicit policy is omitted entirely (fail safe), not shown by
default."""

from __future__ import annotations

from gotiate.domain import engine
from gotiate.domain.events import EventType
from gotiate.domain.projections import EVENT_VISIBILITY, PlayerAudience, PublicAudience, ReplayAudience, project_events
from tests.conftest import make_started_game, now


def _start_game_and_collect_events(player_count: int = 2):
    """engine.handle_command/create_game/join_game each just return the
    events *they* emitted -- nothing centrally accumulates them outside a
    real repository's append_events(). Tests that need START_GAME's own
    events (GAME_STARTED, WATERLINE_SELECTED, ...) can't use
    tests.conftest.make_started_game, which discards them; this collects
    the same sequence a real get_events() call would return."""
    game, events = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now())
    for i in range(1, player_count):
        _, join_events = engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"Player {i}", now=now())
        events += join_events
    events += engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )
    return game, events


def test_event_visibility_registry_is_exhaustive():
    assert set(EVENT_VISIBILITY.keys()) == set(EventType)


def test_public_event_visible_to_everyone():
    game, events = _start_game_and_collect_events(2)
    p0 = game.players[0].game_player_id
    started = [e for e in events if e.type == EventType.GAME_STARTED]
    assert started

    assert any(v["type"] == "GAME_STARTED" for v in project_events(game, started, PublicAudience()))
    assert any(v["type"] == "GAME_STARTED" for v in project_events(game, started, PlayerAudience(p0)))


def test_actor_only_event_hidden_from_others_and_public():
    game = make_started_game(2)
    p0, p1 = [p.game_player_id for p in game.players]
    events = engine.handle_command(
        game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now()
    )
    assert [e for e in events if e.type == EventType.READY_TO_CLOSE_CHANGED]

    own = [v for v in project_events(game, events, PlayerAudience(p0)) if v["type"] == "READY_TO_CLOSE_CHANGED"]
    assert own

    others = [v for v in project_events(game, events, PlayerAudience(p1)) if v["type"] == "READY_TO_CLOSE_CHANGED"]
    assert others == []

    public = [v for v in project_events(game, events, PublicAudience()) if v["type"] == "READY_TO_CLOSE_CHANGED"]
    assert public == []


def test_server_only_event_hidden_live_from_everyone_including_actor():
    game, events = _start_game_and_collect_events(2)
    waterline = [e for e in events if e.type == EventType.WATERLINE_SELECTED]
    assert waterline

    for audience in (PublicAudience(), PlayerAudience(game.players[0].game_player_id), PlayerAudience(game.players[1].game_player_id)):
        assert project_events(game, waterline, audience) == []


def test_server_only_event_visible_via_replay_once_scored():
    game, events = _start_game_and_collect_events(2)
    waterline = [e for e in events if e.type == EventType.WATERLINE_SELECTED]
    p0, p1 = [p.game_player_id for p in game.players]
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p1, expected_version=None, now=now())

    assert project_events(game, waterline, ReplayAudience())


def test_pool_insiders_content_redacted_for_outsider_visible_to_insiders():
    game = make_started_game(4)
    tedy, mortia, hanky, josiah = [p.game_player_id for p in game.players]
    entities = list(game.market.keys())

    engine.handle_command(
        game, command_type="PROPOSE_SWAP", payload={"entity_a": entities[0], "entity_b": entities[1]}, actor_game_player_id=tedy, expected_version=None, now=now()
    )
    proposal_id = next(iter(game.proposals))
    pool_events = engine.handle_command(
        game,
        command_type="CREATE_POOL",
        payload={"proposal_id": proposal_id, "entity_c": entities[2], "entity_d": entities[3], "visibility": "private"},
        actor_game_player_id=mortia,
        expected_version=None,
        now=now(),
    )
    events = [e for e in pool_events if e.type == EventType.PRIVATE_POOL_CREATED]
    assert events

    outsider_view = project_events(game, events, PlayerAudience(hanky))[0]
    assert "entity_c" not in outsider_view["payload"]
    assert "entity_d" not in outsider_view["payload"]
    assert outsider_view["type"] == "PRIVATE_POOL_CREATED"  # existence/type stays public

    insider_view = project_events(game, events, PlayerAudience(mortia))[0]
    assert insider_view["payload"]["entity_c"] == entities[2]

    base_proposer_view = project_events(game, events, PlayerAudience(tedy))[0]  # base proposer is also an insider
    assert base_proposer_view["payload"]["entity_c"] == entities[2]

    public_view = project_events(game, events, PublicAudience())[0]
    assert "entity_c" not in public_view["payload"]


def test_unknown_event_type_is_omitted_not_defaulted_to_public():
    game, events = _start_game_and_collect_events(2)
    started = [e for e in events if e.type == EventType.GAME_STARTED]
    assert started

    import gotiate.domain.projections as projections_module

    trimmed_registry = dict(EVENT_VISIBILITY)
    del trimmed_registry[EventType.GAME_STARTED]
    original = projections_module.EVENT_VISIBILITY
    try:
        projections_module.EVENT_VISIBILITY = trimmed_registry
        views = project_events(game, started, PublicAudience())
        assert views == []
    finally:
        projections_module.EVENT_VISIBILITY = original
