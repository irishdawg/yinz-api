"""project(state, audience) — domain model §03/§06. The one place visibility
rules get enforced; every client view, live or replay, is this same function
called with a different audience. Audience is never client-supplied — the
caller (api/routes.py) derives it from the verified JWT and game.phase.

Cadence/economy redesign (prototype branch): checkpoint 1 stripped every
Influence, Market Correction, gameplay-clock, Accept Lock, multi-accept
-threshold, and old Reserve/Pickup field/branch out of the projection (the
pending-pickup frozen-view short-circuit is removed along with it -- its
*pattern* returns in a later checkpoint for Boosts, under a different
name). Checkpoint 2 makes Pass fully public: a passed proposal/pool is no
longer omitted from the passer's own view, and passed_player_ids is now
exposed to everyone, not just an anonymous count to the proposer.
Checkpoint 3 adds Arbitration: a pending arbitration's existence, caller,
and timing are public, but its weights and its jury's votes are never
shown to any live audience -- not even the two active participants --
only that a given juror *has* voted. ReplayAudience unlocks all of it,
same "postgame reveals everything" precedent as Pool contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from gotiate.domain import themes
from gotiate.domain.entities import (
    Game,
    GamePhase,
    GamePlayer,
    Holding,
    HoldingZone,
    Pool,
    PoolResolutionReason,
    PoolVisibility,
    Proposal,
    ResolutionStatus,
)
from gotiate.domain.events import EventType, GameEvent
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

    scored = game.phase == GamePhase.SCORED
    lookup = _theme_lookup(game)

    view: dict = {
        "game_id": game.id,
        "version": game.version,
        "phase": game.phase.value,
        "join_code": game.join_code if isinstance(audience, PlayerAudience) else None,
        # Who's hosting is public narrative, same as a pool's initiator_id --
        # the client needs it to show a host-only Start button.
        "host_player_id": game.host_player_id,
        # Which roster entry is "me" -- never inferred client-side from
        # matching some other identifier, just handed over directly.
        "you": audience.game_player_id if isinstance(audience, PlayerAudience) else None,
        # LOBBY-only. Raw deadline + the grace duration, not a precomputed
        # countdown -- same pattern as started_at below, the client derives
        # its own display from authoritative timestamps.
        "lobby_reminder_deadline_at": game.lobby_reminder_deadline_at,
        "lobby_reminder_grace_seconds": game.config.lobby_reminder_grace_seconds,
        "started_at": game.started_at,
        # The profile's contents: hidden until haircut_profile_revealed_at,
        # or unconditionally once scored (a game that closes via the
        # ready-threshold before the live reveal trigger fires never sees
        # it live -- this is the fallback that still exposes it, since
        # compute_final_scores' result depends on it being knowable).
        "haircut_profile": (
            {"depth_probabilities": game.haircut_profile.depth_probabilities}
            if game.haircut_profile is not None and (game.haircut_profile_revealed_at is not None or scored)
            else None
        ),
        # Unconditionally public from game start, unlike haircut_profile
        # itself -- computed straight from config (round(market_size *
        # risk_depth_fraction), the same formula
        # engine._generate_random_haircut_profile targets), deliberately
        # NOT game.haircut_profile.max_depth. Reading the profile's own
        # shape here would leak exactly how deep the real risk goes before
        # the reveal.
        "haircut_risk_band_depth": (
            round(len(game.market) * game.config.risk_depth_fraction) if game.haircut_profile is not None else None
        ),
        # Only meaningful once phase is CANCELLED -- null otherwise.
        "cancellation_reason": game.cancellation_reason.value if game.cancellation_reason else None,
        # The *fact* of why/when the market closed, revealed only at the
        # instant it actually happens -- not a leading indicator. Distinct
        # from ready_to_close itself, which stays strictly self-only with
        # no aggregate anywhere (see _project_player) -- deliberately kept
        # a secret trigger, not a public countdown.
        "close_reason": game.close_reason.value if game.close_reason else None,
        "closed_at": game.closed_at,
        # Public and unconditional -- at most one bare negotiation open
        # table-wide at any time; null whenever the table is open for
        # anyone with Moves left to seize initiative.
        "active_proposal_id": game.active_proposal_id,
        # Public and unconditional, flips once, permanently -- every Boost
        # control should disable table-wide the instant this is true.
        "boosts_expired": game.boosts_expired,
        "market": _project_market(game, lookup),
        "players": [_project_player(game, p, audience) for p in game.players],
        # NOTE (cadence/economy redesign): a passed proposal/pool is no
        # longer omitted from the passer's own view -- Pass is public and
        # narrows the active participant set visibly, it doesn't make the
        # negotiation disappear. The engine-side legality checks (can't
        # accept/pool after passing) still enforce that a passed player
        # can no longer act on it.
        "proposals": [_project_proposal(game, p, audience) for p in game.proposals.values()],
        "pools": [_project_pool(game, pool, audience) for pool in game.pools.values()],
    }

    if scored:
        from gotiate.domain.engine import compute_final_scores  # local import avoids a cycle at module load

        # Reads the already-persisted realized_haircut_depth, never draws a
        # fresh one -- compute_final_scores is pure/rng-free specifically so
        # this call and the one that built the original GAME_SCORED payload
        # always agree. realized_haircut_depth is None for any game that
        # reached SCORED under the retired waterline_baseline_v1 model
        # (before this field existed) -- that data is gone, not
        # recoverable, so this merge is skipped entirely rather than
        # asserted/crashed on.
        if game.realized_haircut_depth is not None:
            view.update(compute_final_scores(game, game.realized_haircut_depth))
        view["holdings"] = [_holding_view(h, lookup) for h in game.holdings.values()]
    elif isinstance(audience, PlayerAudience):
        # Live, to the owner themselves: portfolio holdings are always
        # known, always shown. (There is no unrevealed-to-owner zone left
        # in checkpoint 1 -- that redaction returns in a later checkpoint
        # once Boosts introduce a new frozen-decision holding.)
        view["holdings"] = [
            _holding_view(h, lookup) for h in game.holdings.values() if h.owner_player_id == audience.game_player_id
        ]

    return view


class EventVisibility(StrEnum):
    """Every EventType maps to exactly one of these -- see EVENT_VISIBILITY.
    Deliberately not the same shape as pool/holding visibility (no
    per-instance content redaction except POOL_INSIDERS) -- most events are
    either wholly public or wholly hidden to a given audience."""

    PUBLIC = "public"
    ACTOR_ONLY = "actor_only"
    # Never shown live, to anyone, including the actor -- only ReplayAudience
    # (post-SCORED) ever sees these. Distinct from ACTOR_ONLY: there is no
    # live audience this is visible to at all.
    SERVER_ONLY = "server_only"
    # Existence/initiator/status-style fields stay public regardless (same
    # rule _project_pool already applies) -- only entity_c/entity_d, the
    # actual swap contents, get stripped for non-insiders.
    POOL_INSIDERS = "pool_insiders"


# Explicit, exhaustive, default-deny: an EventType with no entry here is
# omitted from project_events entirely (see the `is None: continue` below),
# not shown by default. test_event_visibility.py asserts this dict's keys
# equal set(EventType) exactly, so adding a new event type without an
# explicit visibility decision fails the test suite, not just code review.
EVENT_VISIBILITY: dict[EventType, EventVisibility] = {
    EventType.GAME_CREATED: EventVisibility.PUBLIC,
    EventType.PLAYER_JOINED: EventVisibility.PUBLIC,
    EventType.LOBBY_TIMER_EXTENDED: EventVisibility.PUBLIC,
    EventType.GAME_STARTED: EventVisibility.PUBLIC,
    EventType.MARKET_INITIALIZED: EventVisibility.PUBLIC,
    # Empty payloads today, one event for the whole game rather than one per
    # player -- there's no actor to key an ACTOR_ONLY/OWNER-style policy on,
    # and what actually got dealt is already visible per-owner through
    # project()'s own holdings redaction. SERVER_ONLY keeps that the only
    # path to this information, so a future payload addition here doesn't
    # silently start leaking through the event log instead.
    EventType.PORTFOLIO_DEALT: EventVisibility.SERVER_ONLY,
    # Hidden until the live reveal trigger (engine._maybe_reveal_haircut).
    EventType.HAIRCUT_PROFILE_SELECTED: EventVisibility.SERVER_ONLY,
    # Fires once, table-wide, the instant the reveal trigger crosses --
    # nothing per-player to redact.
    EventType.HAIRCUT_RISK_REVEALED: EventVisibility.PUBLIC,
    # Fires once, table-wide, the instant boosts_expired flips -- nothing
    # per-player to redact.
    EventType.BOOSTS_EXPIRED: EventVisibility.PUBLIC,
    EventType.PROPOSAL_CREATED: EventVisibility.PUBLIC,
    EventType.PROPOSAL_RESOLVED: EventVisibility.PUBLIC,
    # Fully public -- a Pass is now a visible, intentional information leak.
    EventType.PROPOSAL_PASSED: EventVisibility.PUBLIC,
    # Either active participant already knows who called it; this is the
    # irreversible starting gun for the whole 20s window.
    EventType.ARBITRATION_CALLED: EventVisibility.PUBLIC,
    EventType.ARBITRATION_POOL_REVEALED: EventVisibility.PUBLIC,
    # The vote's own content lives in the payload but stays actor-only live
    # -- same shape as READY_TO_CLOSE_CHANGED. Only "a juror has voted,"
    # never which choice, is separately projected to everyone else -- see
    # _project_proposal's voted_player_ids.
    EventType.ARBITRATION_VOTE_CAST: EventVisibility.ACTOR_ONLY,
    # PUBLIC at the table-driven policy level (the outcome reason and any
    # resulting SWAP_EXECUTED are already publicly observable regardless),
    # but project_events applies its own bespoke redaction below, stripping
    # base_weights/final_weights/votes from every live audience -- not just
    # non-insiders, the two active participants included -- unlocked only
    # for ReplayAudience.
    EventType.ARBITRATION_RESOLVED: EventVisibility.PUBLIC,
    # Payload carries entity_c/entity_d directly -- the actual swap.
    EventType.PRIVATE_POOL_CREATED: EventVisibility.POOL_INSIDERS,
    EventType.PUBLIC_POOL_CREATED: EventVisibility.PUBLIC,
    EventType.POOL_MADE_PUBLIC: EventVisibility.PUBLIC,
    # Checked against the real payload, not assumed: POOL_RESOLVED only ever
    # carries pool_id/reason, nothing to redact.
    EventType.POOL_RESOLVED: EventVisibility.PUBLIC,
    EventType.SWAP_EXECUTED: EventVisibility.PUBLIC,
    EventType.READY_TO_CLOSE_CHANGED: EventVisibility.ACTOR_ONLY,
    EventType.CLOSE_THRESHOLD_REACHED: EventVisibility.PUBLIC,
    EventType.MARKET_CLOSED: EventVisibility.PUBLIC,
    EventType.PORTFOLIOS_REVEALED: EventVisibility.PUBLIC,
    EventType.GAME_SCORED: EventVisibility.PUBLIC,
    EventType.GAME_ENDED: EventVisibility.PUBLIC,
    EventType.GAME_CANCELLED: EventVisibility.PUBLIC,
}

_POOL_CONTENT_KEYS = frozenset({"entity_c", "entity_d"})
_ARBITRATION_RESOLVED_SECRET_KEYS = frozenset({"base_weights", "final_weights", "votes"})


def project_events(game: Game, events: Iterable[GameEvent], audience: Audience) -> list[dict]:
    """The activity-log counterpart to project() -- same audience-driven
    visibility discipline, applied per event instead of per current-state
    field. ReplayAudience (only reachable once SCORED, per project()'s own
    guard) bypasses every policy, same shape as _project_pool's is_replay
    carve-out."""
    is_replay = isinstance(audience, ReplayAudience)
    views: list[dict] = []
    for event in events:
        policy = EVENT_VISIBILITY.get(event.type)
        if policy is None:
            continue  # unknown event type -- fail safe, omit rather than default to public
        if is_replay:
            views.append(_event_view(event))
            continue
        if policy is EventVisibility.SERVER_ONLY:
            continue
        if policy is EventVisibility.ACTOR_ONLY:
            if isinstance(audience, PlayerAudience) and audience.game_player_id == event.actor_game_player_id:
                views.append(_event_view(event))
            continue
        if policy is EventVisibility.POOL_INSIDERS:
            pool = game.pools.get(event.payload.get("pool_id"))
            can_see_contents = pool is not None and (
                pool.visibility == PoolVisibility.PUBLIC
                or _pool_executed(pool)
                or (isinstance(audience, PlayerAudience) and audience.game_player_id in _pool_insiders(game, pool))
            )
            views.append(_event_view(event, redact=() if can_see_contents else _POOL_CONTENT_KEYS))
            continue
        if event.type is EventType.ARBITRATION_RESOLVED:
            # Never shown live, to anyone, including the two active
            # participants -- "the jury can lean on the machine, never
            # become it" only holds if the weights/votes stay invisible
            # during play. is_replay already returned above.
            views.append(_event_view(event, redact=_ARBITRATION_RESOLVED_SECRET_KEYS))
            continue
        views.append(_event_view(event))  # PUBLIC
    return views


def _event_view(event: GameEvent, *, redact: Iterable[str] = ()) -> dict:
    redact_set = set(redact)
    return {
        "seq_no": event.seq_no,
        "type": event.type.value,
        "actor_game_player_id": event.actor_game_player_id,
        "payload": {k: v for k, v in event.payload.items() if k not in redact_set},
        "created_at": event.created_at,
    }


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
        # Rolled at seating, not at name-preview time -- always public, same
        # as any other roster fact. The entire point of it being rare is
        # that other players can see it, not just the player themselves.
        "is_golden_name": player.is_golden_name,
        # Public roster facts -- same tier as reserve_count_remaining used
        # to be: players need to see who can still seize initiative
        # (Moves) or still has a unilateral card to play (Boosts).
        "moves_remaining": player.moves_remaining,
        "boosts_remaining": player.boosts_remaining,
    }

    # Ready-to-close: owner-only live, never the aggregate — see visibility §06.
    if is_self:
        out["ready_to_close"] = player.ready_to_close
    elif is_replay and game.config.ready_to_close_revealed_in_replay:
        out["ready_to_close"] = player.ready_to_close

    if is_self or is_replay:
        out["projected_value"] = _portfolio_value(game, player.game_player_id)
        # safe_value only means anything once the profile is visible.
        # Derived structurally from the profile's max_depth, not a
        # certainty == 1.0 float comparison.
        if _haircut_visible(game):
            out["safe_value"] = _safe_value(game, player.game_player_id)

    return out


def _haircut_visible(game: Game) -> bool:
    return game.haircut_profile is not None and (game.haircut_profile_revealed_at is not None or game.phase == GamePhase.SCORED)


def _safe_value(game: Game, game_player_id: str) -> int:
    assert game.haircut_profile is not None
    max_depth = game.haircut_profile.max_depth
    n = len(game.market)
    positions = {eid: m.position for eid, m in game.market.items()}
    holdings = [h for h in game.holdings.values() if h.owner_player_id == game_player_id and h.zone == HoldingZone.PORTFOLIO]
    return sum(n - positions[h.entity_id] + 1 for h in holdings if positions[h.entity_id] > max_depth)


def _project_proposal(game: Game, proposal: Proposal, audience: Audience) -> dict:
    out: dict = {
        "proposal_id": proposal.proposal_id,
        "entity_a": proposal.swap.entity_a,
        "entity_b": proposal.swap.entity_b,
        # Locked at authoring time, unconditionally public -- bare
        # proposal entities are always public, so their locked direction
        # is too. The frontend must pin Visualize's arrows to this, never
        # recompute from current positions.
        "rising_entity_id": proposal.swap.rising_entity_id,
        "proposer_id": proposal.swap.initiator_player_id,
        "status": proposal.status.value,
        "resolution_reason": proposal.resolution_reason.value if proposal.resolution_reason else None,
        # Fully public, identities and all -- Pass is a visible,
        # intentional information leak that narrows the active
        # participant set for everyone to see. Empty until anyone passes.
        "passed_player_ids": sorted(proposal.passed_player_ids),
        "pending_arbitration": _project_pending_arbitration(proposal),
    }
    return out


def _project_pending_arbitration(proposal: Proposal) -> dict | None:
    """Existence, caller, and timing are public the instant Arbitration is
    called -- the two active participants already know, and everyone else
    needs to see the irreversible countdown start. voted_player_ids is
    "who has voted," never what -- see EVENT_VISIBILITY's own
    ARBITRATION_VOTE_CAST/ARBITRATION_RESOLVED entries for where the actual
    content stays hidden. base_weights/final_weights/votes are
    deliberately never included here at all, live or replay -- Replay
    reads them from the ARBITRATION_RESOLVED event's own (unredacted for
    ReplayAudience) payload instead, once resolved."""
    pending = proposal.pending_arbitration
    if pending is None:
        return None
    return {
        "arbitration_id": pending.arbitration_id,
        "called_by": pending.called_by,
        "caller_role": "originator" if pending.called_by == proposal.swap.initiator_player_id else "other",
        "resolves_at": pending.resolves_at,
        "eligible_pool_id": pending.eligible_pool_id,
        "voted_player_ids": sorted(pending.votes.keys()),
    }


def _project_pool(game: Game, pool: Pool, audience: Audience) -> dict:
    is_replay = isinstance(audience, ReplayAudience)
    can_see_contents = (
        pool.visibility == PoolVisibility.PUBLIC
        or is_replay
        or _pool_executed(pool)
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
        # Same visibility tier as entity_c/entity_d -- direction is
        # content, not metadata, for a still-private pool.
        out["rising_entity_id"] = pool.swap.rising_entity_id
    return out


def _pool_executed(pool: Pool) -> bool:
    """A private Pool stays private forever if it's declined, withdrawn,
    preempted, or otherwise never executes -- but the instant it executes,
    its contents become public as part of that executed transaction and
    thereafter contribute to activity history and support markers, same
    as a bare proposal. Checked against current pool state, not the event
    that's being rendered, so both PRIVATE_POOL_CREATED and POOL_RESOLVED
    correctly flip to visible together once this becomes true."""
    return pool.status == ResolutionStatus.RESOLVED and pool.resolution_reason == PoolResolutionReason.EXECUTED


def _pool_insiders(game: Game, pool: Pool) -> set[str]:
    insiders = {pool.swap.initiator_player_id}
    base = game.proposals.get(pool.base_proposal_id)
    if base is not None:
        insiders.add(base.swap.initiator_player_id)
    return insiders


def _holding_view(h: Holding, lookup: dict[str, ThemeEntityDefinition], *, reveal: bool = True) -> dict:
    if not reveal:
        return {
            "holding_id": h.holding_id,
            "entity_id": None,
            "owner_player_id": h.owner_player_id,
            "zone": h.zone.value,
            "display_name": None,
            "ticker_symbol": None,
            "logo_url": None,
        }
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
