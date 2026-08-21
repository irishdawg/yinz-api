"""The Haircut-risk scoring economy that replaced Waterline -- see the
design writeup. Covers: HaircutProfile validation and the config/profile-
drift guard, the draw/score split (one random draw at close, a pure
deterministic scorer everywhere else), the halftime reveal time-transition,
and find_active_game_seated_in."""

from __future__ import annotations

import random

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


def test_generated_profile_is_valid_across_every_configured_player_count():
    # round(market_size * risk_depth_fraction) for n=2..6 -- see
    # GameConfig.market_size_by_players / risk_depth_fraction. One extra
    # entry beyond risk_band_depth -- see the "off by one" note in
    # _generate_random_haircut_profile's own docstring.
    risk_band_depths = [round(GameConfig().market_size_by_players[n] * GameConfig().risk_depth_fraction) for n in range(2, 7)]
    rng = random.Random(7)
    for depth in risk_band_depths:
        for _ in range(200):
            profile = engine._generate_random_haircut_profile(depth, rng)
            assert len(profile.depth_probabilities) == depth + 1
            assert sum(profile.depth_probabilities) == pytest.approx(1.0, abs=1e-9)
            assert all(p >= 0 for p in profile.depth_probabilities)


def test_the_last_in_band_position_never_reaches_full_safety():
    # The actual bug caught in real play: building exactly risk_band_depth
    # entries forced summing all of them -- the deepest in-band position's
    # own survival chance -- to exactly 1.0, so "the top K positions carry
    # some risk" was a lie for position K itself, always perfectly safe in
    # practice. Now the profile carries one extra slot past the band, and
    # every in-band position's own cumulative survival is capped at
    # _HAIRCUT_WITHIN_BAND_CEILING (92%).
    rng = random.Random(29)
    risk_band_depth = 4
    for _ in range(2000):
        profile = engine._generate_random_haircut_profile(risk_band_depth, rng)
        assert len(profile.depth_probabilities) == risk_band_depth + 1
        last_in_band_survival = sum(profile.depth_probabilities[:risk_band_depth])
        assert last_in_band_survival <= engine._HAIRCUT_WITHIN_BAND_CEILING + 1e-9
        assert last_in_band_survival < 1.0 - 1e-9
        # The one slot past the band absorbs the remainder -- always at
        # least (1 - ceiling), never zero.
        assert profile.depth_probabilities[-1] >= (1.0 - engine._HAIRCUT_WITHIN_BAND_CEILING) - 1e-9


def test_generated_profile_respects_the_first_and_second_position_survival_ranges():
    # sum(depth_probabilities[0:position]) is the survival CDF -- position
    # 1's own survival is depth_probabilities[0] alone, position 2's is
    # the first two summed. See _generate_random_haircut_profile's
    # docstring for why this is the natural space to bound.
    rng = random.Random(11)
    for _ in range(2000):
        profile = engine._generate_random_haircut_profile(5, rng)
        first = profile.depth_probabilities[0]
        second = first + profile.depth_probabilities[1]
        assert 0.05 <= first <= 0.50
        assert 0.11 <= second <= 0.61
        assert second > first


def test_generated_profile_adjacent_depths_are_never_closer_than_the_minimum_gap():
    # Real playtest feedback: an unbounded draw landed two adjacent
    # positions only ~1 percentage point apart, reading as no real
    # differentiation. Every adjacent pair of cumulative survival values
    # must now differ by at least _HAIRCUT_MIN_ADJACENT_GAP (4%), with two
    # exemptions: a step that *lands on* the within-band ceiling (whether
    # it's the deepest in-band position or an earlier "big jump to the
    # ceiling" -- reaching it is always meaningful regardless of how far
    # it had to jump, explicitly tolerated per real playtest direction),
    # and any gap once the ceiling was already reached earlier (every
    # further in-band slot is trivially 0 -- flat continuations, not two
    # genuinely different risk levels crammed together). The final jump
    # past the band (always >= 1 - ceiling, i.e. >= 8%) is excluded by the
    # loop bound itself, same as before.
    rng = random.Random(23)
    ceiling = engine._HAIRCUT_WITHIN_BAND_CEILING
    for _ in range(2000):
        profile = engine._generate_random_haircut_profile(6, rng)
        cumulative = []
        running = 0.0
        for p in profile.depth_probabilities:
            running += p
            cumulative.append(running)
        for i in range(1, len(cumulative) - 1):
            if cumulative[i - 1] >= ceiling - 1e-9 or cumulative[i] >= ceiling - 1e-9:
                continue
            gap = cumulative[i] - cumulative[i - 1]
            assert gap >= 0.04 - 1e-9, f"gap {gap} between depths {i - 1} and {i} is below the 4% floor"


def test_generated_profile_certainty_is_monotonic_and_reaches_100_percent_at_the_last_slot():
    rng = random.Random(13)
    for _ in range(500):
        profile = engine._generate_random_haircut_profile(6, rng)
        cumulative = 0.0
        for p in profile.depth_probabilities:
            new_cumulative = cumulative + p
            assert new_cumulative >= cumulative - 1e-9  # monotonic non-decreasing
            cumulative = new_cumulative
        assert cumulative == pytest.approx(1.0, abs=1e-9)


def test_generated_profiles_show_real_game_to_game_variability():
    # Not a family of curves a player could learn to recognize -- draws
    # across many seeds should span close to the full stated first
    # -position range, not cluster around one or two shapes.
    rng = random.Random(17)
    firsts = [engine._generate_random_haircut_profile(5, rng).depth_probabilities[0] for _ in range(500)]
    assert min(firsts) < 0.12
    assert max(firsts) > 0.43
    assert max(firsts) - min(firsts) > 0.30


def test_generated_profile_can_hit_the_ceiling_before_the_last_in_band_position():
    # "A big jump" is allowed to happen early within the band too -- once
    # the ceiling's reached, every subsequent in-band position (before the
    # one guaranteed-nonzero slot past the band) shows a genuinely zero
    # probability of its own, a flat continuation rather than a forced
    # small residual.
    rng = random.Random(19)
    risk_band_depth = 6
    assert any(
        0.0 in engine._generate_random_haircut_profile(risk_band_depth, rng).depth_probabilities[:-1] for _ in range(500)
    )


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
# Halftime reveal -- wired up in a later checkpoint (Moves don't exist yet
# in checkpoint 1, so there's nothing to trigger a live reveal against).
# --------------------------------------------------------------------------


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
