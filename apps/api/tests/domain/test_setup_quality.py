"""Initial-distribution quality: hard-reject predicates as pure-function
unit tests, plus a statistical regression test that formalizes the Monte
Carlo validation this whole design was built on -- across many seeded
runs of the real pipeline, no result may violate that player count's own
configured hard constraints. See the plan's design writeup."""

from __future__ import annotations

import random

import pytest

from gotiate.domain import themes
from gotiate.domain.entities import SetupQualityConfig
from gotiate.domain.errors import UnableToGenerateStartingStateError
from gotiate.domain.setup import (
    SETUP_QUALITY_VERSION,
    _geometry_hard_reject,
    _geometry_metrics,
    _geometry_score,
    _topology_hard_reject,
    _topology_metrics,
    generate_starting_state,
)
from tests.conftest import now

THEME_SET = themes.get_theme_set("fictional_companies_v1")
DEFAULT_SHAPE = [2, 1, 1, 1]


def _config(**overrides) -> SetupQualityConfig:
    return SetupQualityConfig(max_pair_overlap=overrides.pop("max_pair_overlap", 2), **overrides)


# --------------------------------------------------------------------------
# Topology hard-reject predicates -- pure functions, hand-constructed
# portfolios, no theme set needed.
# --------------------------------------------------------------------------


def test_shared_anchor_is_hard_rejected():
    portfolios = {"p1": ["A", "A", "B", "C", "D"], "p2": ["A", "A", "E", "F", "G"]}
    metrics = _topology_metrics(portfolios)
    assert metrics.shared_anchor_pairs == [("p1", "p2")]
    assert _topology_hard_reject(metrics, _config(max_pair_overlap=4, reject_isolated_player=False))


def test_identical_portfolios_are_hard_rejected():
    portfolios = {"p1": ["A", "A", "B", "C", "D"], "p2": ["A", "A", "B", "C", "D"]}
    metrics = _topology_metrics(portfolios)
    assert metrics.identical_pairs == [("p1", "p2")]
    assert _topology_hard_reject(metrics, _config(max_pair_overlap=4, reject_isolated_player=False))


def test_overlap_over_cap_is_hard_rejected():
    # Shares B and C (not the anchor) -- overlap 2, cap 1.
    portfolios = {"p1": ["A", "A", "B", "C", "D"], "p2": ["E", "E", "B", "C", "F"]}
    metrics = _topology_metrics(portfolios)
    assert metrics.max_overlap == 2
    assert _topology_hard_reject(metrics, _config(max_pair_overlap=1, reject_isolated_player=False))
    assert not _topology_hard_reject(metrics, _config(max_pair_overlap=2, reject_isolated_player=False))


def test_isolated_player_hard_rejected_only_when_configured():
    portfolios = {
        "p1": ["A", "A", "B", "C", "D"],
        "p2": ["A", "A", "E", "F", "G"],  # shares anchor with p1, but we're only testing isolation here
        "p3": ["H", "H", "I", "J", "K"],  # shares nothing with anyone
    }
    metrics = _topology_metrics(portfolios)
    assert metrics.isolated_players == ["p3"]
    assert _topology_hard_reject(metrics, _config(max_pair_overlap=4, reject_isolated_player=True))
    # With isolation rejection off, this candidate is only rejected for its
    # (unrelated) shared anchor -- confirm isolation alone isn't the trigger.
    metrics_no_anchor_share = _topology_metrics({"p1": ["A", "A", "B", "C", "D"], "p2": ["E", "E", "F", "G", "H"], "p3": ["I", "I", "J", "K", "L"]})
    assert metrics_no_anchor_share.isolated_players == ["p1", "p2", "p3"]
    assert not _topology_hard_reject(metrics_no_anchor_share, _config(max_pair_overlap=4, reject_isolated_player=False))


def test_a_healthy_triangle_is_not_hard_rejected():
    # Hanky/Mortia/Josiah worked example from the design discussion: A-D
    # shared between the first pair, D shared... use distinct single
    # overlaps per pair, no shared anchor, nobody isolated.
    portfolios = {
        "hanky": ["A", "A", "B", "C", "D"],
        "mortia": ["D", "D", "E", "F", "G"],
        "josiah": ["G", "G", "H", "I", "J"],
    }
    metrics = _topology_metrics(portfolios)
    assert metrics.max_overlap == 1
    assert metrics.isolated_players == []
    assert metrics.shared_anchor_pairs == []
    assert not _topology_hard_reject(metrics, _config(max_pair_overlap=1, reject_isolated_player=True))


# --------------------------------------------------------------------------
# Geometry hard-reject predicate
# --------------------------------------------------------------------------


