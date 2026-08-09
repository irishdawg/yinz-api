"""project(state, audience) — domain model §03/§06. The one place visibility
rules get enforced; every client view, live or replay, is this same function
called with a different audience. Audience is never client-supplied — the
caller (api/routes.py) derives it from the verified JWT and game.phase."""

from __future__ import annotations

from dataclasses import dataclass

from gotiate.domain import themes
from gotiate.domain.entities import (
    Game,
    GamePhase,
    GamePlayer,
    Holding,
    HoldingZone,
    Pool,
    PoolVisibility,
    Proposal,
)
from gotiate.domain.themes import ThemeEntityDefinition


@dataclass(frozen=True)
class PlayerAudience:
    game_player_id: str


@dataclass(frozen=True)
class PublicAudience:
    pass


@dataclass(frozen=True)
class ReplayAudience:
    pass


Audience = PlayerAudience | PublicAudience | ReplayAudience


def project(game: Game, audience: Audience) -> dict:
    if isinstance(audience, ReplayAudience) and game.phase != GamePhase.SCORED:
        raise PermissionError("replay is only available once the game is scored")

    if isinstance(audience, PlayerAudience):
        player = game.player_by_id(audience.game_player_id)
        if player.pending_pickup is not None:
            # Frozen view: rendered once at PICK_UP_RESERVE time, re-served
            # verbatim until the pickup resolves. Not recomputed against
            # current state — see domain model §03.
            return player.pending_pickup.cached_view

    scored = game.phase == GamePhase.SCORED
    lookup = _theme_lookup(game)

    view: dict = {
        "game_id": game.id,
        "version": game.version,
        "phase": game.phase.value,
        "join_code": game.join_code if isinstance(audience, PlayerAudience) else None,
        "expected_player_count": game.expected_player_count,
        "market": _project_market(game, lookup),
        "players": [_project_player(game, p, audience) for p in game.players],
        "proposals": [_project_proposal(p) for p in game.proposals.values()],
        "pools": [_project_pool(game, pool, audience) for pool in game.pools.values()],
    }

    if scored:
        view["waterline_entity_id"] = game.waterline_entity_id
        view["holdings"] = [_holding_view(h, lookup) for h in game.holdings.values()]
    elif isinstance(audience, PlayerAudience):
        view["holdings"] = [_holding_view(h, lookup) for h in game.holdings.values() if h.owner_player_id == audience.game_player_id]

    return view


def _theme_lookup(game: Game) -> dict[str, ThemeEntityDefinition]:
    """Resolved live from the game's ThemeSet, not stored on MarketEntity —
    see entities.MarketEntity.theme_key."""
    theme_set = themes.get_theme_set(game.config.theme_set_id)
    return {e.theme_key: e for e in theme_set.entities}


def _entity_view(theme_key: str, lookup: dict[str, ThemeEntityDefinition]) -> dict:
    entity = lookup.get(theme_key)
    if entity is None:
        return {"display_name": theme_key, "ticker_symbol": theme_key[:4].upper(), "logo_url": None}
    return {"display_name": entity.display_name, "ticker_symbol": entity.ticker_symbol, "logo_url": entity.logo_url}


def _project_market(game: Game, lookup: dict[str, ThemeEntityDefinition]) -> list[dict]:
    return sorted(
        (
            {"entity_id": m.entity_id, "theme_key": m.theme_key, "position": m.position, **_entity_view(m.theme_key, lookup)}
            for m in game.market.values()
        ),
        key=lambda x: x["position"],
    )


def _project_player(game: Game, player: GamePlayer, audience: Audience) -> dict:
    is_self = isinstance(audience, PlayerAudience) and audience.game_player_id == player.game_player_id
    is_replay = isinstance(audience, ReplayAudience)

    out: dict = {
        "game_player_id": player.game_player_id,
        "seat": player.seat,
        "display_name": player.display_name,
        "influence": {"available": player.influence_available, "committed": player.influence_committed, "spent": player.influence_spent},
        "reserve_count_remaining": sum(
            1 for h in game.holdings.values() if h.owner_player_id == player.game_player_id and h.zone == HoldingZone.RESERVE_UNREVEALED
        ),
    }

    # Ready-to-close: owner-only live, never the aggregate — see visibility §06.
    if is_self:
        out["ready_to_close"] = player.ready_to_close
    elif is_replay and game.config.ready_to_close_revealed_in_replay:
        out["ready_to_close"] = player.ready_to_close

    if is_self or is_replay:
        out["portfolio_value"] = _portfolio_value(game, player.game_player_id)

    return out


def _project_proposal(proposal: Proposal) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "entity_a": proposal.swap.entity_a,
        "entity_b": proposal.swap.entity_b,
        "proposer_id": proposal.swap.initiator_player_id,
        "status": proposal.status.value,
        "resolution_reason": proposal.resolution_reason.value if proposal.resolution_reason else None,
    }


def _project_pool(game: Game, pool: Pool, audience: Audience) -> dict:
    is_replay = isinstance(audience, ReplayAudience)
    can_see_contents = (
        pool.visibility == PoolVisibility.PUBLIC
        or is_replay
        or (isinstance(audience, PlayerAudience) and audience.game_player_id in _pool_insiders(game, pool))
    )
    out: dict = {
        "pool_id": pool.pool_id,
        "base_proposal_id": pool.base_proposal_id,
        "visibility": pool.visibility.value,
        # The pool's existence and *who* created it are always public, per
        # §06 — "Mortia privately pooled" is public narrative even when the
        # swap itself isn't.
        "initiator_id": pool.swap.initiator_player_id,
        "status": pool.status.value,
        "resolution_reason": pool.resolution_reason.value if pool.resolution_reason else None,
    }
    if can_see_contents:
        out["entity_c"] = pool.swap.entity_a
        out["entity_d"] = pool.swap.entity_b
    return out


def _pool_insiders(game: Game, pool: Pool) -> set[str]:
    insiders = {pool.swap.initiator_player_id}
    base = game.proposals.get(pool.base_proposal_id)
    if base is not None:
        insiders.add(base.swap.initiator_player_id)
    return insiders


def _holding_view(h: Holding, lookup: dict[str, ThemeEntityDefinition]) -> dict:
    return {
        "holding_id": h.holding_id,
        "entity_id": h.entity_id,
        "owner_player_id": h.owner_player_id,
        "zone": h.zone.value,
        **_entity_view(h.entity_id, lookup),
    }


def _portfolio_value(game: Game, game_player_id: str) -> int:
    """linear_rank_v1 — higher-ranked entities are worth more. Pluggable in
    name only for now; a real policy registry is future work."""
    n = len(game.market)
    positions = {eid: m.position for eid, m in game.market.items()}
    holdings = [h for h in game.holdings.values() if h.owner_player_id == game_player_id and h.zone == HoldingZone.PORTFOLIO]
    return sum(n - positions[h.entity_id] + 1 for h in holdings)
