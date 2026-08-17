"""The rules engine — domain model §04. Pure, synchronous, zero I/O.

Every mutation to a `Game` goes through here: lock -> apply_due_time_transitions
-> validate command -> execute -> append events -> version++ (domain model §08).
The lock and persistence live in `api/routes.py` and `persistence/`; this module
only ever sees one `Game` at a time and mutates it in place, returning the list
of newly emitted events.
"""

from __future__ import annotations

import random
import string
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from gotiate.domain.entities import (
    CancellationReason,
    CloseReason,
    Game,
    GameConfig,
    GamePhase,
    GamePlayer,
    HaircutProfile,
    Holding,
    HoldingZone,
    MarketCorrectionMove,
    MarketCorrectionResolutionReason,
    MarketEntity,
    PendingMarketCorrection,
    PendingPickup,
    PickupFailureReason,
    Pool,
    PoolResolutionReason,
    PoolVisibility,
    Proposal,
    ProposalResolutionReason,
    ResolutionStatus,
    SwapIntent,
)
from gotiate.domain.errors import DomainError, IllegalCommandError, StaleVersionError
from gotiate.domain.events import EventType, GameEvent
from gotiate.domain import setup, themes

# --------------------------------------------------------------------------
# IDs
# --------------------------------------------------------------------------

_JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I ambiguity


def new_id() -> str:
    return str(uuid.uuid4())


def new_join_code(length: int) -> str:
    return "".join(random.choices(_JOIN_CODE_ALPHABET, k=length))


# --------------------------------------------------------------------------
# Lobby — three commands, not one (see decision log §10)
# --------------------------------------------------------------------------


def create_game(
    *,
    actor_auth_user_id: str,
    display_name: str,
    now: datetime,
    config: GameConfig | None = None,
    is_golden_name: bool = False,
) -> tuple[Game, list[GameEvent]]:
    resolved_config = config or GameConfig()
    game = Game(
        id=new_id(),
        created_at=now,
        join_code=new_join_code(resolved_config.join_code_length),
        config=resolved_config,
        lobby_reminder_deadline_at=now + timedelta(seconds=resolved_config.lobby_reminder_seconds),
    )
    host = GamePlayer(
        game_player_id=new_id(),
        auth_user_id=actor_auth_user_id,
        seat=0,
        display_name=display_name,
        is_golden_name=is_golden_name,
        influence_available=game.config.starting_influence,
    )
    game.host_player_id = host.game_player_id
    game.players.append(host)

    events = [
        _emit(game, now, EventType.GAME_CREATED, actor=host.game_player_id, payload={"join_code": game.join_code}),
        _emit(game, now, EventType.PLAYER_JOINED, actor=host.game_player_id, payload={"seat": 0}),
    ]
    game.version += 1
    return game, events


def join_game(
    game: Game, *, actor_auth_user_id: str, display_name: str, now: datetime, is_golden_name: bool = False
) -> tuple[GamePlayer, list[GameEvent]]:
    if game.phase != GamePhase.LOBBY:
        raise IllegalCommandError("game is no longer accepting players")
    if now > game.created_at + timedelta(minutes=game.config.join_code_lifetime_minutes):
        raise IllegalCommandError("join code has expired")
    if len(game.players) >= 6:
        raise IllegalCommandError("game is full")
    if game.player_by_auth_id(actor_auth_user_id) is not None:
        raise IllegalCommandError("already joined this game")

    player = GamePlayer(
        game_player_id=new_id(),
        auth_user_id=actor_auth_user_id,
        seat=len(game.players),
        display_name=display_name,
        is_golden_name=is_golden_name,
        influence_available=game.config.starting_influence,
    )
    game.players.append(player)
    events = [_emit(game, now, EventType.PLAYER_JOINED, actor=player.game_player_id, payload={"seat": player.seat})]
    game.version += 1
    return player, events


# --------------------------------------------------------------------------
# Command dispatch
# --------------------------------------------------------------------------

_VERSION_EXEMPT_COMMANDS = frozenset({"DISCARD_HOLDING", "DECLINE_PICKUP"})


def handle_command(
    game: Game,
    *,
    command_type: str,
    payload: dict,
    actor_game_player_id: str | None,
    expected_version: int | None,
    now: datetime,
) -> list[GameEvent]:
    events = apply_due_time_transitions(game, now)
    if events:
        game.version += 1

    handler = _HANDLERS.get(command_type)
    if handler is None:
        raise IllegalCommandError(f"unknown command type {command_type!r}")

    # DISCARD_HOLDING and DECLINE_PICKUP are deliberately exempt -- both
    # are bound to pending_pickup_id instead, since the rest of the game
    # keeps moving during a player's lock and a frozen client can't know
    # the live version.
    if command_type not in _VERSION_EXEMPT_COMMANDS and expected_version is not None and expected_version != game.version:
        err = StaleVersionError(f"expected version {expected_version}, game is at {game.version}")
        err.partial_events = events
        raise err

    try:
        new_events = handler(game, payload=payload, actor_game_player_id=actor_game_player_id, now=now)
    except DomainError as exc:
        exc.partial_events = events
        raise

    if new_events:
        game.version += 1
    events += new_events
    return events


# --------------------------------------------------------------------------
# Time-driven transitions — run ahead of every command (§04, §08)
# --------------------------------------------------------------------------


def apply_due_time_transitions(game: Game, now: datetime) -> list[GameEvent]:
    events: list[GameEvent] = []

    if game.phase == GamePhase.LOBBY:
        # Grace window past the reminder deadline, with no Start and no
        # EXTEND_LOBBY_TIMER in between -- the host went quiet, auto-cancel.
        # routes.get_game also runs this (see is_time_transition_due) --
        # an abandoned lobby has nobody left to submit a command that
        # would otherwise trigger it.
        if (
            game.lobby_reminder_deadline_at is not None
            and now >= game.lobby_reminder_deadline_at + timedelta(seconds=game.config.lobby_reminder_grace_seconds)
        ):
            game.phase = GamePhase.CANCELLED
            game.cancellation_reason = CancellationReason.LOBBY_TIMEOUT
            events.append(
                _emit(game, now, EventType.GAME_CANCELLED, actor=None, payload={"reason": CancellationReason.LOBBY_TIMEOUT.value})
            )
        return events

    if game.phase != GamePhase.NEGOTIATION:
        return events

    for player in game.players:
        pp = player.pending_pickup
        if pp is None:
            continue
        deadline = pp.decision_deadline_at + timedelta(milliseconds=game.config.pickup_transport_grace_ms)
        if now > deadline:
            events += _fail_pending_pickup(game, player, PickupFailureReason.DECISION_TIMEOUT, now)

    if game.unilateral_window_closed_at is None and game.unilateral_cutoff_at is not None and now >= game.unilateral_cutoff_at:
        game.unilateral_window_closed_at = now
        events.append(_emit(game, now, EventType.UNILATERAL_WINDOW_CLOSED, actor=None, payload={}))

    if game.haircut_profile_revealed_at is None and game.haircut_reveal_at is not None and now >= game.haircut_reveal_at:
        game.haircut_profile_revealed_at = now
        events.append(_emit(game, now, EventType.HAIRCUT_RISK_REVEALED, actor=None, payload={}))

    if len(game.players) == 2:
        events += _apply_due_market_correction_transitions(game, now)

    if game.phase == GamePhase.NEGOTIATION and game.started_at is not None and game.max_duration_s is not None:
        if now >= game.started_at + timedelta(seconds=game.max_duration_s):
            events += close_market(game, CloseReason.TIME_EXPIRED, now)

    return events


