"""Initial-distribution quality — generate a starting market + portfolio
assignment that's random but not pathological. Pure, synchronous, zero I/O,
same discipline as engine.py.

Two-phase generate/hard-filter/score/select, over the *complete* starting
state rather than one search:

Phase A (topology): generate many portfolio assignments (who owns what --
this is independent of market position, since a player's set of entity
IDs doesn't care where they sit). Hard-reject pathological ones (shared
anchor, near-identical hands, an isolated player). Score survivors, keep
a shortlist rather than declaring one topology "the winner."

Phase B (geometry): for every shortlisted topology, generate many market
permutations. Hard-reject fortress starts (too many of one player's
interests clustered at the top of the scale). Score survivors, pool every
surviving (topology, permutation) complete state across the *whole*
shortlist together, and randomly select from the top band of that pooled
ranking. The final choice reflects real starting geometry, not just the
theoretically nicest ownership graph on paper.

A hard constraint that can't be satisfied within the attempt budget fails
loudly (errors.UnableToGenerateStartingStateError) -- never silently
relaxed. See the initial-distribution-quality design writeup for the
empirical survival-rate measurements this is built on.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations

from gotiate.domain.entities import SetupQualityConfig
from gotiate.domain.errors import IllegalCommandError, UnableToGenerateStartingStateError
from gotiate.domain.themes import ThemeEntityDefinition, ThemeSet

SETUP_QUALITY_VERSION = 1


# --------------------------------------------------------------------------
# Market entity selection (which entities, not where they sit -- position
# is entirely Phase B's concern, re-shuffled fresh per geometry candidate)
# --------------------------------------------------------------------------


def select_market_entities(theme_set: ThemeSet, market_size: int, rng: random.Random) -> list[ThemeEntityDefinition]:
    if len(theme_set.entities) < market_size:
        raise IllegalCommandError(
            f"theme set {theme_set.theme_set_id!r} has only {len(theme_set.entities)} entities, needs {market_size}"
        )
    locked = [e for e in theme_set.entities if e.is_locked]
    swappable = [e for e in theme_set.entities if not e.is_locked]
    if len(locked) > market_size:
        raise IllegalCommandError(
            f"theme set {theme_set.theme_set_id!r} has {len(locked)} locked entities but the {market_size}-entity market can't fit them all"
        )
    # Locked entities are always dealt in; the swappable pool fills whatever's
    # left. "Locked" means always present, not fixed to a particular position
    # -- position is decided fresh per Phase B candidate, not here.
    return locked + rng.sample(swappable, market_size - len(locked))


def _deal_portfolio(entity_ids: list[str], shape: list[int], rng: random.Random) -> list[str]:
    """One player's portfolio holdings, expanded (the doubled/anchor entity
    -- whichever `sample` returns first -- appears twice). Independent of
    every other player's portfolio and of market position."""
    chosen = rng.sample(entity_ids, len(shape))
    holdings: list[str] = []
    for entity_id, count in zip(chosen, shape):
        holdings.extend([entity_id] * count)
    return holdings


def _anchor_of(holdings: list[str]) -> str:
    return Counter(holdings).most_common(1)[0][0]


# --------------------------------------------------------------------------
# Phase A -- portfolio topology (who owns what)
# --------------------------------------------------------------------------


@dataclass
class TopologyMetrics:
    pairwise_overlap: dict[tuple[str, str], int]
    max_overlap: int
    isolated_players: list[str]
    shared_anchor_pairs: list[tuple[str, str]]
    identical_pairs: list[tuple[str, str]]
    heaviest_pair_share: float
    connected_pairs_fraction: float


def _topology_metrics(portfolios: dict[str, list[str]]) -> TopologyMetrics:
    player_ids = list(portfolios)
    pairs = list(combinations(player_ids, 2))
    pairwise_overlap = {(i, j): len(set(portfolios[i]) & set(portfolios[j])) for i, j in pairs}

    max_overlap = max(pairwise_overlap.values()) if pairwise_overlap else 0
    total_overlap = sum(pairwise_overlap.values())
    heaviest_pair_share = (max_overlap / total_overlap) if total_overlap > 0 else 0.0

    connected_pairs = sum(1 for v in pairwise_overlap.values() if v >= 1)
    connected_pairs_fraction = (connected_pairs / len(pairs)) if pairs else 1.0

    degree = dict.fromkeys(player_ids, 0)
    for (i, j), v in pairwise_overlap.items():
        if v >= 1:
            degree[i] += 1
            degree[j] += 1
    isolated_players = [p for p in player_ids if degree[p] == 0]

    anchors = {p: _anchor_of(portfolios[p]) for p in player_ids}
    shared_anchor_pairs = [(i, j) for i, j in pairs if anchors[i] == anchors[j]]
    identical_pairs = [(i, j) for i, j in pairs if set(portfolios[i]) == set(portfolios[j])]

    return TopologyMetrics(
        pairwise_overlap=pairwise_overlap,
        max_overlap=max_overlap,
        isolated_players=isolated_players,
        shared_anchor_pairs=shared_anchor_pairs,
        identical_pairs=identical_pairs,
        heaviest_pair_share=heaviest_pair_share,
        connected_pairs_fraction=connected_pairs_fraction,
    )