def test_elite_concentration_is_soft_penalized_not_hard_rejected():
    # Under Haircut, a top-heavy portfolio is no longer structurally
    # guaranteed dominant (the top of the scale carries real wipe risk) --
    # this moved from a hard reject to a soft penalty via the existing
    # elite_penalty_weight term. See the Haircut-risk design writeup.
    portfolios = {"p1": ["A", "A", "B", "C", "D"]}
    # A (anchor, counted once as a distinct entity), B, C in positions 1-3
    # (the elite zone for an 11-entity market at the default 1/3 fraction);
    # D elsewhere -- 3 of 4 distinct entities in the elite zone.
    position_of = {"A": 1, "B": 2, "C": 3, "D": 9, **{chr(ord("E") + i): 4 + i for i in range(6)}}
    metrics = _geometry_metrics(portfolios, position_of, market_size=11, config=_config())
    assert metrics.per_player["p1"]["elite_count"] == 3
    assert not _geometry_hard_reject(metrics, _config(elite_concentration_limit=3))
    assert not _geometry_hard_reject(metrics, _config(elite_concentration_limit=4))

    # But it does still cost real score, via elite_penalty_weight -- a
    # spread-out portfolio with zero elite concentration scores strictly
    # higher (less negative) than this top-heavy one, all else equal.
    spread_portfolios = {"p1": ["A", "A", "B", "C", "D"]}
    spread_position_of = {"A": 1, "B": 4, "C": 7, "D": 10, **{chr(ord("E") + i): 2 + i for i in range(6)}}
    spread_metrics = _geometry_metrics(spread_portfolios, spread_position_of, market_size=11, config=_config())
    assert _geometry_score(metrics, _config()) < _geometry_score(spread_metrics, _config())


def test_spread_out_holdings_are_not_hard_rejected():
    portfolios = {"p1": ["A", "A", "B", "C", "D"]}
    position_of = {"A": 1, "B": 4, "C": 7, "D": 10, **{chr(ord("E") + i): 2 + i for i in range(6)}}
    metrics = _geometry_metrics(portfolios, position_of, market_size=11, config=_config())
    assert not _geometry_hard_reject(metrics, _config(elite_concentration_limit=3))


# --------------------------------------------------------------------------
# Full pipeline: statistical regression test against the real theme set,
# formalizing the Monte Carlo validation the design itself was built on.
# --------------------------------------------------------------------------


DEFAULT_CONFIG_BY_PLAYERS = {
    2: SetupQualityConfig(max_pair_overlap=2, reject_isolated_player=False),
    3: SetupQualityConfig(max_pair_overlap=1, reject_isolated_player=True),
    4: SetupQualityConfig(max_pair_overlap=2, reject_isolated_player=True),
    5: SetupQualityConfig(max_pair_overlap=2, reject_isolated_player=True),
    6: SetupQualityConfig(max_pair_overlap=2, reject_isolated_player=True),
}


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
def test_generated_states_never_violate_their_own_hard_constraints(n):
    # A regression guard, not the statistical proof -- the standalone Monte
    # Carlo script (run separately, not part of every test run) is what
    # actually established these survival rates over tens of thousands of
    # trials. 15 real full-pipeline runs per player count is enough to
    # catch a broken hard-reject check without slowing down every `pytest`.
    config = DEFAULT_CONFIG_BY_PLAYERS[n]
    market_size = {2: 11, 3: 11, 4: 13, 5: 15, 6: 17}[n]
    rng = random.Random(20260813 + n)
    player_ids = [f"p{i}" for i in range(n)]

    for _ in range(15):
        state = generate_starting_state(THEME_SET, market_size, player_ids, DEFAULT_SHAPE, config, rng)

        topo_metrics = _topology_metrics(state.portfolios)
        assert not _topology_hard_reject(topo_metrics, config), state.diagnostics

        position_of = {eid: i + 1 for i, eid in enumerate(state.market_order)}
        geom_metrics = _geometry_metrics(state.portfolios, position_of, market_size, config)
        assert not _geometry_hard_reject(geom_metrics, config), state.diagnostics


# --------------------------------------------------------------------------
# Explicit failure -- never a silently relaxed constraint.
# --------------------------------------------------------------------------


def test_unsatisfiable_config_fails_explicitly_not_silently():
    # max_pair_overlap=0 combined with reject_isolated_player=True is
    # impossible for any n >= 2: a graph with zero edges has every node
    # isolated by definition. Low attempt ceiling so the test is fast.
    config = SetupQualityConfig(max_pair_overlap=0, reject_isolated_player=True, max_generation_attempts=200)
    rng = random.Random(1)
    with pytest.raises(UnableToGenerateStartingStateError):
        generate_starting_state(THEME_SET, 11, ["p0", "p1", "p2"], DEFAULT_SHAPE, config, rng)


# --------------------------------------------------------------------------
# Diagnostics payload shape, via a real START_GAME command.
# --------------------------------------------------------------------------


def test_portfolio_dealt_payload_carries_setup_diagnostics():
    from gotiate.domain import engine

    # make_started_game (tests.conftest) discards START_GAME's own events,
    # so collect them directly here instead -- same pattern as
    # test_event_visibility.py's _start_game_and_collect_events.
    game, events = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now())
    for i in range(1, 3):
        _, join_events = engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"Player {i}", now=now())
        events += join_events
    events += engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )

    dealt = next(e for e in events if e.type.value == "PORTFOLIO_DEALT")
    payload = dealt.payload
    assert payload["setup_quality_version"] == SETUP_QUALITY_VERSION
    assert payload["player_count"] == 3
    for key in ("config", "topology_metrics", "geometry_metrics", "topology_score", "geometry_score", "combined_score", "candidate_counts"):
        assert key in payload