def _apply_due_market_correction_transitions(game: Game, now: datetime) -> list[GameEvent]:
    """2-player-only anti-stagnation mechanic -- see the Market Correction
    design writeup. Two independent transitions: expire a pending
    correction nobody triggered in time, or offer a new one once the
    market's been quiet long enough and the post-resolution cooldown has
    cleared. Kept as its own function (rather than inlined into
    apply_due_time_transitions) purely for readability -- called from
    both apply_due_time_transitions and mirrored in
    is_time_transition_due, same discipline as every other transition
    here."""
    events: list[GameEvent] = []
    correction = game.pending_market_correction

    if correction is not None:
        if now >= correction.expires_at:
            events.append(_resolve_market_correction(game, correction, MarketCorrectionResolutionReason.EXPIRED, None, now))
        return events

    if (
        game.market_correction_cooldown_until is not None
        and now >= game.market_correction_cooldown_until
        and game.last_negotiated_execution_at is not None
        and now - game.last_negotiated_execution_at >= timedelta(seconds=game.config.market_correction_stagnation_seconds)
    ):
        new_correction = _construct_market_correction(game, now, random.Random())
        if new_correction is not None:
            game.pending_market_correction = new_correction
            events.append(
                _emit(
                    game,
                    now,
                    EventType.MARKET_CORRECTION_OFFERED,
                    actor=None,
                    payload={"correction_id": new_correction.correction_id, "expires_at": new_correction.expires_at.isoformat()},
                )
            )
        # Construction failing (no legal destination for either eligible
        # candidate) is expected to be exceptionally rare -- deliberately
        # NOT pushing the cooldown here, so the next tick just retries
        # against whatever the board looks like by then, rather than
        # sitting dark for a full cooldown period over what should
        # self-resolve as soon as anything on the board changes.

    return events


def is_time_transition_due(game: Game, now: datetime) -> bool:
    """Side-effect-free mirror of apply_due_time_transitions' conditions --
    lets a read path (routes.get_game) decide cheaply whether it's worth
    acquiring the write lock and reapplying for real, without paying that
    cost on every single poll. A negotiation clock elapsing or a lobby
    grace window elapsing both need *something* to notice and persist the
    transition; once the relevant actor has gone quiet, polling is the
    only thing left that ever will. Keep in sync with
    apply_due_time_transitions -- same conditions, no mutation."""
    if game.phase == GamePhase.LOBBY:
        return (
            game.lobby_reminder_deadline_at is not None
            and now >= game.lobby_reminder_deadline_at + timedelta(seconds=game.config.lobby_reminder_grace_seconds)
        )
    if game.phase != GamePhase.NEGOTIATION:
        return False
    for player in game.players:
        pp = player.pending_pickup
        if pp is not None and now > pp.decision_deadline_at + timedelta(milliseconds=game.config.pickup_transport_grace_ms):
            return True
    if game.unilateral_window_closed_at is None and game.unilateral_cutoff_at is not None and now >= game.unilateral_cutoff_at:
        return True
    if game.haircut_profile_revealed_at is None and game.haircut_reveal_at is not None and now >= game.haircut_reveal_at:
        return True
    if len(game.players) == 2:
        correction = game.pending_market_correction
        if correction is not None:
            if now >= correction.expires_at:
                return True
        elif (
            game.market_correction_cooldown_until is not None
            and now >= game.market_correction_cooldown_until
            and game.last_negotiated_execution_at is not None
            and now - game.last_negotiated_execution_at >= timedelta(seconds=game.config.market_correction_stagnation_seconds)
        ):
            return True
    return game.started_at is not None and game.max_duration_s is not None and now >= game.started_at + timedelta(seconds=game.max_duration_s)


def can_burn_reserve_for_swap(game: Game, now: datetime) -> bool:
    return game.unilateral_cutoff_at is not None and now < game.unilateral_cutoff_at


def _fail_pending_pickup(
    game: Game, player: GamePlayer, reason: PickupFailureReason, now: datetime
) -> list[GameEvent]:
    pp = player.pending_pickup
    assert pp is not None
    reserve = game.holdings[pp.reserve_holding_id]
    reserve.zone = HoldingZone.PICKUP_SURRENDERED
    player.pending_pickup = None
    return [
        _emit(
            game,
            now,
            EventType.PICKUP_FAILED,
            actor=player.game_player_id,
            payload={
                "pending_pickup_id": pp.pending_pickup_id,
                "reserve_holding_id": pp.reserve_holding_id,
                "reason": reason.value,
            },
        )
    ]


# --------------------------------------------------------------------------
# close_market — the one canonical closure operation (§04, §07)
# --------------------------------------------------------------------------


def close_market(game: Game, reason: CloseReason, now: datetime) -> list[GameEvent]:
    if game.phase != GamePhase.NEGOTIATION:
        return []

    events: list[GameEvent] = []
    game.phase = GamePhase.CLOSING
    game.closed_at = now
    game.close_reason = reason
    events.append(_emit(game, now, EventType.MARKET_CLOSED, actor=None, payload={"reason": reason.value}))

    for proposal in game.proposals.values():
        if proposal.status == ResolutionStatus.OPEN:
            # _resolve_proposal refunds committed -> available for any
            # non-EXECUTED reason, MARKET_CLOSED included -- no beneficial
            # negotiated action happened, so there's nothing to charge for.
            events.append(_resolve_proposal(game, proposal, ProposalResolutionReason.MARKET_CLOSED, None, now))

    for pool in list(game.pools.values()):
        if pool.status == ResolutionStatus.OPEN:
            events.append(_resolve_pool(game, pool, PoolResolutionReason.MARKET_CLOSED, None, now, spend=False))

    # An offered-but-unresolved Market Correction is moot once the game
    # ends -- EXPIRED is the closest existing fit ("this opportunity is
    # now gone"), not a new reason just for this.
    pending_correction = game.pending_market_correction
    if pending_correction is not None:
        events.append(_resolve_market_correction(game, pending_correction, MarketCorrectionResolutionReason.EXPIRED, None, now))

    for player in game.players:
        if player.pending_pickup is not None:
            events += _fail_pending_pickup(game, player, PickupFailureReason.MARKET_CLOSED, now)

    for holding in game.holdings.values():
        if holding.zone == HoldingZone.RESERVE_UNREVEALED:
            holding.zone = HoldingZone.SURRENDERED_UNUSED

    events.append(_emit(game, now, EventType.PORTFOLIOS_REVEALED, actor=None, payload={}))

    # The one and only random draw for this game's scoring -- see the
    # Haircut-risk design writeup's invariant. Persisted immediately;
    # everything downstream (this event's own payload, and every later
    # project()/replay call at SCORED) reads it back through the pure
    # compute_final_scores, never rerolls it. A fresh unseeded rng here,
    # same convention as _handle_start_game -- draw_haircut_depth itself
    # takes an explicit rng specifically so it stays unit-testable with a
    # seeded one.
    assert game.haircut_profile is not None
    game.realized_haircut_depth = draw_haircut_depth(game.haircut_profile, random.Random())
    result = compute_final_scores(game, game.realized_haircut_depth)
    events.append(_emit(game, now, EventType.GAME_SCORED, actor=None, payload=result))

    game.scored_at = now
    game.phase = GamePhase.SCORED
    events.append(_emit(game, now, EventType.GAME_ENDED, actor=None, payload={}))

    return events


def draw_haircut_depth(profile: HaircutProfile, rng: random.Random) -> int:
    """The sole random draw behind Haircut scoring -- one correlated pick
    of a wipe depth, never independent per-position rolls. Must only ever
    be called once per game, from close_market; see compute_final_scores
    for the pure, deterministic half of scoring."""
    depths = range(len(profile.depth_probabilities))
    return rng.choices(depths, weights=profile.depth_probabilities, k=1)[0]


def compute_final_scores(game: Game, realized_haircut_depth: int) -> dict:
    """haircut_risk_v1 — positions 1..realized_haircut_depth score zero,
    everything else scores its linear-rank value; highest total wins, exact
    ties share the win. Pure and rng-free by design: called once to build
    GAME_SCORED's payload and again, idempotently, from every later
    project()/replay call at SCORED -- see the "randomness happens exactly
    once" invariant in the Haircut-risk design writeup. Never draws a depth
    itself, only ever consumes an already-persisted one."""
    n = len(game.market)
    positions = {eid: m.position for eid, m in game.market.items()}
    wiped_entity_ids = sorted(eid for eid, pos in positions.items() if pos <= realized_haircut_depth)

    results = []
    for player in game.players:
        holdings = [
            h for h in game.holdings.values() if h.owner_player_id == player.game_player_id and h.zone == HoldingZone.PORTFOLIO
        ]
        final_value = sum(n - positions[h.entity_id] + 1 for h in holdings if positions[h.entity_id] > realized_haircut_depth)
        results.append({"game_player_id": player.game_player_id, "final_value": final_value})

    max_value = max(r["final_value"] for r in results)
    winners = [r["game_player_id"] for r in results if r["final_value"] == max_value]

    return {
        "realized_haircut_depth": realized_haircut_depth,
        "wiped_entity_ids": wiped_entity_ids,
        "results": results,
        "winners": winners,
    }