def _topology_hard_reject(metrics: TopologyMetrics, config: SetupQualityConfig) -> bool:
    if metrics.shared_anchor_pairs:
        return True
    if metrics.identical_pairs:
        return True
    if metrics.max_overlap > config.max_pair_overlap:
        return True
    if config.reject_isolated_player and metrics.isolated_players:
        return True
    return False


def _topology_score(metrics: TopologyMetrics, config: SetupQualityConfig) -> float:
    return -(config.heaviest_pair_share_weight * metrics.heaviest_pair_share) + (
        config.connectivity_weight * metrics.connected_pairs_fraction
    )


# --------------------------------------------------------------------------
# Phase B -- market geometry (where things sit, for a fixed topology)
# --------------------------------------------------------------------------


@dataclass
class GeometryMetrics:
    per_player: dict[str, dict[str, float]] = field(default_factory=dict)


def _geometry_metrics(portfolios: dict[str, list[str]], position_of: dict[str, int], market_size: int, config: SetupQualityConfig) -> GeometryMetrics:
    elite_cutoff = math.ceil(market_size * config.elite_zone_fraction)
    basement_cutoff = market_size - math.ceil(market_size * config.basement_zone_fraction) + 1
    per_player: dict[str, dict[str, float]] = {}
    for player_id, flat_holdings in portfolios.items():
        distinct = set(flat_holdings)
        positions = [position_of[e] for e in distinct]
        value = sum(market_size - position_of[e] + 1 for e in flat_holdings)  # matches projections._portfolio_value's linear-rank formula
        spread = ((max(positions) - min(positions)) / (market_size - 1)) if market_size > 1 else 0.0
        elite_count = sum(1 for p in positions if p <= elite_cutoff)
        basement_count = sum(1 for p in positions if p >= basement_cutoff)
        per_player[player_id] = {"value": value, "spread": spread, "elite_count": elite_count, "basement_count": basement_count}
    return GeometryMetrics(per_player=per_player)


def _geometry_hard_reject(metrics: GeometryMetrics, config: SetupQualityConfig) -> bool:
    # Elite concentration used to be hard-rejected here (waterline_baseline_v1
    # made a top-heavy portfolio a guaranteed dominant position, "positions
    # 1-4, everyone please go away"). Under Haircut, the top of the scale
    # carries real risk, so top-heavy is no longer *structurally* guaranteed
    # bad -- it moves to a soft penalty (_geometry_score's existing
    # elite_penalty_weight term, already applied below) instead. No hard
    # rejects remain in geometry; kept as a function (returning False) rather
    # than removed so generate_starting_state's call site doesn't need a
    # conditional, and so a future geometry hard-reject has an obvious home.
    return False


def _geometry_score(metrics: GeometryMetrics, config: SetupQualityConfig) -> float:
    if not metrics.per_player:
        return 0.0
    penalties = [
        config.elite_penalty_weight * p["elite_count"] + config.basement_penalty_weight * p["basement_count"] + config.spread_penalty_weight * (1 - p["spread"])
        for p in metrics.per_player.values()
    ]
    return -sum(penalties) / len(penalties)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass
class StartingState:
    market_order: list[str]  # entity_id per position, index 0 = position 1
    portfolios: dict[str, list[str]]  # player_id -> flat holdings list (anchor entity appears twice)
    diagnostics: dict


def _serialize_topology_metrics(metrics: TopologyMetrics) -> dict:
    return {
        "pairwise_overlap": {f"{i}|{j}": v for (i, j), v in metrics.pairwise_overlap.items()},
        "max_overlap": metrics.max_overlap,
        "isolated_players": metrics.isolated_players,
        "shared_anchor_pairs": [list(pair) for pair in metrics.shared_anchor_pairs],
        "identical_pairs": [list(pair) for pair in metrics.identical_pairs],
        "heaviest_pair_share": metrics.heaviest_pair_share,
        "connected_pairs_fraction": metrics.connected_pairs_fraction,
    }


