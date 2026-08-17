"""project(state, audience) — domain model §03/§06. The one place visibility
rules get enforced; every client view, live or replay, is this same function
called with a different audience. Audience is never client-supplied — the
caller (api/routes.py) derives it from the verified JWT and game.phase."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Iterable

from gotiate.domain import themes
from gotiate.domain.entities import (
    Game,
    GamePhase,
    GamePlayer,
    Holding,
    HoldingZone,
    MarketCorrectionResolutionReason,
    Pool,
    PoolResolutionReason,
    PoolVisibility,
    Proposal,
    ProposalResolutionReason,
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
        # Who's hosting is public narrative, same as a pool's initiator_id --
        # the client needs it to show a host-only Start button.
        "host_player_id": game.host_player_id,
        # Which roster entry is "me" -- never inferred client-side from
        # matching some other identifier, just handed over directly.
        "you": audience.game_player_id if isinstance(audience, PlayerAudience) else None,
        # LOBBY-only. Raw deadline + the grace duration, not a precomputed
        # countdown -- same pattern as started_at/max_duration_s below, the
        # client derives its own display from authoritative timestamps.
        "lobby_reminder_deadline_at": game.lobby_reminder_deadline_at,
        "lobby_reminder_grace_seconds": game.config.lobby_reminder_grace_seconds,
        # Set together at START_GAME (engine._handle_start_game) -- all
        # three are always non-null once phase is NEGOTIATION or later.
        # Public: the clock and the unilateral-window cutoff are shared
        # facts about the match, not private state.
        "started_at": game.started_at,
        "max_duration_s": game.max_duration_s,
        "unilateral_cutoff_at": game.unilateral_cutoff_at,
        # Public once offered, but only {correction_id, expires_at} --
        # never the moves/displacement. See the Market Correction design
        # writeup: the constitutional rule is public, the contents aren't
        # until it triggers.
        "pending_market_correction": (
            {"correction_id": game.pending_market_correction.correction_id, "expires_at": game.pending_market_correction.expires_at}
            if game.pending_market_correction is not None
            else None
        ),
        # Public once set (same tier as unilateral_cutoff_at) -- the
        # deadline itself isn't a secret, only the profile's contents are
        # until it passes. See the Haircut-risk design writeup.
        "haircut_reveal_at": game.haircut_reveal_at,
        # The profile's contents: hidden until haircut_profile_revealed_at,
        # or unconditionally once scored (a game that closes via the
        # ready-threshold before the halfway mark never lives to see the
        # live reveal fire -- this is the fallback that still exposes it,
        # since compute_final_scores' result depends on it being knowable).
        "haircut_profile": (
            {"depth_probabilities": game.haircut_profile.depth_probabilities}
            if game.haircut_profile is not None and (game.haircut_profile_revealed_at is not None or scored)
            else None
        ),
        # Unconditionally public from game start, unlike haircut_profile
        # itself -- every profile configured for this player count is
        # validated at config load to share the exact same max_depth (see
        # GameConfig's model_validator), so "the top K positions carry some
        # risk" is structural, not profile-specific information. Revealing
        # the boundary doesn't reveal the profile's shape (how risky each
        # of those K positions actually is), matching the design's "some
        # risk, not its shape" pre-reveal framing. Lets the client render a
        # payout-chance row keyed by *position*, not by whichever entity
        # currently occupies it -- positions beyond this depth are 100%
        # safe from the very first render, not just after reveal.
        "haircut_risk_band_depth": game.haircut_profile.max_depth if game.haircut_profile is not None else None,
        # Only meaningful once phase is CANCELLED -- null otherwise.
        "cancellation_reason": game.cancellation_reason.value if game.cancellation_reason else None,
        # The *fact* of why/when the market closed, revealed only at the
        # instant it actually happens -- not a leading indicator. Distinct
        # from ready_to_close itself, which stays strictly self-only with
        # no aggregate anywhere (see _project_player) -- deliberately kept
        # a secret trigger, not a public countdown. MARKET_CLOSED's event
        # payload already carries this reason publicly; this just exposes
        # the same already-public fact as a current-state field too.
        "close_reason": game.close_reason.value if game.close_reason else None,
        "closed_at": game.closed_at,
        "market": _project_market(game, lookup),
        "players": [_project_player(game, p, audience) for p in game.players],
        # A proposal (and every Pool attached to it) is omitted entirely
        # -- not hidden client-side -- from the live view of any player
        # who has PASS_PROPOSAL'd it. This is the actual enforcement of
        # "this negotiation ceases to exist in my world"; the engine-side
        # legality checks (can't accept/pool after passing) are the
        # backstop, this is what makes it true even before a player tries
        # to act. See the Pass design writeup.
        "proposals": [_project_proposal(game, p, audience) for p in game.proposals.values() if not _proposal_passed_by(p, audience)],
        "pools": [
            _project_pool(game, pool, audience)
            for pool in game.pools.values()
            if not _proposal_passed_by(game.proposals.get(pool.base_proposal_id), audience)
        ],
    }

    if scored:
        from gotiate.domain.engine import compute_final_scores  # local import avoids a cycle at module load

        # Reads the already-persisted realized_haircut_depth, never draws a
        # fresh one -- compute_final_scores is pure/rng-free specifically so
        # this call and the one that built the original GAME_SCORED payload
        # always agree (see the Haircut-risk design writeup's invariant).
        # realized_haircut_depth is None for any game that reached SCORED
        # under the retired waterline_baseline_v1 model (before this field
        # existed) -- that data is gone, not recoverable, so this merge is
        # skipped entirely rather than asserted/crashed on, same as before
        # results/winners existed in the projection at all. Real production
        # incident, not a hypothetical: a live pre-migration SCORED game hit
        # exactly this path and 500'd on every poll until this guard landed.
        if game.realized_haircut_depth is not None:
            view.update(compute_final_scores(game, game.realized_haircut_depth))
        view["holdings"] = [_holding_view(h, lookup) for h in game.holdings.values()]
    elif isinstance(audience, PlayerAudience):
        # Live, to the owner themselves: a reserve's identity is redacted
        # until it's actually been revealed to them (Pick Up, or a failed
        # pickup that already showed it) -- "you don't know your reserve
        # until you Pick Up" is a rule enforced here, not left to the
        # frontend's discretion. A curious player's own devtools shouldn't
        # be able to see what a burned-unseen or still-unrevealed reserve
        # is before the mechanic reveals it. Portfolio holdings are always
        # known, always shown.
        view["holdings"] = [
            _holding_view(h, lookup, reveal=h.zone not in _UNREVEALED_TO_OWNER_ZONES)
            for h in game.holdings.values()
            if h.owner_player_id == audience.game_player_id
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
    EventType.RESERVES_DEALT: EventVisibility.SERVER_ONLY,
    # Hidden until haircut_reveal_at, same treatment the old WATERLINE_SELECTED
    # got -- the live reveal itself is HAIRCUT_RISK_REVEALED below, not this.
    EventType.HAIRCUT_PROFILE_SELECTED: EventVisibility.SERVER_ONLY,
    EventType.PROPOSAL_CREATED: EventVisibility.PUBLIC,
    EventType.PROPOSAL_RESOLVED: EventVisibility.PUBLIC,
    # Own pass only, in the passer's own history -- the proposer's only
    # channel to Pass feedback is the anonymous, aggregate passed_count on
    # the live proposal (project()/_project_proposal), never this event.
    EventType.PROPOSAL_PASSED: EventVisibility.ACTOR_ONLY,
    # Payload carries entity_c/entity_d directly -- the actual swap.
    EventType.PRIVATE_POOL_CREATED: EventVisibility.POOL_INSIDERS,
    EventType.PUBLIC_POOL_CREATED: EventVisibility.PUBLIC,
    EventType.POOL_MADE_PUBLIC: EventVisibility.PUBLIC,
    # Checked against the real payload, not assumed: POOL_RESOLVED only ever
    # carries pool_id/reason/influence, never entity_c/entity_d -- and
    # influence is already unconditionally public via _project_player. PUBLIC
    # is correct here, not POOL_INSIDERS (nothing to redact).
    EventType.POOL_RESOLVED: EventVisibility.PUBLIC,
    EventType.SWAP_EXECUTED: EventVisibility.PUBLIC,
    # PICKUP_STARTED's payload reveals revealed_entity_id; COMPLETED/FAILED
    # don't carry an entity_id but are kept ACTOR_ONLY too for a consistent
    # policy across the whole pickup family rather than splitting hairs
    # field-by-field.
    EventType.PICKUP_STARTED: EventVisibility.ACTOR_ONLY,
    EventType.PICKUP_COMPLETED: EventVisibility.ACTOR_ONLY,
    EventType.PICKUP_FAILED: EventVisibility.ACTOR_ONLY,
    EventType.RESERVE_BURNED_FOR_SWAP: EventVisibility.SERVER_ONLY,
    EventType.READY_TO_CLOSE_CHANGED: EventVisibility.ACTOR_ONLY,
    EventType.HAIRCUT_RISK_REVEALED: EventVisibility.PUBLIC,
    # Both PUBLIC -- OFFERED's payload is minimal by construction (never
    # entities/displacement), and RESOLVED gets a bespoke special-case in
    # project_events (below) redacting `moves` unless reason=="triggered",
    # same shape as PROPOSAL_RESOLVED's EXPIRED_ALL_PASSED masking. See
    # the Market Correction design writeup.
    EventType.MARKET_CORRECTION_OFFERED: EventVisibility.PUBLIC,
    EventType.MARKET_CORRECTION_RESOLVED: EventVisibility.PUBLIC,
    EventType.UNILATERAL_WINDOW_CLOSED: EventVisibility.PUBLIC,
    EventType.CLOSE_THRESHOLD_REACHED: EventVisibility.PUBLIC,
    EventType.MARKET_CLOSED: EventVisibility.PUBLIC,
    EventType.PORTFOLIOS_REVEALED: EventVisibility.PUBLIC,
    EventType.GAME_SCORED: EventVisibility.PUBLIC,
    EventType.GAME_ENDED: EventVisibility.PUBLIC,
    EventType.GAME_CANCELLED: EventVisibility.PUBLIC,
    # Applies identically to every player -- nothing per-player to redact,
    # same reasoning as MARKET_CLOSED.
    EventType.INFLUENCE_TOPPED_UP: EventVisibility.PUBLIC,
}

_POOL_CONTENT_KEYS = frozenset({"entity_c", "entity_d"})


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
        if event.type is EventType.PROPOSAL_RESOLVED:
            # Same masking _public_resolution_reason applies to the live
            # proposal projection -- EXPIRED_ALL_PASSED must never appear
            # live, in the event log any more than in the proposal's own
            # current-state view. See the Pass design writeup.
            view = _event_view(event)
            if view["payload"].get("reason") == ProposalResolutionReason.EXPIRED_ALL_PASSED.value:
                view = {**view, "payload": {**view["payload"], "reason": ProposalResolutionReason.WITHDRAWN_BY_INITIATOR.value}}
            views.append(view)
            continue
        if event.type is EventType.MARKET_CORRECTION_RESOLVED:
            # The payload always carries the full `moves` detail
            # internally (so a future Replay build gets full transparency
            # for free -- same "postgame reveals everything" precedent as
            # burned-unseen reserves), but a live audience only ever sees
            # it once reason == "triggered" -- the hidden contents of an
            # expired/invalidated/still-resuming correction are never
            # revealed live. See the Market Correction design writeup.
            reveal_moves = is_replay or event.payload.get("reason") == MarketCorrectionResolutionReason.TRIGGERED.value
            views.append(_event_view(event, redact=() if reveal_moves else {"moves"}))
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
        "reserve_count_remaining": sum(
            1 for h in game.holdings.values() if h.owner_player_id == player.game_player_id and h.zone == HoldingZone.RESERVE_UNREVEALED
        ),
    }

    # Ready-to-close: owner-only live, never the aggregate — see visibility §06.
    if is_self:
        out["ready_to_close"] = player.ready_to_close
    elif is_replay and game.config.ready_to_close_revealed_in_replay:
        out["ready_to_close"] = player.ready_to_close

    # Influence balance is self-only, full stop -- once cost depends on
    # real ownership (private Influence economy), a visible balance or a
    # visible balance-delta becomes a hand-reconstruction oracle ("proposal
    # executed, Hanky's balance dropped -> Hanky must own A"). Whether
    # replay ever reveals it is deliberately undecided; defaults to still
    # private there too, flippable via config without a schema change.
    if is_self:
        out["influence"] = {"available": player.influence_available, "committed": player.influence_committed, "spent": player.influence_spent}
    elif is_replay and game.config.influence_revealed_in_replay:
        out["influence"] = {"available": player.influence_available, "committed": player.influence_committed, "spent": player.influence_spent}

    if is_self or is_replay:
        out["projected_value"] = _portfolio_value(game, player.game_player_id)
        # safe_value only means anything once the profile is visible -- see
        # the Haircut-risk design writeup. Derived structurally from the
        # profile's max_depth, not a certainty == 1.0 float comparison.
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


def _rising_entity(game: Game, entity_a: str, entity_b: str) -> str:
    """Mirrors engine._rising_entity -- duplicated rather than imported to
    avoid a projections -> engine dependency; see the private Influence
    economy design writeup."""
    a, b = game.market[entity_a], game.market[entity_b]
    return entity_a if a.position > b.position else entity_b


def _owns(game: Game, player_id: str, entity_id: str) -> bool:
    return any(
        h.owner_player_id == player_id and h.entity_id == entity_id and h.zone == HoldingZone.PORTFOLIO
        for h in game.holdings.values()
    )


def liability_for(game: Game, player_id: str, entity_a: str, entity_b: str) -> int:
    """1 iff player_id owns the rising entity of this swap, else 0 -- the
    read-only twin of engine._liability_for, exposed here (not underscored)
    since routes.py's propose-cost preview endpoint needs it too."""
    return 1 if _owns(game, player_id, _rising_entity(game, entity_a, entity_b)) else 0