# --------------------------------------------------------------------------
# Private Influence economy — one rule, reused everywhere a negotiated
# action needs a cost: a player's liability for a swap is 1 iff they own
# the entity that rises out of it, else 0. Never more than 1 per resolved
# package (see the ACCEPT_POOL combine logic below), never based on
# quantity owned or movement magnitude. See the plan's design writeup.
# --------------------------------------------------------------------------


def _rising_entity(game: Game, entity_a: str, entity_b: str) -> str:
    """Whichever of the two currently holds the worse (higher) position --
    it takes the other's better position once swapped. Not assumed to be
    entity_a; PROPOSE_SWAP's payload just reflects click order, not market
    direction, so direction is always derived from live positions."""
    a = _market_entity(game, entity_a)
    b = _market_entity(game, entity_b)
    return entity_a if a.position > b.position else entity_b


def _owns(game: Game, player_id: str, entity_id: str) -> bool:
    return any(
        h.owner_player_id == player_id and h.entity_id == entity_id and h.zone == HoldingZone.PORTFOLIO
        for h in game.holdings.values()
    )


def _liability_for(game: Game, player_id: str, entity_a: str, entity_b: str) -> int:
    """1 iff player_id owns the rising entity of this swap, else 0."""
    return 1 if _owns(game, player_id, _rising_entity(game, entity_a, entity_b)) else 0


def _has_open_authored_negotiation(game: Game, player_id: str) -> bool:
    """True if player_id currently authors any open Proposal or Pool --
    used to block reserve/hole-card actions, which would otherwise let a
    player change the portfolio basis of a liability they've already
    locked in (see PICK_UP_RESERVE/BURN_RESERVE_FOR_SWAP)."""
    return any(p.status == ResolutionStatus.OPEN and p.swap.initiator_player_id == player_id for p in game.proposals.values()) or any(
        p.status == ResolutionStatus.OPEN and p.swap.initiator_player_id == player_id for p in game.pools.values()
    )


# --------------------------------------------------------------------------
# The Agency Principle, written once (§01, §04)
# --------------------------------------------------------------------------


def resolve_sibling_pools(game: Game, base_proposal: Proposal, resolving_actor: str, now: datetime) -> list[GameEvent]:
    events: list[GameEvent] = []
    for pool in list(game.pools.values()):
        if pool.base_proposal_id != base_proposal.proposal_id or pool.status != ResolutionStatus.OPEN:
            continue
        if pool.swap.initiator_player_id == resolving_actor:
            events.append(
                _resolve_pool(game, pool, PoolResolutionReason.INVALIDATED_BY_INITIATOR_ACTION, resolving_actor, now, spend=True)
            )
        else:
            events.append(
                _resolve_pool(game, pool, PoolResolutionReason.PREEMPTED_BY_OTHER_ACTION, resolving_actor, now, spend=False)
            )
    return events


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------