def generate_starting_state(
    theme_set: ThemeSet,
    market_size: int,
    player_ids: list[str],
    shape: list[int],
    config: SetupQualityConfig,
    rng: random.Random,
) -> StartingState:
    entity_defs = select_market_entities(theme_set, market_size, rng)
    entity_ids = [e.theme_key for e in entity_defs]

    # Phase A: portfolio topology. Early-exit loop, not
    # generate-a-fixed-batch-then-filter -- matters at 3P's ~10% yield.
    legal_topologies: list[tuple[dict[str, list[str]], TopologyMetrics, float]] = []
    attempts = 0
    while len(legal_topologies) < config.topology_candidates and attempts < config.max_generation_attempts:
        attempts += 1
        portfolios = {pid: _deal_portfolio(entity_ids, shape, rng) for pid in player_ids}
        metrics = _topology_metrics(portfolios)
        if _topology_hard_reject(metrics, config):
            continue
        legal_topologies.append((portfolios, metrics, _topology_score(metrics, config)))

    if not legal_topologies:
        raise UnableToGenerateStartingStateError(
            f"no legal portfolio topology found in {attempts} attempts for {len(player_ids)} players "
            f"(max_pair_overlap={config.max_pair_overlap}, reject_isolated_player={config.reject_isolated_player})"
        )

    legal_topologies.sort(key=lambda t: t[2], reverse=True)
    band_size = max(1, math.ceil(len(legal_topologies) * config.selection_band_fraction))
    band = legal_topologies[:band_size]
    shortlist = rng.sample(band, min(config.topology_shortlist_size, len(band)))

    # Phase B: market geometry, searched across the whole shortlist -- not
    # just for one declared-best topology.
    pooled: list[tuple[dict[str, list[str]], list[str], TopologyMetrics, GeometryMetrics, float, float, float]] = []
    for portfolios, topo_metrics, topo_score in shortlist:
        geom_attempts = 0
        legal_count = 0
        while legal_count < config.geometry_candidates_per_topology and geom_attempts < config.max_generation_attempts:
            geom_attempts += 1
            permutation = list(entity_ids)
            rng.shuffle(permutation)
            position_of = {eid: i + 1 for i, eid in enumerate(permutation)}
            geom_metrics = _geometry_metrics(portfolios, position_of, market_size, config)
            if _geometry_hard_reject(geom_metrics, config):
                continue
            geom_score = _geometry_score(geom_metrics, config)
            pooled.append((portfolios, permutation, topo_metrics, geom_metrics, topo_score, geom_score, topo_score + geom_score))
            legal_count += 1
        # A topology yielding zero legal permutations within its own budget
        # is simply dropped from the pool, not treated as a failure on its
        # own -- only an empty *pooled* result across the whole shortlist
        # triggers the explicit failure below.

    if not pooled:
        # Geometry no longer hard-rejects anything (see _geometry_hard_reject) --
        # this branch is effectively unreachable now that shortlist non-empty
        # implies pooled non-empty, but kept as a defensive guard rather than
        # assumed away, in case a future geometry hard-reject is reintroduced.
        raise UnableToGenerateStartingStateError(
            f"no legal complete starting state found across {len(shortlist)} shortlisted topologies"
        )

    pooled.sort(key=lambda t: t[6], reverse=True)
    final_band_size = max(1, math.ceil(len(pooled) * config.selection_band_fraction))
    final_band = pooled[:final_band_size]
    portfolios, market_order, topo_metrics, geom_metrics, topo_score, geom_score, combined_score = rng.choice(final_band)

    diagnostics = {
        "setup_quality_version": SETUP_QUALITY_VERSION,
        "player_count": len(player_ids),
        "config": config.model_dump(mode="json"),
        "topology_metrics": _serialize_topology_metrics(topo_metrics),
        "geometry_metrics": geom_metrics.per_player,
        "topology_score": topo_score,
        "geometry_score": geom_score,
        "combined_score": combined_score,
        "candidate_counts": {
            "topology_legal": len(legal_topologies),
            "topology_shortlisted": len(shortlist),
            "geometry_legal_total": len(pooled),
            "selected_band_size": len(final_band),
        },
    }
    return StartingState(market_order=market_order, portfolios=portfolios, diagnostics=diagnostics)
