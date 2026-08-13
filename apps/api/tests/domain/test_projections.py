"""The visibility matrix — domain model §06. This is the security-critical
surface: hiding information from other players is the entire game."""

from __future__ import annotations

from gotiate.domain import engine
from gotiate.domain.projections import PlayerAudience, PublicAudience, project
from tests.conftest import make_started_game, now


def test_private_pool_contents_hidden_from_third_party_visible_to_insiders():
    game = make_started_game(4)
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

    view_hanky = project(game, PlayerAudience(hanky))
    pool_view = next(p for p in view_hanky["pools"] if p["initiator_id"] == mortia)
    assert pool_view["initiator_id"] == mortia  # existence + who is public
    assert "entity_c" not in pool_view  # contents are not

    view_mortia = project(game, PlayerAudience(mortia))
    assert next(p for p in view_mortia["pools"] if p["initiator_id"] == mortia)["entity_c"] == entities[2]

    view_tedy = project(game, PlayerAudience(tedy))  # base proposer is also an insider
    assert next(p for p in view_tedy["pools"] if p["initiator_id"] == mortia)["entity_c"] == entities[2]


def test_ready_to_close_never_visible_to_others():
    game = make_started_game(2)
    p0, p1 = [p.game_player_id for p in game.players]
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())

    view_p1 = project(game, PlayerAudience(p1))
    other = next(p for p in view_p1["players"] if p["game_player_id"] == p0)
    assert "ready_to_close" not in other

    view_p0 = project(game, PlayerAudience(p0))
    self_view = next(p for p in view_p0["players"] if p["game_player_id"] == p0)
    assert self_view["ready_to_close"] is True


def test_holdings_hidden_before_scored_visible_after():
    game = make_started_game(2)

    public_view = project(game, PublicAudience())
    assert "holdings" not in public_view

    p0 = game.players[0].game_player_id
    p1 = game.players[1].game_player_id
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p1, expected_version=None, now=now())

    scored_public_view = project(game, PublicAudience())
    assert len(scored_public_view["holdings"]) == len(game.holdings)


def test_scored_game_with_no_realized_haircut_depth_does_not_crash():
    # Real production incident: a game that reached SCORED under the
    # retired waterline_baseline_v1 model (before realized_haircut_depth
    # existed) has that field as None forever -- the old winner data is
    # gone, not recoverable, so project() must skip the results/winners
    # merge gracefully rather than asserting/crashing. See the Haircut-risk
    # design writeup's SCORED-branch note.
    game = make_started_game(2)
    p0 = game.players[0].game_player_id
    p1 = game.players[1].game_player_id
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p1, expected_version=None, now=now())
    assert game.realized_haircut_depth is not None  # sanity check on the normal path

    game.realized_haircut_depth = None  # simulates a legacy pre-migration SCORED game
    view = project(game, PublicAudience())  # must not raise
    assert "results" not in view
    assert "winners" not in view
    assert len(view["holdings"]) == len(game.holdings)  # holdings still revealed regardless


def test_haircut_profile_hidden_before_reveal():
    game = make_started_game(2)
    view = project(game, PublicAudience())
    assert view["haircut_profile"] is None
    assert view["haircut_reveal_at"] is not None  # the deadline itself isn't secret, only the contents


def test_haircut_profile_visible_once_revealed():
    game = make_started_game(2)
    game.haircut_profile_revealed_at = now()  # simulates apply_due_time_transitions firing HAIRCUT_RISK_REVEALED
    view = project(game, PublicAudience())
    assert view["haircut_profile"] == {"depth_probabilities": game.haircut_profile.depth_probabilities}