def _proposal_passed_by(proposal: Proposal | None, audience: Audience) -> bool:
    return proposal is not None and isinstance(audience, PlayerAudience) and audience.game_player_id in proposal.passed_player_ids


def _public_resolution_reason(reason: ProposalResolutionReason | None, audience: Audience) -> str | None:
    """EXPIRED_ALL_PASSED is masked back to WITHDRAWN_BY_INITIATOR for
    every live audience -- publicly indistinguishable from an ordinary
    withdrawal, by design. If we reported "no takers" live, we'd have
    reconstructed the exact public stigma Pass exists to avoid, one level
    removed. ReplayAudience sees the true reason, same "revealed once
    scored" precedent as everything else post-game. See the Pass design
    writeup."""
    if reason is None:
        return None
    if reason is ProposalResolutionReason.EXPIRED_ALL_PASSED and not isinstance(audience, ReplayAudience):
        return ProposalResolutionReason.WITHDRAWN_BY_INITIATOR.value
    return reason.value


def _project_proposal(game: Game, proposal: Proposal, audience: Audience) -> dict:
    out: dict = {
        "proposal_id": proposal.proposal_id,
        "entity_a": proposal.swap.entity_a,
        "entity_b": proposal.swap.entity_b,
        # Locked at authoring time, unconditionally public -- bare
        # proposal entities are always public, so their locked direction
        # is too. See the market-direction-reversal design writeup: the
        # frontend must pin Visualize's arrows to this, never recompute
        # from current positions.
        "rising_entity_id": proposal.swap.rising_entity_id,
        "proposer_id": proposal.swap.initiator_player_id,
        "status": proposal.status.value,
        "resolution_reason": _public_resolution_reason(proposal.resolution_reason, audience),
        # Raw timestamp, not a precomputed countdown -- same pattern as
        # haircut_reveal_at/decision_deadline_at, the client derives its own
        # display. Public and unconditional: every viewer needs it to know
        # whether Accept is still locked, not just the proposer. Also what
        # a public pool's own accept-lock is gated to -- see
        # engine._require_accept_unlocked.
        "accept_locked_until": proposal.created_at + timedelta(seconds=game.config.accept_lock_seconds),
    }
    if isinstance(audience, PlayerAudience):
        if audience.game_player_id == proposal.swap.initiator_player_id:
            # The proposer's own locked liability -- must survive a page
            # reload, since the "the number never changes after you press
            # it" promise is only meaningful if it's still visible later.
            out["my_influence_liability"] = proposal.initiator_influence_liability
            # Anonymous, aggregate-only -- never identities. This is the
            # proposer's one and only channel to Pass feedback; PROPOSAL_PASSED
            # itself is ACTOR_ONLY in EVENT_VISIBILITY, so there's no live
            # event stream of individual passes to correlate against timing.
            out["passed_count"] = len(proposal.passed_player_ids)
        elif proposal.status == ResolutionStatus.OPEN:
            # A live, fresh preview of what accepting would cost *this*
            # viewer right now -- never stored, recomputed on every read.
            out["my_accept_liability"] = liability_for(game, audience.game_player_id, proposal.swap.entity_a, proposal.swap.entity_b)
    return out


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
    if isinstance(audience, PlayerAudience) and can_see_contents:
        if audience.game_player_id == pool.swap.initiator_player_id:
            out["my_influence_liability"] = pool.initiator_influence_liability
        elif pool.status == ResolutionStatus.OPEN:
            # Mirrors engine._handle_accept_pool's combine rule: base-leg
            # bit (locked, if this viewer authored the base proposal;
            # fresh otherwise) OR pool-leg bit (always fresh), capped at 1.
            base = game.proposals.get(pool.base_proposal_id)
            base_leg_liability = 0
            if base is not None:
                is_base_author = audience.game_player_id == base.swap.initiator_player_id
                base_leg_liability = (
                    base.initiator_influence_liability
                    if is_base_author
                    else liability_for(game, audience.game_player_id, base.swap.entity_a, base.swap.entity_b)
                )
            pool_leg_liability = liability_for(game, audience.game_player_id, pool.swap.entity_a, pool.swap.entity_b)
            out["my_accept_liability"] = 1 if (base_leg_liability == 1 or pool_leg_liability == 1) else 0
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


# Zones where even the owner hasn't actually seen the entity yet, live --
# the `scored` branch above never consults this, everything's revealed at
# game end regardless of zone.
_UNREVEALED_TO_OWNER_ZONES = frozenset({HoldingZone.RESERVE_UNREVEALED, HoldingZone.BURNED_UNSEEN, HoldingZone.SURRENDERED_UNUSED})


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