def _handle_cancel_game(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    # LOBBY-only, deliberately: once real gameplay is underway (Influence
    # spent, holdings dealt), the host unilaterally nuking the match for
    # everyone else is a bad power dynamic to allow. A NEGOTIATION-phase
    # game that gets abandoned closes on its own via the negotiation clock
    # regardless -- that's the only escape hatch past this point.
    if game.phase != GamePhase.LOBBY:
        raise IllegalCommandError("can only cancel while still in the lobby")
    if actor_game_player_id != game.host_player_id:
        raise IllegalCommandError("only the host can cancel")

    game.phase = GamePhase.CANCELLED
    game.cancellation_reason = CancellationReason.HOST_INITIATED
    return [_emit(game, now, EventType.GAME_CANCELLED, actor=actor_game_player_id, payload={"reason": CancellationReason.HOST_INITIATED.value})]


def _handle_extend_lobby_timer(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    if game.phase != GamePhase.LOBBY:
        raise IllegalCommandError("can only be extended while still in the lobby")
    if actor_game_player_id != game.host_player_id:
        raise IllegalCommandError("only the host can ask for more time")

    game.lobby_reminder_deadline_at = now + timedelta(seconds=game.config.lobby_reminder_seconds)
    return [
        _emit(
            game,
            now,
            EventType.LOBBY_TIMER_EXTENDED,
            actor=actor_game_player_id,
            payload={"lobby_reminder_deadline_at": game.lobby_reminder_deadline_at.isoformat()},
        )
    ]


def _handle_start_game(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    if game.phase != GamePhase.LOBBY:
        raise IllegalCommandError("game already started")
    if actor_game_player_id != game.host_player_id:
        raise IllegalCommandError("only the host can start the game")
    n = len(game.players)
    if not (2 <= n <= 6):
        raise IllegalCommandError("need 2-6 players to start")

    rng = random.Random()
    events: list[GameEvent] = []

    market_size = game.config.market_size_by_players[n]
    theme_set = themes.get_theme_set(game.config.theme_set_id)
    player_ids = [p.game_player_id for p in game.players]
    starting_state = setup.generate_starting_state(
        theme_set, market_size, player_ids, game.config.portfolio_shape, game.config.setup_quality_by_players[n], rng
    )

    for i, entity_id in enumerate(starting_state.market_order, start=1):
        # entity_id and theme_key coincide by construction — each theme_key
        # is sampled at most once per market — but stay distinct fields; see
        # themes.py's module docstring for why.
        game.market[entity_id] = MarketEntity(entity_id=entity_id, theme_key=entity_id, position=i)
    events.append(_emit(game, now, EventType.MARKET_INITIALIZED, actor=None, payload={"size": market_size, "theme_set_id": theme_set.theme_set_id}))

    entity_ids = list(game.market.keys())
    for player in game.players:
        for entity_id in starting_state.portfolios[player.game_player_id]:
            h = Holding(holding_id=new_id(), entity_id=entity_id, owner_player_id=player.game_player_id, zone=HoldingZone.PORTFOLIO)
            game.holdings[h.holding_id] = h
        for _ in range(game.config.reserve_count):
            entity_id = rng.choice(entity_ids)
            h = Holding(
                holding_id=new_id(), entity_id=entity_id, owner_player_id=player.game_player_id, zone=HoldingZone.RESERVE_UNREVEALED
            )
            game.holdings[h.holding_id] = h
    # Diagnostics never shown live (PORTFOLIO_DEALT is SERVER_ONLY), visible
    # via replay once scored and queryable forever from event_ledger -- see
    # the initial-distribution-quality design writeup.
    events.append(_emit(game, now, EventType.PORTFOLIO_DEALT, actor=None, payload=starting_state.diagnostics))
    events.append(_emit(game, now, EventType.RESERVES_DEALT, actor=None, payload={}))

    game.max_duration_s = game.config.max_clock_seconds_by_players[n]
    game.started_at = now
    game.unilateral_cutoff_at = now + timedelta(
        seconds=game.max_duration_s * (1 - game.config.unilateral_cutoff_fraction)
    )
    game.close_threshold = game.config.close_threshold(n)

    # 2-player-only, but harmless to set regardless -- every check that
    # consumes these already gates on len(game.players) == 2. No extra
    # grace beyond the stagnation threshold itself: cooldown starts
    # already-cleared, at started_at. See the Market Correction design
    # writeup.
    game.last_negotiated_execution_at = now
    game.market_correction_cooldown_until = now

    # Chosen and locked now, hidden until haircut_reveal_at -- see the
    # Haircut-risk design writeup. game.max_duration_s is already assigned
    # above, so this can reference it directly rather than recomputing from
    # config (both would agree, but this avoids the ordering dependency).
    game.haircut_profile = rng.choice(game.config.haircut_profiles_by_players[n])
    game.haircut_reveal_at = now + timedelta(seconds=game.max_duration_s * game.config.haircut_reveal_fraction)
    events.append(_emit(game, now, EventType.HAIRCUT_PROFILE_SELECTED, actor=None, payload={}))
    game.phase = GamePhase.NEGOTIATION
    events.append(_emit(game, now, EventType.GAME_STARTED, actor=actor_game_player_id, payload={"player_count": n}))
    return events


def _handle_propose_swap(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    entity_a, entity_b = payload["entity_a"], payload["entity_b"]
    if entity_a == entity_b:
        raise IllegalCommandError("a proposal must name two different entities")
    _require_entities_exist(game, entity_a, entity_b)
    if any(
        p.status == ResolutionStatus.OPEN and p.swap.initiator_player_id == actor_game_player_id for p in game.proposals.values()
    ):
        raise IllegalCommandError("you already have an open proposal")

    player = game.player_by_id(actor_game_player_id)
    # A 0-liability proposal (advocating movement that doesn't benefit your
    # actual holdings) is always legal regardless of balance -- Influence
    # is a private tax on beneficial actions, not a toll on speaking. Only
    # a liability of 1 needs affordability and reserves Influence, locked
    # for the lifetime of this proposal.
    liability = _liability_for(game, actor_game_player_id, entity_a, entity_b)
    if liability == 1:
        if player.influence_available < 1:
            raise IllegalCommandError("no Influence available")
        player.influence_available -= 1
        player.influence_committed += 1

    proposal = Proposal(
        proposal_id=new_id(),
        swap=SwapIntent(
            entity_a=entity_a,
            entity_b=entity_b,
            initiator_player_id=actor_game_player_id,
            rising_entity_id=_rising_entity(game, entity_a, entity_b),
        ),
        initiator_influence_liability=liability,
    )
    game.proposals[proposal.proposal_id] = proposal
    return [
        _emit(
            game,
            now,
            EventType.PROPOSAL_CREATED,
            actor=actor_game_player_id,
            payload={"proposal_id": proposal.proposal_id, "entity_a": entity_a, "entity_b": entity_b},
        )
    ]


def _handle_withdraw_proposal(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    if proposal.swap.initiator_player_id != actor_game_player_id:
        raise IllegalCommandError("only the proposer can withdraw")

    events = [_resolve_proposal(game, proposal, ProposalResolutionReason.WITHDRAWN_BY_INITIATOR, actor_game_player_id, now)]

    for pool in list(game.pools.values()):
        if pool.base_proposal_id == proposal.proposal_id and pool.status == ResolutionStatus.OPEN:
            events.append(
                _resolve_pool(game, pool, PoolResolutionReason.BASE_PROPOSAL_WITHDRAWN, actor_game_player_id, now, spend=False)
            )
    return events


def _all_others_passed(game: Game, proposal: Proposal) -> bool:
    """True once every seated player except the proposal's own initiator
    has PASS_PROPOSAL'd it -- mathematically dead, nobody rejected it.
    See the Pass design writeup."""
    others = {p.game_player_id for p in game.players} - {proposal.swap.initiator_player_id}
    return others <= proposal.passed_player_ids


def _handle_pass_proposal(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    if proposal.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("the proposer cannot pass their own proposal")
    if actor_game_player_id in proposal.passed_player_ids:
        raise IllegalCommandError("you already passed this proposal")
    # Scoped to the actor's OWN open Pool on this proposal -- other
    # players' Pools, public or private, never block a Pass. "You can't
    # leave the hand while your own chips are in the pot."
    if any(
        pool.status == ResolutionStatus.OPEN
        and pool.base_proposal_id == proposal.proposal_id
        and pool.swap.initiator_player_id == actor_game_player_id
        for pool in game.pools.values()
    ):
        raise IllegalCommandError("withdraw your Pool on this proposal before passing")

    proposal.passed_player_ids.add(actor_game_player_id)
    events = [_emit(game, now, EventType.PROPOSAL_PASSED, actor=actor_game_player_id, payload={"proposal_id": proposal.proposal_id})]

    # By construction, no Pool can be open here: a passed player can never
    # hold one (blocked above) and can never create a new one (proposals
    # are only poolable by players who haven't passed -- see
    # _handle_create_pool). So this is a pure status flip, never a cascade.
    if _all_others_passed(game, proposal):
        events.append(_resolve_proposal(game, proposal, ProposalResolutionReason.EXPIRED_ALL_PASSED, None, now))
    return events


def _handle_accept_proposal(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    if proposal.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("cannot accept your own proposal")
    if actor_game_player_id in proposal.passed_player_ids:
        raise IllegalCommandError("you passed this proposal and can no longer accept it")

    # The accepter's liability is fresh, evaluated right now against their
    # current holdings, and charged straight away -- no commit interval,
    # since accepting executes synchronously. Entirely independent of the
    # proposer's own already-locked liability, settled separately below.
    accepter = game.player_by_id(actor_game_player_id)
    if _liability_for(game, actor_game_player_id, proposal.swap.entity_a, proposal.swap.entity_b) == 1:
        if accepter.influence_available < 1:
            raise IllegalCommandError("no Influence available")
        accepter.influence_available -= 1
        accepter.influence_spent += 1

    # A negotiated deal executing is proof the market isn't frozen --
    # unconditional, resolved *before* this deal's own _execute_swap runs
    # (not after), so a deal that happens to also cross a pending
    # correction's locked move still resolves it as MARKET_RESUMED, never
    # INVALIDATED -- ordering caught on review, not incidental. See the
    # Market Correction design writeup.
    game.last_negotiated_execution_at = now
    events: list[GameEvent] = []
    pending_correction = game.pending_market_correction
    if pending_correction is not None:
        events.append(_resolve_market_correction(game, pending_correction, MarketCorrectionResolutionReason.MARKET_RESUMED, None, now))
    events += _execute_swap(game, proposal.swap, now, exclude_proposal_id=proposal.proposal_id)
    events.append(_resolve_proposal(game, proposal, ProposalResolutionReason.EXECUTED, actor_game_player_id, now))
    events += resolve_sibling_pools(game, proposal, actor_game_player_id, now)
    return events


def _handle_create_pool(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    if proposal.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("the proposer cannot pool their own proposal")
    if actor_game_player_id in proposal.passed_player_ids:
        raise IllegalCommandError("you passed this proposal and can no longer pool onto it")

    entity_c, entity_d = payload["entity_c"], payload["entity_d"]
    if entity_c == entity_d:
        raise IllegalCommandError("a pool must name two different entities")
    _require_entities_exist(game, entity_c, entity_d)
    overlap = {proposal.swap.entity_a, proposal.swap.entity_b} & {entity_c, entity_d}
    if overlap:
        raise IllegalCommandError(f"pool cannot reference {sorted(overlap)}, already in the base proposal")

    visibility = PoolVisibility(payload["visibility"])
    if visibility is PoolVisibility.PUBLIC and not game.config.allow_public_pools:
        raise IllegalCommandError("public pools are disabled for this game")
    if visibility is PoolVisibility.PRIVATE and not game.config.allow_private_pools:
        raise IllegalCommandError("private pools are disabled for this game")

    for pool in game.pools.values():
        if (
            pool.base_proposal_id == proposal.proposal_id
            and pool.status == ResolutionStatus.OPEN
            and pool.swap.initiator_player_id == actor_game_player_id
        ):
            raise IllegalCommandError("you already have an open pool on this proposal")

    player = game.player_by_id(actor_game_player_id)
    # Same rule as PROPOSE_SWAP: locked once, against the pool's own swap,
    # from the pooler's holdings right now. 0-liability pools are always
    # legal regardless of balance.
    liability = _liability_for(game, actor_game_player_id, entity_c, entity_d)
    if liability == 1:
        if player.influence_available < 1:
            raise IllegalCommandError("no Influence available")
        player.influence_available -= 1
        player.influence_committed += 1

    pool = Pool(
        pool_id=new_id(),
        base_proposal_id=proposal.proposal_id,
        swap=SwapIntent(
            entity_a=entity_c,
            entity_b=entity_d,
            initiator_player_id=actor_game_player_id,
            rising_entity_id=_rising_entity(game, entity_c, entity_d),
        ),
        initiator_influence_liability=liability,
        visibility=visibility,
    )
    game.pools[pool.pool_id] = pool
    event_type = EventType.PUBLIC_POOL_CREATED if visibility is PoolVisibility.PUBLIC else EventType.PRIVATE_POOL_CREATED
    return [
        _emit(
            game,
            now,
            event_type,
            actor=actor_game_player_id,
            payload={
                "pool_id": pool.pool_id,
                "base_proposal_id": proposal.proposal_id,
                "entity_c": entity_c,
                "entity_d": entity_d,
            },
        )
    ]


def _handle_withdraw_pool(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    pool = _require_open_pool(game, payload["pool_id"])
    if pool.swap.initiator_player_id != actor_game_player_id:
        raise IllegalCommandError("only the pool's initiator can withdraw it")
    return [_resolve_pool(game, pool, PoolResolutionReason.WITHDRAWN_BY_INITIATOR, actor_game_player_id, now, spend=False)]


def _handle_make_pool_public(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    pool = _require_open_pool(game, payload["pool_id"])
    if pool.swap.initiator_player_id != actor_game_player_id:
        raise IllegalCommandError("only the pool's initiator can make it public")
    if pool.visibility is PoolVisibility.PUBLIC:
        raise IllegalCommandError("pool is already public")
    pool.visibility = PoolVisibility.PUBLIC
    return [_emit(game, now, EventType.POOL_MADE_PUBLIC, actor=actor_game_player_id, payload={"pool_id": pool.pool_id})]


def _handle_decline_pool(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    pool = _require_open_pool(game, payload["pool_id"])
    if pool.visibility is not PoolVisibility.PRIVATE:
        raise IllegalCommandError("only a private pool can be declined")
    base = _require_open_proposal(game, pool.base_proposal_id)
    if base.swap.initiator_player_id != actor_game_player_id:
        raise IllegalCommandError("only the base proposer may decline a private pool")
    return [_resolve_pool(game, pool, PoolResolutionReason.DECLINED_BY_TARGET, actor_game_player_id, now, spend=False)]


def _handle_accept_pool(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    pool = _require_open_pool(game, payload["pool_id"])
    base = _require_open_proposal(game, pool.base_proposal_id)

    if pool.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("cannot accept your own pool")
    if pool.visibility is PoolVisibility.PRIVATE and actor_game_player_id != base.swap.initiator_player_id:
        raise IllegalCommandError("only the base proposer may accept a private pool")
    if actor_game_player_id in base.passed_player_ids:
        raise IllegalCommandError("you passed this proposal and can no longer accept a Pool on it")

    # Accepting a pool affirms BOTH legs at once -- the accepter's total
    # liability is the OR of a base-leg bit and a pool-leg bit, capped at 1
    # (see the private Influence economy design). The base-leg bit is the
    # base proposal's own author-locked value *only* when the accepter is
    # that author (true for every private-pool accept, since only the base
    # proposer may accept one); otherwise it's evaluated fresh, same as for
    # a third party accepting a public pool. The pool-leg bit is always
    # fresh -- the pool's own initiator can never be its own accepter.
    is_base_author = actor_game_player_id == base.swap.initiator_player_id
    base_leg_liability = (
        base.initiator_influence_liability
        if is_base_author
        else _liability_for(game, actor_game_player_id, base.swap.entity_a, base.swap.entity_b)
    )
    pool_leg_liability = _liability_for(game, actor_game_player_id, pool.swap.entity_a, pool.swap.entity_b)
    already_committed = is_base_author and base.initiator_influence_liability == 1
    if not already_committed and (base_leg_liability == 1 or pool_leg_liability == 1):
        accepter = game.player_by_id(actor_game_player_id)
        if accepter.influence_available < 1:
            raise IllegalCommandError("no Influence available")
        accepter.influence_available -= 1
        accepter.influence_spent += 1

    # Same ordering rule as _handle_accept_proposal -- resolve any pending
    # correction as MARKET_RESUMED *before* either leg's _execute_swap
    # runs, so the reason is deterministic regardless of incidental
    # overlap. See the Market Correction design writeup.
    game.last_negotiated_execution_at = now
    events: list[GameEvent] = []
    pending_correction = game.pending_market_correction
    if pending_correction is not None:
        events.append(_resolve_market_correction(game, pending_correction, MarketCorrectionResolutionReason.MARKET_RESUMED, None, now))
    events += _execute_swap(game, base.swap, now, exclude_proposal_id=base.proposal_id, exclude_pool_id=pool.pool_id)
    events += _execute_swap(game, pool.swap, now, exclude_proposal_id=base.proposal_id, exclude_pool_id=pool.pool_id)
    events.append(_resolve_proposal(game, base, ProposalResolutionReason.EXECUTED, actor_game_player_id, now))
    events.append(_resolve_pool(game, pool, PoolResolutionReason.EXECUTED, actor_game_player_id, now, spend=True))
    events += resolve_sibling_pools(game, base, actor_game_player_id, now)
    return events


def _handle_pick_up_reserve(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    from gotiate.domain.projections import PlayerAudience, project  # local import avoids a cycle at module load

    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    if _has_open_authored_negotiation(game, actor_game_player_id):
        raise IllegalCommandError("finish your open proposal or Pool before picking up a reserve")
    player = game.player_by_id(actor_game_player_id)
    holding = game.holdings.get(payload["reserve_holding_id"])
    if holding is None or holding.owner_player_id != actor_game_player_id or holding.zone != HoldingZone.RESERVE_UNREVEALED:
        raise IllegalCommandError("not a valid unrevealed reserve you own")

    holding.zone = HoldingZone.PICKUP_PENDING
    holding.revealed_to_owner = True
    holding.revealed_at_seq_no = game.next_seq_no

    original_five = [
        h.holding_id for h in game.holdings.values() if h.owner_player_id == actor_game_player_id and h.zone == HoldingZone.PORTFOLIO
    ]
    deadline = now + timedelta(seconds=game.config.pickup_decision_seconds)

    pp = PendingPickup(
        pending_pickup_id=new_id(),
        reserve_holding_id=holding.holding_id,
        original_portfolio_holding_ids=original_five,
        revealed_entity_id=holding.entity_id,
        started_at=now,
        decision_deadline_at=deadline,
        market_version_at_start=game.version,
    )
    # Cached view rendered *before* pending_pickup is attached -- project()
    # short-circuits to player.pending_pickup.cached_view the moment that
    # field is set, so computing it after attaching would just re-read its
    # own still-empty default back into itself, permanently. The holding's
    # zone is already flipped to PICKUP_PENDING above, so this snapshot
    # already reflects the frozen moment this player is about to be locked
    # into -- attaching pp is what actually locks it. The pending_pickup
    # block itself (deadline, revealed entity) is injected directly here,
    # not read back through _project_player -- by the time pp is attached,
    # project() never calls _project_player again for this game until the
    # pickup resolves, so there'd be nothing left to compute it from.
    pp.cached_view = project(game, PlayerAudience(actor_game_player_id))
    pp.cached_view["pending_pickup"] = {
        "pending_pickup_id": pp.pending_pickup_id,
        "reserve_holding_id": pp.reserve_holding_id,
        "revealed_entity_id": pp.revealed_entity_id,
        "decision_deadline_at": pp.decision_deadline_at,
    }
    player.pending_pickup = pp

    return [
        _emit(
            game,
            now,
            EventType.PICKUP_STARTED,
            actor=actor_game_player_id,
            payload={
                "pending_pickup_id": pp.pending_pickup_id,
                "reserve_holding_id": holding.holding_id,
                "revealed_entity_id": holding.entity_id,
                "decision_deadline_at": deadline.isoformat(),
            },
        )
    ]


def _handle_discard_holding(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    player = game.player_by_id(actor_game_player_id)
    pp = player.pending_pickup
    if pp is None or pp.pending_pickup_id != payload["pending_pickup_id"]:
        # apply_due_time_transitions already ran ahead of this handler (see
        # handle_command) — if the pickup had expired, it would already be
        # gone by now. Reaching here with no match means it was never valid.
        raise IllegalCommandError("no matching open pending pickup")

    discard_id = payload["holding_id_to_discard"]
    if discard_id not in pp.original_portfolio_holding_ids:
        raise IllegalCommandError("must discard one of the original five")

    game.holdings[discard_id].zone = HoldingZone.DISCARDED
    reserve = game.holdings[pp.reserve_holding_id]
    reserve.zone = HoldingZone.PORTFOLIO
    player.pending_pickup = None

    events = [
        _emit(
            game,
            now,
            EventType.PICKUP_COMPLETED,
            actor=actor_game_player_id,
            payload={"pending_pickup_id": pp.pending_pickup_id, "reserve_holding_id": reserve.holding_id, "discarded_holding_id": discard_id},
        )
    ]
    # A discard changes portfolio ownership without moving any market
    # position -- the one invalidation path a pending correction needs
    # that _execute_swap's own choke point can't cover, since this never
    # calls it. See _market_correction_still_valid / the Market
    # Correction design writeup.
    pending_correction = game.pending_market_correction
    if pending_correction is not None and not _market_correction_still_valid(game):
        events.append(_resolve_market_correction(game, pending_correction, MarketCorrectionResolutionReason.INVALIDATED, None, now))
    return events


def _handle_decline_pickup(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    """"Skip" -- the player keeps their original five and the reserve goes
    back to unseen-and-surrendered, exactly the outcome a timeout already
    produces. Reuses _fail_pending_pickup's existing machinery unchanged,
    just player-triggered instead of clock-triggered, tagged with a
    distinct reason. No _require_negotiation needed, same reasoning as
    _handle_discard_holding: close_market force-resolves every open
    pending pickup before phase can leave NEGOTIATION, so `pp is None`
    alone is already equivalent to a phase check here."""
    player = game.player_by_id(actor_game_player_id)
    pp = player.pending_pickup
    if pp is None or pp.pending_pickup_id != payload["pending_pickup_id"]:
        raise IllegalCommandError("no matching open pending pickup")
    return _fail_pending_pickup(game, player, PickupFailureReason.DECLINED_BY_PLAYER, now)


def _handle_burn_reserve_for_swap(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    if _has_open_authored_negotiation(game, actor_game_player_id):
        raise IllegalCommandError("finish your open proposal or Pool before burning a reserve")
    if not can_burn_reserve_for_swap(game, now):
        raise IllegalCommandError("unilateral window has closed")

    holding = game.holdings.get(payload["reserve_holding_id"])
    if holding is None or holding.owner_player_id != actor_game_player_id or holding.zone != HoldingZone.RESERVE_UNREVEALED:
        raise IllegalCommandError("not a valid unrevealed reserve you own")

    entity_a, entity_b = payload["entity_a"], payload["entity_b"]
    if entity_a == entity_b:
        raise IllegalCommandError("must name two different entities")
    _require_entities_exist(game, entity_a, entity_b)

    holding.zone = HoldingZone.BURNED_UNSEEN
    swap = SwapIntent(
        entity_a=entity_a,
        entity_b=entity_b,
        initiator_player_id=actor_game_player_id,
        rising_entity_id=_rising_entity(game, entity_a, entity_b),
    )
    events = _execute_swap(game, swap, now)
    events.append(_emit(game, now, EventType.RESERVE_BURNED_FOR_SWAP, actor=actor_game_player_id, payload={"reserve_holding_id": holding.holding_id}))
    return events


def _handle_set_ready_to_close(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    player = game.player_by_id(actor_game_player_id)
    new_value = bool(payload["ready"])
    if player.ready_to_close == new_value:
        return []  # no-op: applied, but nothing observable changed — no version bump upstream

    player.ready_to_close = new_value
    events = [_emit(game, now, EventType.READY_TO_CLOSE_CHANGED, actor=actor_game_player_id, payload={"ready": new_value})]

    if new_value:
        count = sum(1 for p in game.players if p.ready_to_close)
        assert game.close_threshold is not None
        if count >= game.close_threshold:
            events.append(_emit(game, now, EventType.CLOSE_THRESHOLD_REACHED, actor=None, payload={"count": count}))
            events += close_market(game, CloseReason.READY_THRESHOLD, now)
    return events


def _handle_trigger_market_correction(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    """Either player may trigger; the other has no veto. Never automatic
    -- this handler is the *only* path a correction ever actually
    executes through. Not exempt from the optimistic-concurrency check
    (unlike DISCARD_HOLDING/DECLINE_PICKUP): this is a normal, live
    -polled, game-level offer visible to both players through the
    ordinary view, not a frozen per-player snapshot, so a client's own
    expected_version is meaningfully fresh. See the Market Correction
    design writeup."""
    _require_negotiation(game)
    correction = game.pending_market_correction
    if correction is None or correction.correction_id != payload["correction_id"]:
        raise IllegalCommandError("no matching Market Correction available")
    if not _market_correction_still_valid(game):
        # Defense-in-depth -- the invalidation hooks (the _execute_swap
        # choke point, and the DISCARD_HOLDING check) should already have
        # cleared this if it were broken. Re-check anyway rather than
        # trust that nothing slipped through, and clean up if so.
        game.pending_market_correction = None
        raise IllegalCommandError("that Market Correction is no longer valid")

    # Clear *before* executing either swap -- otherwise _execute_swap's
    # own invalidation scan would see this still pending after the first
    # leg and mistakenly resolve it as invalidated the instant its own
    # first move succeeds (its locked direction trivially "crosses" once
    # it's done exactly what it promised). Real bug caught on review, not
    # hypothetical -- see _invalidate_crossed_negotiations' comment on
    # this same point.
    game.pending_market_correction = None
    events: list[GameEvent] = []
    for move in correction.moves:
        events += _execute_swap(game, move.swap, now)
    events.append(_resolve_market_correction(game, correction, MarketCorrectionResolutionReason.TRIGGERED, actor_game_player_id, now))
    return events


_HANDLERS: dict[str, Callable[..., list[GameEvent]]] = {
    "CANCEL_GAME": _handle_cancel_game,
    "EXTEND_LOBBY_TIMER": _handle_extend_lobby_timer,
    "START_GAME": _handle_start_game,
    "PROPOSE_SWAP": _handle_propose_swap,
    "WITHDRAW_PROPOSAL": _handle_withdraw_proposal,
    "PASS_PROPOSAL": _handle_pass_proposal,
    "ACCEPT_PROPOSAL": _handle_accept_proposal,
    "CREATE_POOL": _handle_create_pool,
    "WITHDRAW_POOL": _handle_withdraw_pool,
    "MAKE_POOL_PUBLIC": _handle_make_pool_public,
    "DECLINE_POOL": _handle_decline_pool,
    "ACCEPT_POOL": _handle_accept_pool,
    "PICK_UP_RESERVE": _handle_pick_up_reserve,
    "DISCARD_HOLDING": _handle_discard_holding,
    "DECLINE_PICKUP": _handle_decline_pickup,
    "BURN_RESERVE_FOR_SWAP": _handle_burn_reserve_for_swap,
    "TRIGGER_MARKET_CORRECTION": _handle_trigger_market_correction,
    "SET_READY_TO_CLOSE": _handle_set_ready_to_close,
}


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _emit(game: Game, now: datetime, type_: EventType, *, actor: str | None, payload: dict) -> GameEvent:
    event = GameEvent(game_id=game.id, seq_no=game.next_seq_no, type=type_, actor_game_player_id=actor, payload=payload, created_at=now)
    game.next_seq_no += 1
    return event


def _resolve_proposal(game: Game, proposal: Proposal, reason: ProposalResolutionReason, resolved_by: str | None, now: datetime) -> GameEvent:
    # Settles the PROPOSER's own locked liability (never the accepter's --
    # that's charged separately, fresh, in _handle_accept_proposal). A
    # liability of 0 never committed anything, so there's nothing to
    # settle here at all.
    if proposal.initiator_influence_liability == 1:
        initiator = game.player_by_id(proposal.swap.initiator_player_id)
        initiator.influence_committed -= 1
        if reason is ProposalResolutionReason.EXECUTED:
            initiator.influence_spent += 1
        else:
            initiator.influence_available += 1
    proposal.status = ResolutionStatus.RESOLVED
    proposal.resolved_at_seq_no = game.next_seq_no
    proposal.resolved_by_player_id = resolved_by
    proposal.resolution_reason = reason
    return _emit(game, now, EventType.PROPOSAL_RESOLVED, actor=resolved_by, payload={"proposal_id": proposal.proposal_id, "reason": reason.value})


def _resolve_pool(game: Game, pool: Pool, reason: PoolResolutionReason, resolved_by: str | None, now: datetime, *, spend: bool) -> GameEvent:
    # Settles the pool INITIATOR's own locked liability -- entirely
    # separate from whoever accepts it, who's charged fresh in
    # _handle_accept_pool. A liability of 0 never committed anything.
    if pool.initiator_influence_liability == 1:
        initiator = game.player_by_id(pool.swap.initiator_player_id)
        initiator.influence_committed -= 1
        if spend:
            initiator.influence_spent += 1
        else:
            initiator.influence_available += 1
    pool.status = ResolutionStatus.RESOLVED
    pool.resolved_at_seq_no = game.next_seq_no
    pool.resolved_by_player_id = resolved_by
    pool.resolution_reason = reason
    return _emit(
        game,
        now,
        EventType.POOL_RESOLVED,
        actor=resolved_by,
        payload={"pool_id": pool.pool_id, "reason": reason.value},
    )


def _execute_swap(
    game: Game,
    swap: SwapIntent,
    now: datetime,
    *,
    exclude_proposal_id: str | None = None,
    exclude_pool_id: str | None = None,
) -> list[GameEvent]:
    """The sole choke point where two entities' positions ever change --
    every negotiated or unilateral swap funnels through here (see the
    market-direction-reversal design writeup). After moving the market,
    scans every OTHER open negotiation referencing either moved entity for
    a crossed direction and voids it. exclude_proposal_id/exclude_pool_id
    skip the negotiation this very swap belongs to (if any) -- right after
    its own swap, a fresh direction check would always look "crossed" (the
    entity that was rising just took the better position), which is the
    negotiation succeeding as promised, not a violation."""
    a = _market_entity(game, swap.entity_a)
    b = _market_entity(game, swap.entity_b)
    a.position, b.position = b.position, a.position
    events: list[GameEvent] = [
        _emit(
            game,
            now,
            EventType.SWAP_EXECUTED,
            actor=swap.initiator_player_id,
            payload={"entity_a": swap.entity_a, "entity_b": swap.entity_b, "position_a": a.position, "position_b": b.position},
        )
    ]
    events += _invalidate_crossed_negotiations(game, swap.entity_a, swap.entity_b, now, exclude_proposal_id, exclude_pool_id)
    return events


def _invalidate_crossed_negotiations(
    game: Game,
    moved_a: str,
    moved_b: str,
    now: datetime,
    exclude_proposal_id: str | None,
    exclude_pool_id: str | None,
) -> list[GameEvent]:
    """A swap only ever changes the positions of its own two entities, so
    only a negotiation whose own two entities intersect {moved_a, moved_b}
    could possibly have crossed -- everything else is untouched by
    construction and skipped without even a direction check."""
    moved = {moved_a, moved_b}
    events: list[GameEvent] = []

    for proposal in list(game.proposals.values()):
        if proposal.status != ResolutionStatus.OPEN or proposal.proposal_id == exclude_proposal_id:
            continue
        if {proposal.swap.entity_a, proposal.swap.entity_b} & moved and _direction_crossed(game, proposal.swap):
            events.append(_resolve_proposal(game, proposal, ProposalResolutionReason.VOIDED_MARKET_SWUNG, None, now))
            for pool in list(game.pools.values()):
                if pool.base_proposal_id == proposal.proposal_id and pool.status == ResolutionStatus.OPEN:
                    events.append(_resolve_pool(game, pool, PoolResolutionReason.BASE_PROPOSAL_VOIDED, None, now, spend=False))

    for pool in list(game.pools.values()):
        if pool.status != ResolutionStatus.OPEN or pool.pool_id == exclude_pool_id:
            continue
        if {pool.swap.entity_a, pool.swap.entity_b} & moved and _direction_crossed(game, pool.swap):
            events.append(_resolve_pool(game, pool, PoolResolutionReason.VOIDED_MARKET_SWUNG, None, now, spend=False))

    # A pending Market Correction's own moves are real SwapIntents with
    # locked directions too -- same crossing check, same infrastructure.
    # When TRIGGER_MARKET_CORRECTION executes its own two moves, it
    # clears game.pending_market_correction *before* calling
    # _execute_swap for either one, so this is always a no-op for a
    # correction resolving itself (there's nothing left here to find) --
    # see _handle_trigger_market_correction. This only ever fires for an
    # *unrelated* swap (a unilateral burn) crossing a still-pending
    # correction. A negotiated deal never reaches this path either -- see
    # _handle_accept_proposal/_handle_accept_pool, which resolve any
    # pending correction as MARKET_RESUMED before their own swap runs.
    pending_correction = game.pending_market_correction
    if pending_correction is not None and not _market_correction_still_valid(game):
        events.append(_resolve_market_correction(game, pending_correction, MarketCorrectionResolutionReason.INVALIDATED, None, now))

    return events


def _direction_crossed(game: Game, swap: SwapIntent) -> bool:
    return _rising_entity(game, swap.entity_a, swap.entity_b) != swap.rising_entity_id


def _market_correction_still_valid(game: Game) -> bool:
    """True if every move in game.pending_market_correction still holds
    its construction-time premise: locked direction hasn't crossed, the
    source is still owned by the targeted player, and the destination is
    still unowned by every player. True (vacuously) if nothing is
    pending. See the Market Correction design writeup."""
    correction = game.pending_market_correction
    if correction is None:
        return True
    for move in correction.moves:
        if _direction_crossed(game, move.swap):
            return False
        if not _owns(game, move.target_player_id, move.swap.entity_a):
            return False
        if any(_owns(game, p.game_player_id, move.swap.entity_b) for p in game.players):
            return False
    return True


def _resolve_market_correction(
    game: Game,
    correction: PendingMarketCorrection,
    reason: MarketCorrectionResolutionReason,
    resolved_by: str | None,
    now: datetime,
) -> GameEvent:
    """Takes `correction` explicitly rather than reading
    game.pending_market_correction itself -- TRIGGER_MARKET_CORRECTION
    needs to clear that field *before* executing either of the
    correction's own swaps (see the self-invalidation fix in
    _handle_trigger_market_correction), so by the time this runs for the
    triggered case the field is already None. Idempotent either way:
    always (re-)clears it and pushes the cooldown forward. The payload
    always carries the full `moves` detail regardless of reason -- see
    projections.project_events' bespoke redaction for MARKET_CORRECTION_RESOLVED,
    which hides it live unless reason == "triggered" (or the audience is
    Replay), so a future Replay build gets full transparency for free."""
    game.pending_market_correction = None
    game.market_correction_cooldown_until = now + timedelta(seconds=game.config.market_correction_cooldown_seconds)
    payload = {
        "correction_id": correction.correction_id,
        "reason": reason.value,
        "moves": [
            {"target_player_id": m.target_player_id, "entity_a": m.swap.entity_a, "entity_b": m.swap.entity_b} for m in correction.moves
        ],
    }
    return _emit(game, now, EventType.MARKET_CORRECTION_RESOLVED, actor=resolved_by, payload=payload)


def _projected_value(game: Game, player_id: str) -> int:
    """Duplicated from projections._portfolio_value rather than imported
    -- same reasoning projections.py already gives for duplicating
    _rising_entity/_owns from here instead of importing across the
    engine<->projections boundary: keeps this module's only dependency
    on projections local and explicit (the one existing exception,
    `from gotiate.domain.projections import PlayerAudience, project` in
    _handle_pick_up_reserve, is for the *public* projection API, not a
    private helper). Server-internal only -- never shown to players,
    never mentioned in any Market Correction copy. See the design
    writeup's confidentiality rule."""
    n = len(game.market)
    positions = {eid: m.position for eid, m in game.market.items()}
    holdings = [h for h in game.holdings.values() if h.owner_player_id == player_id and h.zone == HoldingZone.PORTFOLIO]
    return sum(n - positions[h.entity_id] + 1 for h in holdings)


def _clamp_displacement(raw: float, market_size: int) -> int:
    return max(1, min(market_size - 1, round(raw)))


def _find_correction_destination(game: Game, source_position: int, target_displacement: int, unavailable: set[str]) -> str | None:
    """Nearest-legal-destination search, in the exact preference order
    from the Market Correction design writeup: closer to the exact
    target position wins, among every position strictly below (worse
    than) the source that isn't in `unavailable` (owned by either player,
    or already claimed by this correction's other move). A single pass
    over every position below the source is cheap at this market's size
    (<=17) and naturally finds the exact target first when it's
    available, since distance 0 always wins ties."""
    market_size = len(game.market)
    entity_by_position = {m.position: eid for eid, m in game.market.items()}
    exact_target = min(market_size, source_position + target_displacement)
    best: str | None = None
    best_distance: int | None = None
    for pos in range(source_position + 1, market_size + 1):
        candidate = entity_by_position.get(pos)
        if candidate is None or candidate in unavailable:
            continue
        distance = abs(pos - exact_target)
        if best_distance is None or distance < best_distance:
            best, best_distance = candidate, distance
    return best


def _construct_one_correction_move(
    game: Game, player_id: str, target_displacement: int, unavailable_destinations: set[str], rng: random.Random
) -> MarketCorrectionMove | None:
    """Targeting rules, locked: start from the player's top two *distinct*
    owned entities by current position. A doubled/anchor holding (owned
    x2+) is excluded if the other of the top two is singly-owned; only
    when *both* are doubled does a double become eligible. Selection
    within the eligible set is uniform-random, never damage-optimized.
    If the randomly-drawn candidate has no legal destination, the
    *other* eligible candidate is tried before giving up -- never chains
    multiple swaps to force an exact displacement."""
    positions = {eid: m.position for eid, m in game.market.items()}
    owned_counts: dict[str, int] = {}
    for h in game.holdings.values():
        if h.owner_player_id == player_id and h.zone == HoldingZone.PORTFOLIO:
            owned_counts[h.entity_id] = owned_counts.get(h.entity_id, 0) + 1
    top_two = sorted(owned_counts, key=lambda eid: positions[eid])[:2]
    if not top_two:
        return None

    singly_owned = [eid for eid in top_two if owned_counts[eid] == 1]
    eligible = singly_owned if singly_owned else top_two

    # Owned by either player, in PORTFOLIO -- a 2-player game, so this is
    # exactly "owned by neither" once combined with the caller's own
    # already-claimed-destination exclusions.
    owned_by_anyone = {h.entity_id for h in game.holdings.values() if h.zone == HoldingZone.PORTFOLIO}
    unavailable = owned_by_anyone | unavailable_destinations

    candidates = list(eligible)
    rng.shuffle(candidates)
    for source_id in candidates:
        destination = _find_correction_destination(game, positions[source_id], target_displacement, unavailable)
        if destination is not None:
            return MarketCorrectionMove(
                target_player_id=player_id,
                swap=SwapIntent(entity_a=source_id, entity_b=destination, initiator_player_id=player_id, rising_entity_id=destination),
            )
    return None


def _market_correction_target_displacements(game: Game) -> tuple[str, str, dict[str, int]]:
    """The bounded score-gap -> displacement formula, pure and rng-free:
    gap = |projected leader - projected trailer| (server-internal only,
    see _projected_value); spread scales linearly with gap up to
    gap_saturation, then holds flat at max_spread; leader gets base -
    spread/2, trailer gets base + spread/2, both clamped to
    [1, market_size - 1]. Split out from _construct_market_correction so
    the formula itself is unit-testable independent of destination
    -search/market-boundary effects (an achieved displacement can be
    truncated by hitting the market edge -- that's _find_correction_destination's
    concern, not this formula's). See the Market Correction design writeup."""
    players = game.players
    assert len(players) == 2
    market_size = len(game.market)
    p0_id, p1_id = players[0].game_player_id, players[1].game_player_id
    v0, v1 = _projected_value(game, p0_id), _projected_value(game, p1_id)
    leader_id, trailer_id = (p0_id, p1_id) if v0 >= v1 else (p1_id, p0_id)
    gap = abs(v0 - v1)

    base = game.config.market_correction_base_displacement_fraction * market_size
    max_spread = game.config.market_correction_max_spread_fraction * market_size
    gap_saturation = game.config.market_correction_gap_saturation_fraction * market_size
    spread = max_spread * min(1.0, gap / gap_saturation) if gap_saturation > 0 else 0.0
    target_displacement = {
        leader_id: _clamp_displacement(base - spread / 2, market_size),
        trailer_id: _clamp_displacement(base + spread / 2, market_size),
    }
    return leader_id, trailer_id, target_displacement


def _construct_market_correction(game: Game, now: datetime, rng: random.Random) -> PendingMarketCorrection | None:
    """2-player-only. Builds a hidden, fixed correction -- one downward
    move per player -- before either player has chosen anything; that's
    load-bearing, not incidental (accepting is a wager on a fixed unknown
    outcome, never a roll the server makes after seeing the acceptance).
    Severity is derived from the private Projected Value gap and never
    shown; returns None if either player's eligible top-two holdings have
    no legal destination this cycle (expected to be exceptionally rare).
    See the Market Correction design writeup."""
    players = game.players
    _leader_id, _trailer_id, target_displacement = _market_correction_target_displacements(game)

    used_destinations: set[str] = set()
    moves: list[MarketCorrectionMove] = []
    for player in players:
        move = _construct_one_correction_move(game, player.game_player_id, target_displacement[player.game_player_id], used_destinations, rng)
        if move is None:
            return None
        used_destinations.add(move.swap.entity_b)
        moves.append(move)

    return PendingMarketCorrection(
        correction_id=new_id(),
        offered_at=now,
        expires_at=now + timedelta(seconds=game.config.market_correction_offer_seconds),
        moves=moves,
    )


def _market_entity(game: Game, entity_id: str) -> MarketEntity:
    m = game.market.get(entity_id)
    if m is None:
        raise IllegalCommandError(f"unknown market entity {entity_id!r}")
    return m


def _require_entities_exist(game: Game, *entity_ids: str) -> None:
    for e in entity_ids:
        if e not in game.market:
            raise IllegalCommandError(f"unknown market entity {e!r}")


def _require_negotiation(game: Game) -> None:
    if game.phase != GamePhase.NEGOTIATION:
        raise IllegalCommandError(f"not legal in phase {game.phase}")


def _require_no_pending_pickup(game: Game, actor_game_player_id: str | None) -> None:
    player = game.player_by_id(actor_game_player_id)
    if player.pending_pickup is not None:
        raise IllegalCommandError("finish your pending pickup first")


def _require_open_proposal(game: Game, proposal_id: str) -> Proposal:
    proposal = game.proposals.get(proposal_id)
    if proposal is None or proposal.status != ResolutionStatus.OPEN:
        raise IllegalCommandError("proposal is not open")
    return proposal


def _require_open_pool(game: Game, pool_id: str) -> Pool:
    pool = game.pools.get(pool_id)
    if pool is None or pool.status != ResolutionStatus.OPEN:
        raise IllegalCommandError("pool is not open")
    return pool
