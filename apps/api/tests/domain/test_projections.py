"""The visibility matrix — domain model §06. This is the security-critical
surface: hiding information from other players is the entire game."""

from __future__ import annotations

from datetime import timedelta

from gotiate.domain import engine
from gotiate.domain.entities import CloseReason, GameConfig, GamePhase
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


def test_ready_to_close_revealed_to_all_after_ready_threshold_close():
    # The one post-hoc exception to the self-only rule: once the market
    # closed *because* the ready threshold was reached, every player's
    # ready_to_close is exposed table-wide so the results leaderboard can
    # mark who voted. A non-voter (4-player threshold is 3) shows False,
    # not omitted.
    game = make_started_game(4)
    voters = [p.game_player_id for p in game.players[:3]]
    abstainer = game.players[3].game_player_id
    for pid in voters:
        engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=pid, expected_version=None, now=now())

    assert game.phase == GamePhase.SCORED
    assert game.close_reason is CloseReason.READY_THRESHOLD

    for audience in (PlayerAudience(abstainer), PublicAudience()):
        players = {p["game_player_id"]: p for p in project(game, audience)["players"]}
        for pid in voters:
            assert players[pid]["ready_to_close"] is True
        assert players[abstainer]["ready_to_close"] is False


def test_ready_to_close_stays_hidden_when_close_reason_is_not_ready_threshold():
    game = make_started_game(4, config=GameConfig(negotiation_abandonment_seconds=5.0))
    p0, p1 = game.players[0].game_player_id, game.players[1].game_player_id
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())

    engine.apply_due_time_transitions(game, game.last_activity_at + timedelta(seconds=10))
    assert game.close_reason is CloseReason.ABANDONED

    other = next(p for p in project(game, PlayerAudience(p1))["players"] if p["game_player_id"] == p0)
    assert "ready_to_close" not in other


def test_haircut_reveal_in_moves_counts_down_then_nulls_once_revealed():
    # The Move-driven successor to the old gameplay clock: total allocation
    # is 2 * 3 = 6, so the profile reveals the instant cumulative consumed
    # Moves reach 3 ((6 + 1) // 2). Only opening a bare negotiation costs a
    # Move; passing is free.
    game = make_started_game(2, config=GameConfig(starting_moves=3))
    p0, p1 = [p.game_player_id for p in game.players]
    e = list(game.market.keys())

    def open_then_pass(proposer: str, passer: str, ea: str, eb: str) -> None:
        events = engine.handle_command(
            game, command_type="PROPOSE_SWAP", payload={"entity_a": ea, "entity_b": eb}, actor_game_player_id=proposer, expected_version=None, now=now()
        )
        pid = next(ev.payload["proposal_id"] for ev in events if ev.type.value == "PROPOSAL_CREATED")
        engine.handle_command(game, command_type="PASS_PROPOSAL", payload={"proposal_id": pid}, actor_game_player_id=passer, expected_version=None, now=now())

    assert project(game, PublicAudience())["haircut_reveal_in_moves"] == 3

    open_then_pass(p0, p1, e[0], e[1])  # 1 consumed
    assert project(game, PublicAudience())["haircut_reveal_in_moves"] == 2

    open_then_pass(p1, p0, e[2], e[3])  # 2 consumed
    assert project(game, PublicAudience())["haircut_reveal_in_moves"] == 1

    open_then_pass(p0, p1, e[0], e[1])  # 3 consumed -- reveal fires mid-command
    assert game.haircut_profile_revealed_at is not None
    revealed = project(game, PublicAudience())
    assert revealed["haircut_reveal_in_moves"] is None
    assert revealed["haircut_profile"] is not None


def test_close_reason_and_closed_at_null_before_close_public_once_closed():
    # The *fact* of why/when the market closed, not a leading indicator --
    # contrast with ready_to_close itself, which stays strictly self-only
    # (test_ready_to_close_never_visible_to_others above) with no public
    # aggregate anywhere. Ready-to-close is a secret trigger; close_reason
    # is the public "boom" the moment it fires. See the Stage 6 design
    # writeup.
    game = make_started_game(2)
    p0, p1 = [p.game_player_id for p in game.players]

    view = project(game, PublicAudience())
    assert view["close_reason"] is None
    assert view["closed_at"] is None

    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p1, expected_version=None, now=now())

    assert game.phase == GamePhase.SCORED
    public_view = project(game, PublicAudience())
    assert public_view["close_reason"] == "READY_THRESHOLD"
    assert public_view["closed_at"] is not None


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


def test_haircut_risk_band_depth_public_before_and_after_reveal():
    # Unlike haircut_profile itself, the risk band boundary is public from
    # game start -- computed straight from config (round(market_size *
    # risk_depth_fraction)), deliberately NOT game.haircut_profile.max_depth,
    # since a randomly generated profile's own effective depth can land
    # earlier than the structural slot count -- see
    # engine._generate_random_haircut_profile's docstring.
    game = make_started_game(2)
    view_before = project(game, PublicAudience())
    assert view_before["haircut_profile"] is None
    expected_depth = round(len(game.market) * game.config.risk_depth_fraction)
    assert view_before["haircut_risk_band_depth"] == expected_depth

    game.haircut_profile_revealed_at = now()
    view_after = project(game, PublicAudience())
    assert view_after["haircut_risk_band_depth"] == view_before["haircut_risk_band_depth"]


def test_haircut_profile_hidden_before_reveal():
    game = make_started_game(2)
    view = project(game, PublicAudience())
    assert view["haircut_profile"] is None


def test_haircut_profile_visible_once_revealed():
    game = make_started_game(2)
    game.haircut_profile_revealed_at = now()  # simulates apply_due_time_transitions firing HAIRCUT_RISK_REVEALED
    view = project(game, PublicAudience())
    assert view["haircut_profile"] == {"depth_probabilities": game.haircut_profile.depth_probabilities}
