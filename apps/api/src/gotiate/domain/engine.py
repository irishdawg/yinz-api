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
    Holding,
    HoldingZone,
    MarketEntity,
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

    # DISCARD_HOLDING is deliberately exempt — it's bound to pending_pickup_id
    # instead, since the rest of the game keeps moving during a player's lock.
    if command_type != "DISCARD_HOLDING" and expected_version is not None and expected_version != game.version:
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

    if game.phase == GamePhase.NEGOTIATION and game.started_at is not None and game.max_duration_s is not None:
        if now >= game.started_at + timedelta(seconds=game.max_duration_s):
            events += close_market(game, CloseReason.TIME_EXPIRED, now)

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

    for player in game.players:
        if player.pending_pickup is not None:
            events += _fail_pending_pickup(game, player, PickupFailureReason.MARKET_CLOSED, now)

    for holding in game.holdings.values():
        if holding.zone == HoldingZone.RESERVE_UNREVEALED:
            holding.zone = HoldingZone.SURRENDERED_UNUSED

    game.waterline_revealed = True
    events.append(
        _emit(game, now, EventType.WATERLINE_REVEALED, actor=None, payload={"entity_id": game.waterline_entity_id})
    )
    events.append(_emit(game, now, EventType.PORTFOLIOS_REVEALED, actor=None, payload={}))

    result = score_game(game)
    events.append(_emit(game, now, EventType.GAME_SCORED, actor=None, payload=result))

    game.scored_at = now
    game.phase = GamePhase.SCORED
    events.append(_emit(game, now, EventType.GAME_ENDED, actor=None, payload={}))

    return events


def score_game(game: Game) -> dict:
    """waterline_baseline_v1 — count at or above the line, full lexicographic
    tiebreak, exact ties declared a shared win (see decision log §10)."""
    assert game.waterline_entity_id is not None
    waterline_position = game.market[game.waterline_entity_id].position
    positions = {eid: m.position for eid, m in game.market.items()}

    results = []
    for player in game.players:
        holdings = [
            h for h in game.holdings.values() if h.owner_player_id == player.game_player_id and h.zone == HoldingZone.PORTFOLIO
        ]
        qualifying = sum(1 for h in holdings if positions[h.entity_id] <= waterline_position)
        ranks_sorted = sorted(positions[h.entity_id] for h in holdings)  # ascending position = strongest first
        results.append(
            {"game_player_id": player.game_player_id, "qualifying_count": qualifying, "tiebreak_ranks": ranks_sorted}
        )

    max_qualifying = max(r["qualifying_count"] for r in results)
    contenders = [r for r in results if r["qualifying_count"] == max_qualifying]
    contenders.sort(key=lambda r: r["tiebreak_ranks"])
    best_ranks = contenders[0]["tiebreak_ranks"]
    winners = [r["game_player_id"] for r in contenders if r["tiebreak_ranks"] == best_ranks]

    return {
        "waterline_entity_id": game.waterline_entity_id,
        "waterline_position": waterline_position,
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

    game.waterline_entity_id = rng.choice(entity_ids)
    events.append(_emit(game, now, EventType.WATERLINE_SELECTED, actor=None, payload={}))

    game.max_duration_s = game.config.max_clock_seconds_by_players[n]
    game.started_at = now
    game.unilateral_cutoff_at = now + timedelta(
        seconds=game.max_duration_s * (1 - game.config.unilateral_cutoff_fraction)
    )
    game.close_threshold = game.config.close_threshold(n)
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
        swap=SwapIntent(entity_a=entity_a, entity_b=entity_b, initiator_player_id=actor_game_player_id),
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


def _handle_accept_proposal(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    if proposal.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("cannot accept your own proposal")

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

    events = [_execute_swap(game, proposal.swap, now)]
    events.append(_resolve_proposal(game, proposal, ProposalResolutionReason.EXECUTED, actor_game_player_id, now))
    events += resolve_sibling_pools(game, proposal, actor_game_player_id, now)
    return events


def _handle_create_pool(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_pickup(game, actor_game_player_id)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    if proposal.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("the proposer cannot pool their own proposal")

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
        swap=SwapIntent(entity_a=entity_c, entity_b=entity_d, initiator_player_id=actor_game_player_id),
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
    return [_resolve_pool(game, pool, PoolResolutionReason.WITHDRAWN_BY_INITIATOR, actor_game_player_id, now, spend=True)]


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

    events = [_execute_swap(game, base.swap, now), _execute_swap(game, pool.swap, now)]
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
    # into -- attaching pp is what actually locks it.
    pp.cached_view = project(game, PlayerAudience(actor_game_player_id))
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

    return [
        _emit(
            game,
            now,
            EventType.PICKUP_COMPLETED,
            actor=actor_game_player_id,
            payload={"pending_pickup_id": pp.pending_pickup_id, "reserve_holding_id": reserve.holding_id, "discarded_holding_id": discard_id},
        )
    ]


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
    swap = SwapIntent(entity_a=entity_a, entity_b=entity_b, initiator_player_id=actor_game_player_id)
    events = [_execute_swap(game, swap, now)]
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


_HANDLERS: dict[str, Callable[..., list[GameEvent]]] = {
    "CANCEL_GAME": _handle_cancel_game,
    "EXTEND_LOBBY_TIMER": _handle_extend_lobby_timer,
    "START_GAME": _handle_start_game,
    "PROPOSE_SWAP": _handle_propose_swap,
    "WITHDRAW_PROPOSAL": _handle_withdraw_proposal,
    "ACCEPT_PROPOSAL": _handle_accept_proposal,
    "CREATE_POOL": _handle_create_pool,
    "WITHDRAW_POOL": _handle_withdraw_pool,
    "MAKE_POOL_PUBLIC": _handle_make_pool_public,
    "DECLINE_POOL": _handle_decline_pool,
    "ACCEPT_POOL": _handle_accept_pool,
    "PICK_UP_RESERVE": _handle_pick_up_reserve,
    "DISCARD_HOLDING": _handle_discard_holding,
    "BURN_RESERVE_FOR_SWAP": _handle_burn_reserve_for_swap,
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


def _execute_swap(game: Game, swap: SwapIntent, now: datetime) -> GameEvent:
    a = _market_entity(game, swap.entity_a)
    b = _market_entity(game, swap.entity_b)
    a.position, b.position = b.position, a.position
    return _emit(
        game,
        now,
        EventType.SWAP_EXECUTED,
        actor=swap.initiator_player_id,
        payload={"entity_a": swap.entity_a, "entity_b": swap.entity_b, "position_a": a.position, "position_b": b.position},
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
