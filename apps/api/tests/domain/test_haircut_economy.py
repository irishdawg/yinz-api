"""The Haircut-risk scoring economy that replaced Waterline -- see the
design writeup. Covers: HaircutProfile validation and the config/profile-
drift guard, the draw/score split (one random draw at close, a pure
deterministic scorer everywhere else), the halftime reveal time-transition,
and find_active_game_seated_in."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from gotiate.domain import engine
from gotiate.domain.entities import GameConfig, GamePhase, HaircutProfile, Holding, HoldingZone
from gotiate.domain.projections import PublicAudience, project
from gotiate.persistence.repository import InMemoryGameRepository
from tests.conftest import make_started_game, now


# --------------------------------------------------------------------------
# HaircutProfile validation
# --------------------------------------------------------------------------


def test_depth_probabilities_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        HaircutProfile(depth_probabilities=[0.5, 0.4])


def test_depth_probabilities_must_be_non_negative():
    with pytest.raises(ValueError, match="non-negative"):
        HaircutProfile(depth_probabilities=[1.2, -0.2])


def test_depth_probabilities_must_not_be_empty():
    with pytest.raises(ValueError, match="not be empty"):
        HaircutProfile(depth_probabilities=[])


def test_max_depth_is_the_highest_index_with_nonzero_probability():
    # Not just len - 1 -- a trailing zero shouldn't count as reachable depth.
    assert HaircutProfile(depth_probabilities=[0.5, 0.5, 0.0]).max_depth == 1
    assert HaircutProfile(depth_probabilities=[1.0]).max_depth == 0


def test_worked_example_certainty_curve_matches_the_design_writeup():
    # The exact 13-slot-market worked example from the design discussion:
    # cumulative certainty 48/67/81/92/98/100% at positions 1-6. Not a
    # dedicated server-side certainty() function (certainty is computed
    # client-side from the public profile, by design), so this exercises
    # the authored default profile data directly against the same
    # sum(depth_probabilities[0:position]) formula the frontend uses.
    profile = next(
        p for p in GameConfig().haircut_profiles_by_players[4] if p.depth_probabilities == [0.48, 0.19, 0.14, 0.11, 0.06, 0.02]
    )
    expected = [0.48, 0.67, 0.81, 0.92, 0.98, 1.00]
    cumulative = 0.0
    for position, want in enumerate(expected, start=1):
        cumulative = sum(profile.depth_probabilities[0:position])
        assert cumulative == pytest.approx(want, abs=1e-9)


def test_config_rejects_a_profile_whose_max_depth_drifts_from_the_risk_band():
    # The enforced invariant from the design writeup: every profile's
    # max_depth must equal round(market_size * risk_depth_fraction) for its
    # player count, so config and profile data can never silently drift
    # apart. This profile has max_depth=2, not the 9-entity market's
    # expected round(9 * 0.35) = 3.
    with pytest.raises(ValueError, match="max_depth"):
        GameConfig(haircut_profiles_by_players={2: [HaircutProfile(depth_probabilities=[0.5, 0.3, 0.2])]})


def test_default_config_profiles_satisfy_their_own_risk_band_invariant():
    # Constructing the real default GameConfig must not raise -- this is
    # the validator running against the actual authored profile sets, not
    # a synthetic one.
    GameConfig()


# --------------------------------------------------------------------------
# draw_haircut_depth -- the sole random draw
# --------------------------------------------------------------------------


def test_draw_haircut_depth_tracks_the_configured_distribution():
    profile = HaircutProfile(depth_probabilities=[0.5, 0.5])
    rng = random.Random(12345)
    trials = 5000
    depth1_count = sum(1 for _ in range(trials) if engine.draw_haircut_depth(profile, rng) == 1)
    assert abs(depth1_count / trials - 0.5) < 0.05


def test_draw_haircut_depth_only_ever_returns_configured_depths():
    profile = HaircutProfile(depth_probabilities=[0.1, 0.0, 0.9])
    rng = random.Random(1)
    for _ in range(200):
        assert engine.draw_haircut_depth(profile, rng) in (0, 2)


# --------------------------------------------------------------------------
# compute_final_scores -- pure, rng-free, called with an already-drawn depth
# --------------------------------------------------------------------------


def _linear_rank_value(market_size: int, position: int) -> int:
    return market_size - position + 1


def test_compute_final_scores_depth_zero_wipes_nothing():
    game = make_started_game(2)
    n = len(game.market)
    result = engine.compute_final_scores(game, 0)
    assert result["wiped_entity_ids"] == []
    for r in result["results"]:
        holdings = [h for h in game.holdings.values() if h.owner_player_id == r["game_player_id"] and h.zone == HoldingZone.PORTFOLIO]
        expected = sum(_linear_rank_value(n, game.market[h.entity_id].position) for h in holdings)
        assert r["final_value"] == expected


def test_compute_final_scores_wipes_positions_one_through_depth_and_only_those():
    game = make_started_game(2)
    n = len(game.market)
    depth = 3
    result = engine.compute_final_scores(game, depth)
    assert result["wiped_entity_ids"] == sorted(eid for eid, m in game.market.items() if m.position <= depth)

    for r in result["results"]:
        holdings = [h for h in game.holdings.values() if h.owner_player_id == r["game_player_id"] and h.zone == HoldingZone.PORTFOLIO]
        expected = sum(
            _linear_rank_value(n, game.market[h.entity_id].position) for h in holdings if game.market[h.entity_id].position > depth
        )
        assert r["final_value"] == expected


def test_compute_final_scores_zeroes_a_doubled_anchor_holding_twice_when_wiped():
    # portfolio_shape's first entry is 2 -- every dealt portfolio has a
    # doubled/anchor entity by construction. Move it into the wiped band
    # and confirm both copies drop out, not just one.
    game = make_started_game(2)
    p0 = game.players[0].game_player_id
    p0_holdings = [h for h in game.holdings.values() if h.owner_player_id == p0 and h.zone == HoldingZone.PORTFOLIO]
    counts: dict[str, int] = {}
    for h in p0_holdings:
        counts[h.entity_id] = counts.get(h.entity_id, 0) + 1
    anchor_entity = next(eid for eid, c in counts.items() if c == 2)

    # Force the anchor into position 1 (guaranteed wiped at depth=1),
    # swapping with whatever currently sits there -- unless it's already
    # there, in which case there's nothing to do.
    if game.market[anchor_entity].position != 1:
        other_entity = next(eid for eid in game.market if game.market[eid].position == 1)
        game.market[anchor_entity].position, game.market[other_entity].position = (
            game.market[other_entity].position,
            game.market[anchor_entity].position,
        )
    assert game.market[anchor_entity].position == 1

    n = len(game.market)
    without_wipe = engine.compute_final_scores(game, 0)
    with_wipe = engine.compute_final_scores(game, 1)
    p0_without = next(r for r in without_wipe["results"] if r["game_player_id"] == p0)["final_value"]
    p0_with = next(r for r in with_wipe["results"] if r["game_player_id"] == p0)["final_value"]
    # Both copies of the anchor holding drop out -- the delta is exactly
    # twice its linear-rank value, not once.
    assert p0_without - p0_with == 2 * _linear_rank_value(n, 1)


def test_compute_final_scores_exact_tie_shares_the_win():
    game = make_started_game(2)
    p0, p1 = [p.game_player_id for p in game.players]
    n = len(game.market)
    by_position = {m.position: eid for eid, m in game.market.items()}

    for h in list(game.holdings.values()):
        del game.holdings[h.holding_id]

    def _give(owner: str, positions: list[int]) -> None:
        for pos in positions:
            h = Holding(
                holding_id=f"h-{owner}-{pos}", entity_id=by_position[pos], owner_player_id=owner, zone=HoldingZone.PORTFOLIO
            )
            game.holdings[h.holding_id] = h

    # value(pos) = n - pos + 1. {2, 8} and {4, 6} both sum to n+1+n-1=2n... in
    # general: (n-1) + (n-7) vs (n-3) + (n-5) -- both equal 2n-8, an exact
    # tie regardless of n.
    _give(p0, [2, 8])
    _give(p1, [4, 6])

    result = engine.compute_final_scores(game, 0)
    p0_value = next(r for r in result["results"] if r["game_player_id"] == p0)["final_value"]
    p1_value = next(r for r in result["results"] if r["game_player_id"] == p1)["final_value"]
    assert p0_value == p1_value
    assert set(result["winners"]) == {p0, p1}


def test_compute_final_scores_is_pure_and_idempotent():
    game = make_started_game(3)
    first = engine.compute_final_scores(game, 2)
    second = engine.compute_final_scores(game, 2)
    assert first == second


# --------------------------------------------------------------------------
# Halftime reveal -- apply_due_time_transitions / is_time_transition_due
# --------------------------------------------------------------------------


def test_haircut_risk_revealed_fires_exactly_at_the_configured_fraction():
    game = make_started_game(2)
    assert game.haircut_profile_revealed_at is None
    assert game.haircut_reveal_at is not None

    just_before = game.haircut_reveal_at - timedelta(seconds=1)
    events = engine.apply_due_time_transitions(game, just_before)
    assert not any(e.type.value == "HAIRCUT_RISK_REVEALED" for e in events)
    assert game.haircut_profile_revealed_at is None

    events = engine.apply_due_time_transitions(game, game.haircut_reveal_at)
    assert any(e.type.value == "HAIRCUT_RISK_REVEALED" for e in events)
    assert game.haircut_profile_revealed_at == game.haircut_reveal_at


def test_haircut_risk_revealed_does_not_refire_once_already_revealed():
    game = make_started_game(2)
    engine.apply_due_time_transitions(game, game.haircut_reveal_at)
    revealed_at = game.haircut_profile_revealed_at
    assert revealed_at is not None

    events = engine.apply_due_time_transitions(game, game.haircut_reveal_at + timedelta(minutes=5))
    assert not any(e.type.value == "HAIRCUT_RISK_REVEALED" for e in events)
    assert game.haircut_profile_revealed_at == revealed_at


def test_ready_threshold_close_before_halfway_never_live_reveals_but_project_exposes_once_scored():
    game = make_started_game(2)
    p0, p1 = [p.game_player_id for p in game.players]
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p0, expected_version=None, now=now())
    engine.handle_command(game, command_type="SET_READY_TO_CLOSE", payload={"ready": True}, actor_game_player_id=p1, expected_version=None, now=now())

    assert game.phase == GamePhase.SCORED
    assert game.haircut_profile_revealed_at is None  # the live reveal transition never fired

    view = project(game, PublicAudience())
    assert view["haircut_profile"] == {"depth_probabilities": game.haircut_profile.depth_probabilities}


# --------------------------------------------------------------------------
# find_active_game_seated_in
# --------------------------------------------------------------------------


async def test_find_active_game_seated_in_returns_the_game_for_a_non_host_seat():
    repo = InMemoryGameRepository()
    game, events = engine.create_game(actor_auth_user_id="auth-host", display_name="Host", now=now())
    await repo.create(game)
    await repo.append_events(events)

    _, join_events = engine.join_game(game, actor_auth_user_id="auth-joiner", display_name="Joiner", now=now())
    await repo.save(game)
    await repo.append_events(join_events)

    found = await repo.find_active_game_seated_in("auth-joiner")
    assert found is not None
    assert found.id == game.id

    assert await repo.find_active_game_seated_in("auth-someone-else") is None


async def test_find_active_game_seated_in_returns_none_once_scored_or_cancelled():
    repo = InMemoryGameRepository()
    game, events = engine.create_game(actor_auth_user_id="auth-host", display_name="Host", now=now())
    await repo.create(game)
    await repo.append_events(events)

    game.phase = GamePhase.SCORED
    await repo.save(game)
    assert await repo.find_active_game_seated_in("auth-host") is None

    game.phase = GamePhase.CANCELLED
    await repo.save(game)
    assert await repo.find_active_game_seated_in("auth-host") is None
