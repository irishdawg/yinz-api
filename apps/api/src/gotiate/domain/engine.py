"""The rules engine — domain model §04. Pure, synchronous, zero I/O.

Every mutation to a `Game` goes through here: lock -> apply_due_time_transitions
-> validate command -> execute -> append events -> version++ (domain model §08).
The lock and persistence live in `api/routes.py` and `persistence/`; this module
only ever sees one `Game` at a time and mutates it in place, returning the list
of newly emitted events.

Cadence/economy redesign (prototype branch): checkpoint 1 removed Influence,
Market Correction, the gameplay clock, Accept Lock, the 5-6 player
multi-accept threshold, and the old Reserve/Pickup/unilateral-burn commands.
Checkpoint 2 wires up the actual cadence: PROPOSE_SWAP now consumes a Move
and is illegal while another negotiation is already active table-wide;
WITHDRAW_PROPOSAL is gone entirely (spending a Move commits the table);
Pass is fully public and narrows the active participant set; Boost expiry
and the Haircut reveal are now Move-driven; the game can now end via Move
exhaustion, not just Ready-to-Close. Checkpoint 3 adds Arbitration: once a
negotiation has narrowed to exactly two active participants (the opener
plus one remaining responder), either may call it, starting an irreversible
20-second last-chance window with a weighted machine draw at the end,
secretly influenced by the jury of already-passed players. Checkpoint 4
wires up Boosts: Concentrate (duplicate an owned entity up to the x3
cap), Force Swap (an unlimited unilateral market move, reusing
_execute_swap directly), and Draw/Refresh (a private, timed decision
window revealing one uniformly-random zero-owned entity, spent
regardless of whether the player keeps it).
"""

from __future__ import annotations

import random
import string
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from gotiate.domain.entities import (
    ArbitrationChoice,
    ArbitrationResolutionReason,
    BoostDrawFailureReason,
    CancellationReason,
    CloseReason,
    Game,
    GameConfig,
    GamePhase,
    GamePlayer,
    HaircutProfile,
    Holding,
    HoldingZone,
    MarketEntity,
    PendingArbitration,
    PendingBoostDraw,
    Pool,
    PoolResolutionReason,
    PoolVisibility,
    Proposal,
    ProposalResolutionReason,
    ProtectedPair,
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
        last_activity_at=now,
    )
    host = GamePlayer(
        game_player_id=new_id(),
        auth_user_id=actor_auth_user_id,
        seat=0,
        display_name=display_name,
        is_golden_name=is_golden_name,
        moves_remaining=game.config.starting_moves,
        boosts_remaining=game.config.starting_boosts,
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
        moves_remaining=game.config.starting_moves,
        boosts_remaining=game.config.starting_boosts,
    )
    game.players.append(player)
    events = [_emit(game, now, EventType.PLAYER_JOINED, actor=player.game_player_id, payload={"seat": player.seat})]
    game.version += 1
    return player, events


# --------------------------------------------------------------------------
# Command dispatch
# --------------------------------------------------------------------------

# RESOLVE_BOOST_DRAW/DECLINE_BOOST_DRAW mirror the old DISCARD_HOLDING/
# DECLINE_PICKUP exemption: a client frozen on PendingBoostDraw.cached_view
# for the length of the decision window can't know the live version, so
# these two are exempt from the optimistic-concurrency check the same way.
_VERSION_EXEMPT_COMMANDS: frozenset[str] = frozenset({"RESOLVE_BOOST_DRAW", "DECLINE_BOOST_DRAW"})


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

    if command_type not in _VERSION_EXEMPT_COMMANDS and expected_version is not None and expected_version != game.version:
        err = StaleVersionError(f"expected version {expected_version}, game is at {game.version}")
        err.partial_events = events
        raise err

    try:
        new_events = handler(game, payload=payload, actor_game_player_id=actor_game_player_id, now=now)
    except DomainError as exc:
        exc.partial_events = events
        raise

    # A successfully handled command is "activity" regardless of whether it
    # produced any events (a legal no-op still proves someone's here) --
    # deliberately not tied to the version bump below, so this bookkeeping
    # field never breaks the documented "a no-op command doesn't bump the
    # game version" invariant. A *rejected* command (the except branch
    # above) never reaches this line -- only genuine player action counts,
    # see CloseReason.ABANDONED.
    game.last_activity_at = now

    if new_events:
        game.version += 1
    events += new_events

    # Checked after every command, not just Move-spending ones -- cheap (a
    # plain scan of the roster/state) and this is the one chokepoint every
    # command already passes through. Only PROPOSE_SWAP can ever change
    # moves_remaining or active_proposal_id today, so this is a no-op the
    # vast majority of the time -- same discipline the old zero-Influence
    # top-up check used.
    if game.phase == GamePhase.NEGOTIATION:
        side_effects: list[GameEvent] = []
        side_effects += _maybe_expire_boosts(game, now)
        side_effects += _maybe_reveal_haircut(game, now)
        if side_effects:
            game.version += 1
        events += side_effects
        # Checked last -- a game-ending close should see the freshest
        # possible state (including the two checks just above), and must
        # not itself be short-circuited by them.
        if game.phase == GamePhase.NEGOTIATION:
            close_events = _maybe_close_on_moves_exhausted(game, now)
            if close_events:
                game.version += 1
            events += close_events

    return events


# --------------------------------------------------------------------------
# Time-driven transitions — run ahead of every command (§04, §08)
# --------------------------------------------------------------------------


def apply_due_time_transitions(game: Game, now: datetime) -> list[GameEvent]:
    """NOTE (cadence/economy redesign): there is no gameplay clock. The
    lobby reminder/grace auto-cancel, the NEGOTIATION-phase abandonment
    backstop (CloseReason.ABANDONED), Arbitration's own 20-second
    last-chance window, and each player's own short Draw/Refresh decision
    window are the only time-driven transitions in this game -- every
    other trigger (Boost expiry, the Haircut reveal, the Move-exhaustion
    endgame) is a direct, synchronous consequence of a command, not
    something that needs polling to notice."""
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

    # Checked first, before Arbitration/Boost-draw timeouts below --
    # close_market's own generic MARKET_CLOSED sequence already resolves
    # any pending Arbitration/Boost draw as part of closing, so there's
    # nothing left for those checks to do once the table's gone quiet long
    # enough to count as abandoned. See CloseReason.ABANDONED.
    if now - game.last_activity_at >= timedelta(seconds=game.config.negotiation_abandonment_seconds):
        return close_market(game, CloseReason.ABANDONED, now)

    pending = _pending_arbitration(game)
    if pending is not None and now >= pending.resolves_at:
        proposal = game.proposals[game.active_proposal_id]  # type: ignore[index]
        events += _resolve_arbitration_via_machine(game, proposal, now, random.Random())

    for player in game.players:
        pending_draw = player.pending_boost_draw
        if pending_draw is not None and now >= pending_draw.decision_deadline_at:
            events += _fail_pending_boost_draw(game, player, BoostDrawFailureReason.DECISION_TIMEOUT, now)

    return events


def is_time_transition_due(game: Game, now: datetime) -> bool:
    """Side-effect-free mirror of apply_due_time_transitions' conditions --
    lets a read path (routes.get_game) decide cheaply whether it's worth
    acquiring the write lock and reapplying for real. Keep in sync with
    apply_due_time_transitions -- same conditions, no mutation."""
    if game.phase == GamePhase.LOBBY:
        return (
            game.lobby_reminder_deadline_at is not None
            and now >= game.lobby_reminder_deadline_at + timedelta(seconds=game.config.lobby_reminder_grace_seconds)
        )
    if game.phase != GamePhase.NEGOTIATION:
        return False
    if now - game.last_activity_at >= timedelta(seconds=game.config.negotiation_abandonment_seconds):
        return True
    pending = _pending_arbitration(game)
    if pending is not None and now >= pending.resolves_at:
        return True
    return any(
        p.pending_boost_draw is not None and now >= p.pending_boost_draw.decision_deadline_at for p in game.players
    )


# --------------------------------------------------------------------------
# Move-driven side effects (checkpoint 2) — each a direct, synchronous
# consequence of PROPOSE_SWAP consuming a Move, checked generically after
# every command (see handle_command). None of these are time-driven.
# --------------------------------------------------------------------------


def _maybe_expire_boosts(game: Game, now: datetime) -> list[GameEvent]:
    """Flips boosts_expired False -> True exactly once, the instant any
    SINGLE player's own moves_remaining first hits zero -- not when every
    player's does. This is the unilateral cutoff now, expressed as game
    state rather than a timer."""
    if game.boosts_expired:
        return []
    if not any(p.moves_remaining <= 0 for p in game.players):
        return []
    game.boosts_expired = True
    return [_emit(game, now, EventType.BOOSTS_EXPIRED, actor=None, payload={})]


def _maybe_reveal_haircut(game: Game, now: datetime) -> list[GameEvent]:
    """Replaces the old clock-fraction reveal trigger: fires exactly once,
    the instant cumulative Moves consumed across the table first reaches
    or crosses 50% of the initial total Move allocation
    (len(players) * starting_moves). A game that ends before crossing this
    threshold never sees it live; project() reveals the profile
    unconditionally once phase is SCORED regardless."""
    if game.haircut_profile_revealed_at is not None or game.haircut_profile is None:
        return []
    total_initial = len(game.players) * game.config.starting_moves
    if total_initial <= 0:
        return []
    consumed = total_initial - sum(p.moves_remaining for p in game.players)
    if consumed / total_initial < 0.5:
        return []
    game.haircut_profile_revealed_at = now
    return [_emit(game, now, EventType.HAIRCUT_RISK_REVEALED, actor=None, payload={})]


def _maybe_close_on_moves_exhausted(game: Game, now: datetime) -> list[GameEvent]:
    """The Move-exhaustion endgame: the game ends once every seated
    player's own moves_remaining has hit zero AND there is no active
    negotiation left to resolve. Deliberately not the same instant as
    boosts_expired (that fires on the *first* player to hit zero) -- the
    table can still spend its last few Moves, opening and resolving
    negotiations, for a while after Boosts have already expired. Only
    fires once the very last negotiation opened by the very last Move has
    itself resolved (active_proposal_id back to None)."""
    if game.active_proposal_id is not None:
        return []
    if not game.players or any(p.moves_remaining > 0 for p in game.players):
        return []
    return close_market(game, CloseReason.MOVES_EXHAUSTED, now)


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
    # Defense-in-depth: the loop below also clears this via
    # _resolve_proposal the instant the matching (still-open) proposal
    # resolves MARKET_CLOSED, but clearing it explicitly here means the
    # invariant holds even if active_proposal_id ever pointed at a
    # proposal that wasn't actually OPEN.
    game.active_proposal_id = None
    events.append(_emit(game, now, EventType.MARKET_CLOSED, actor=None, payload={"reason": reason.value}))

    for proposal in game.proposals.values():
        if proposal.status == ResolutionStatus.OPEN:
            events += _resolve_proposal(game, proposal, ProposalResolutionReason.MARKET_CLOSED, None, now)

    for pool in list(game.pools.values()):
        if pool.status == ResolutionStatus.OPEN:
            events.append(_resolve_pool(game, pool, PoolResolutionReason.MARKET_CLOSED, None, now))

    # Any player still mid-Draw/Refresh-decision when the market closes
    # never gets to choose -- the drawn entity never lands, same as a
    # decline/timeout. The Boost was already spent when the draw started.
    for player in game.players:
        if player.pending_boost_draw is not None:
            events += _fail_pending_boost_draw(game, player, BoostDrawFailureReason.MARKET_CLOSED, now)

    events.append(_emit(game, now, EventType.PORTFOLIOS_REVEALED, actor=None, payload={}))

    # The one and only random draw for this game's scoring -- see the
    # Haircut-risk design writeup's invariant. Persisted immediately;
    # everything downstream (this event's own payload, and every later
    # project()/replay call at SCORED) reads it back through the pure
    # compute_final_scores, never rerolls it.
    assert game.haircut_profile is not None
    game.realized_haircut_depth = draw_haircut_depth(game.haircut_profile, random.Random())
    result = compute_final_scores(game, game.realized_haircut_depth)
    events.append(_emit(game, now, EventType.GAME_SCORED, actor=None, payload=result))

    game.scored_at = now
    game.phase = GamePhase.SCORED
    events.append(_emit(game, now, EventType.GAME_ENDED, actor=None, payload={}))

    return events


_HAIRCUT_FIRST_DEPTH_SURVIVAL_RANGE = (0.05, 0.50)
_HAIRCUT_SECOND_DEPTH_SURVIVAL_RANGE = (0.11, 0.61)
# Every adjacent pair of depths' survival CDF must differ by at least this
# much -- without a floor, a continuous uniform draw can (and did, in real
# play) land two adjacent positions only ~1 percentage point apart, which
# reads as no real differentiation at all even though it's technically a
# valid distribution.
_HAIRCUT_MIN_ADJACENT_GAP = 0.04
# No position *inside* the risk band is ever fully safe -- real playtest
# feedback: the deepest in-band position was showing 100% survival, which
# defeats the entire point of it being "in the risk band" at all. Every
# in-band position's own cumulative survival is capped here; the
# remainder (at least 8%) always lands on the one guaranteed-unsafe-if-
# nothing-else-was outcome, one slot past the band -- see the depth-count
# note below.
_HAIRCUT_WITHIN_BAND_CEILING = 0.92


def _generate_random_haircut_profile(risk_band_depth: int, rng: random.Random) -> HaircutProfile:
    """A fresh Haircut profile, drawn at random every game rather than
    picked from a fixed list. Works entirely in the *survival* CDF
    (sum(depth_probabilities[0:p]) = P(position p survives), per
    HaircutProfile's own docstring) since that's the natural space to
    bound: monotonic by construction, so "second always greater than
    first" falls out for free rather than needing its own check.

    `risk_band_depth` is round(market_size * risk_depth_fraction) -- but
    NOT the length of depth_probabilities: the returned profile has
    risk_band_depth + 1 entries, one per in-band position plus exactly
    one more for "the wipe reached the bottom of the band and nothing
    saved it," which is what's structurally always-100%-safe one position
    past the band.

    Every in-band position's own cumulative survival is additionally
    capped at _HAIRCUT_WITHIN_BAND_CEILING (92%), so even the deepest
    in-band position keeps genuine risk, never approaching certainty
    within the band itself. Deliberately lumpy rather than a fixed step
    size between depths. Every step is still floored at
    _HAIRCUT_MIN_ADJACENT_GAP -- once there's less room left than the
    floor, the step jumps straight to the ceiling instead of squeezing in
    one more too-small increment."""
    if risk_band_depth <= 0:
        return HaircutProfile(depth_probabilities=[1.0])

    cumulative = [rng.uniform(*_HAIRCUT_FIRST_DEPTH_SURVIVAL_RANGE)]
    if risk_band_depth >= 2:
        low, high = _HAIRCUT_SECOND_DEPTH_SURVIVAL_RANGE
        cumulative.append(rng.uniform(max(low, cumulative[0] + _HAIRCUT_MIN_ADJACENT_GAP), min(high, _HAIRCUT_WITHIN_BAND_CEILING)))
    for _ in range(2, risk_band_depth):
        previous = cumulative[-1]
        room = _HAIRCUT_WITHIN_BAND_CEILING - previous
        if room <= _HAIRCUT_MIN_ADJACENT_GAP or rng.random() < 0.25:
            cumulative.append(_HAIRCUT_WITHIN_BAND_CEILING)
        else:
            cumulative.append(previous + rng.uniform(_HAIRCUT_MIN_ADJACENT_GAP, room))
    cumulative[-1] = min(cumulative[-1], _HAIRCUT_WITHIN_BAND_CEILING)  # the deepest in-band position never reaches the ceiling's own cap
    cumulative.append(1.0)  # one slot past the band -- structurally safe, absorbs whatever's left (always >= 1 - ceiling)

    depth_probabilities = [cumulative[0]] + [cumulative[i] - cumulative[i - 1] for i in range(1, len(cumulative))]
    return HaircutProfile(depth_probabilities=depth_probabilities)


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
    project()/replay call at SCORED. Never draws a depth itself, only ever
    consumes an already-persisted one."""
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
# Market-direction locking — kept unchanged by the redesign; still the sole
# mechanism protecting every surviving proposal/pool, and will protect
# Force Swap once Boosts land too.
# --------------------------------------------------------------------------


def _rising_entity(game: Game, entity_a: str, entity_b: str) -> str:
    """Whichever of the two currently holds the worse (higher) position --
    it takes the other's better position once swapped. Not assumed to be
    entity_a; PROPOSE_SWAP's payload just reflects click order, not market
    direction, so direction is always derived from live positions."""
    a = _market_entity(game, entity_a)
    b = _market_entity(game, entity_b)
    return entity_a if a.position > b.position else entity_b


def _is_protected_reversal(game: Game, entity_a: str, entity_b: str) -> bool:
    """True iff {entity_a, entity_b} is exactly the currently-protected
    pair (order-independent) -- see ProtectedPair. Only ever consulted by
    PROPOSE_SWAP/CREATE_POOL; Force Swap is deliberately exempt (a Boost
    undoing a Boost is fair, same-cost play -- see
    engine._use_boost_force_swap, which never calls this)."""
    if game.protected_pair is None:
        return False
    return {entity_a, entity_b} == {game.protected_pair.entity_a, game.protected_pair.entity_b}


# --------------------------------------------------------------------------
# The Agency Principle, written once (§01, §04)
# --------------------------------------------------------------------------


def resolve_sibling_pools(game: Game, base_proposal: Proposal, resolving_actor: str | None, now: datetime) -> list[GameEvent]:
    """resolving_actor is None for a machine-driven Arbitration outcome --
    a sibling pool can never match None as its own initiator_player_id, so
    it always falls through to PREEMPTED_BY_OTHER_ACTION for that case,
    which is exactly right (nobody "chose" this, the machine did)."""
    events: list[GameEvent] = []
    for pool in list(game.pools.values()):
        if pool.base_proposal_id != base_proposal.proposal_id or pool.status != ResolutionStatus.OPEN:
            continue
        if pool.swap.initiator_player_id == resolving_actor:
            events.append(_resolve_pool(game, pool, PoolResolutionReason.INVALIDATED_BY_INITIATOR_ACTION, resolving_actor, now))
        else:
            events.append(_resolve_pool(game, pool, PoolResolutionReason.PREEMPTED_BY_OTHER_ACTION, resolving_actor, now))
    return events


# --------------------------------------------------------------------------
# Arbitration (checkpoint 3) -- eligibility/lookup helpers shared between
# the command handlers and the machine-draw resolver below.
# --------------------------------------------------------------------------


def _pending_arbitration(game: Game) -> PendingArbitration | None:
    if game.active_proposal_id is None:
        return None
    return game.proposals[game.active_proposal_id].pending_arbitration


def _active_responder_ids(game: Game, proposal: Proposal) -> set[str]:
    """Every seated player except the proposal's own initiator, minus
    whoever has already passed -- the active-participant-set narrowing
    mechanic (§Pass) is what Arbitration eligibility is built directly on
    top of. Arbitration becomes callable the instant this set's size hits
    exactly 1 (the opener plus that one remaining responder = "exactly
    two active participants")."""
    return {p.game_player_id for p in game.players} - {proposal.swap.initiator_player_id} - proposal.passed_player_ids


def _eligible_arbitration_pool_id(game: Game, proposal: Proposal) -> str | None:
    """The one Pool eligible for the "pool" outcome, if any. Each
    non-originator may create at most one open Pool per negotiation; the
    originator can never pool their own proposal; and every OTHER
    responder has, by the time they passed, been forced to withdraw any
    Pool of their own first (_handle_pass_proposal's own check). So by
    the time Arbitration eligibility (exactly one active responder) is
    reached, there is structurally at most one still-open Pool on this
    base, and it can only belong to that one remaining responder -- no
    "which Pool" ambiguity is possible."""
    open_pools = [p for p in game.pools.values() if p.base_proposal_id == proposal.proposal_id and p.status == ResolutionStatus.OPEN]
    assert len(open_pools) <= 1, "at most one open Pool can survive to Arbitration eligibility"
    return open_pools[0].pool_id if open_pools else None


def _require_no_active_arbitration(proposal: Proposal) -> None:
    """Once Arbitration is called, its candidate set (the base proposal
    and the one eligible Pool, if any) is locked -- nothing may add a new
    Pool, remove the eligible one, or narrow the participant set further
    (a further Pass) out from under a machine draw that might still pick
    it. Settling normally (Accept) remains the one way out during the
    window; see _handle_accept_proposal/_handle_accept_pool, neither of
    which calls this."""
    if proposal.pending_arbitration is not None:
        raise IllegalCommandError("arbitration is already underway for this negotiation")


def _require_no_active_arbitration_on_pool(game: Game, pool: Pool) -> None:
    base = game.proposals.get(pool.base_proposal_id)
    if base is not None:
        _require_no_active_arbitration(base)


# --------------------------------------------------------------------------
# Boosts (checkpoint 4) -- shared helpers.
# --------------------------------------------------------------------------


def _require_no_pending_boost_draw(game: Game, actor_game_player_id: str | None) -> None:
    """Self-scoped, unlike _require_no_active_arbitration: a player mid
    their own Draw/Refresh decision is served a frozen view (see
    PendingBoostDraw), so they specifically can't be trusted to act on
    live state until they resolve it -- but this never blocks anyone
    else. Mirrors the old pending-pickup-blocks-most-commands pattern."""
    player = game.player_by_id(actor_game_player_id)
    if player.pending_boost_draw is not None:
        raise IllegalCommandError("resolve your pending Draw/Refresh decision first")


def _require_boosts_active(game: Game) -> None:
    if game.boosts_expired:
        raise IllegalCommandError("Boosts have expired")
    if _pending_arbitration(game) is not None:
        raise IllegalCommandError("Boosts are unavailable during Arbitration")


def _owns(game: Game, player_id: str, entity_id: str) -> bool:
    return any(
        h.owner_player_id == player_id and h.zone == HoldingZone.PORTFOLIO and h.entity_id == entity_id
        for h in game.holdings.values()
    )


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------


def _handle_cancel_game(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    # LOBBY-only, deliberately: once real gameplay is underway the host
    # unilaterally nuking the match for everyone else is a bad power
    # dynamic to allow. A NEGOTIATION-phase game that gets abandoned closes
    # on its own once Moves run out (or via Ready-to-Close) -- that's the
    # only escape hatch past this point.
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


def _handle_kick_player(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    """Host-only, LOBBY-only moderation escape hatch -- e.g. an offensive
    typed display name (typed names have no content-moderation filter by
    design, see CURRENT_WORK.md). Removes the target's GamePlayer entirely
    (never a soft-delete/flag) so the same auth_user_id is free to
    immediately rejoin under a different name -- game_player_private's own
    `unique (game_id, auth_user_id)` constraint would otherwise block
    exactly that. Seats are compacted (every remaining player above the
    removed seat shifts down by one) so join_game's own `seat =
    len(game.players)` assignment never collides with the gap left
    behind -- the host is always seat 0 and can never kick themselves, so
    this never touches the host's own seat."""
    if game.phase != GamePhase.LOBBY:
        raise IllegalCommandError("can only remove a player while still in the lobby")
    if actor_game_player_id != game.host_player_id:
        raise IllegalCommandError("only the host can remove a player")

    target_id = payload["game_player_id"]
    if target_id == actor_game_player_id:
        raise IllegalCommandError("the host cannot remove themselves")
    target = next((p for p in game.players if p.game_player_id == target_id), None)
    if target is None:
        raise IllegalCommandError("no such player in this game")

    game.players.remove(target)
    for p in game.players:
        if p.seat > target.seat:
            p.seat -= 1

    return [
        _emit(
            game,
            now,
            EventType.PLAYER_KICKED,
            actor=actor_game_player_id,
            payload={"removed_player_id": target_id, "removed_display_name": target.display_name},
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

    for player in game.players:
        for entity_id in starting_state.portfolios[player.game_player_id]:
            h = Holding(holding_id=new_id(), entity_id=entity_id, owner_player_id=player.game_player_id, zone=HoldingZone.PORTFOLIO)
            game.holdings[h.holding_id] = h
    # Diagnostics never shown live (PORTFOLIO_DEALT is SERVER_ONLY), visible
    # via replay once scored and queryable forever from event_ledger -- see
    # the initial-distribution-quality design writeup.
    events.append(_emit(game, now, EventType.PORTFOLIO_DEALT, actor=None, payload=starting_state.diagnostics))

    game.started_at = now
    game.close_threshold = game.config.close_threshold(n)

    # Generated fresh and locked now -- live reveal trigger is
    # _maybe_reveal_haircut (Move-driven, checked generically after every
    # command). Until it fires, the profile only becomes visible once the
    # game reaches SCORED (see project()).
    game.haircut_profile = _generate_random_haircut_profile(round(market_size * game.config.risk_depth_fraction), rng)
    events.append(_emit(game, now, EventType.HAIRCUT_PROFILE_SELECTED, actor=None, payload={}))
    game.phase = GamePhase.NEGOTIATION
    events.append(_emit(game, now, EventType.GAME_STARTED, actor=actor_game_player_id, payload={"player_count": n}))
    return events


def _handle_propose_swap(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    _require_no_pending_boost_draw(game, actor_game_player_id)
    # Single-active-negotiation constraint: at most one bare negotiation
    # open table-wide at any time. This also makes the old auto-withdraw
    # -your-own-open-proposal path and the cross-player same-pair dedup
    # scan structurally unreachable -- there is never a second proposal to
    # collide with -- so both are gone rather than kept as dead code.
    if game.active_proposal_id is not None:
        raise IllegalCommandError("a negotiation is already active")

    entity_a, entity_b = payload["entity_a"], payload["entity_b"]
    if entity_a == entity_b:
        raise IllegalCommandError("a proposal must name two different entities")
    _require_entities_exist(game, entity_a, entity_b)
    if _is_protected_reversal(game, entity_a, entity_b):
        raise IllegalCommandError("this pair was just Force Swapped and is locked against a direct reverse")

    # Opening a negotiation is the only thing that spends a Move.
    # Committed permanently the instant this succeeds -- never refunded,
    # regardless of how the negotiation eventually resolves (rule 1).
    player = game.player_by_id(actor_game_player_id)
    if player.moves_remaining < 1:
        raise IllegalCommandError("no Moves remaining")

    proposal = Proposal(
        proposal_id=new_id(),
        swap=SwapIntent(
            entity_a=entity_a,
            entity_b=entity_b,
            initiator_player_id=actor_game_player_id,
            rising_entity_id=_rising_entity(game, entity_a, entity_b),
        ),
        created_at=now,
    )
    game.proposals[proposal.proposal_id] = proposal
    player.moves_remaining -= 1
    game.active_proposal_id = proposal.proposal_id

    return [
        _emit(
            game,
            now,
            EventType.PROPOSAL_CREATED,
            actor=actor_game_player_id,
            payload={"proposal_id": proposal.proposal_id, "entity_a": entity_a, "entity_b": entity_b},
        )
    ]


def _all_others_passed(game: Game, proposal: Proposal) -> bool:
    """True once every seated player except the proposal's own initiator
    has PASS_PROPOSAL'd it -- mathematically dead, nobody rejected it."""
    others = {p.game_player_id for p in game.players} - {proposal.swap.initiator_player_id}
    return others <= proposal.passed_player_ids


def _pool_eligible_accepter_ids(game: Game, pool: Pool, base: Proposal) -> set[str]:
    """Players who can still accept this Pool under its current visibility.

    A private Pool has exactly one target: the base proposer. A public
    Pool is open to every seated player except its own initiator and anyone
    who already left the base negotiation via PASS_PROPOSAL.
    """
    if pool.visibility is PoolVisibility.PRIVATE:
        return {base.swap.initiator_player_id}
    return {p.game_player_id for p in game.players} - {pool.swap.initiator_player_id} - base.passed_player_ids


def _all_pool_accepters_passed(game: Game, pool: Pool, base: Proposal) -> bool:
    return _pool_eligible_accepter_ids(game, pool, base) <= pool.passed_player_ids


def _handle_pass_proposal(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    _require_no_active_arbitration(proposal)
    _require_no_pending_boost_draw(game, actor_game_player_id)
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

    # Public and permanent -- this is the active-participant-set narrowing
    # mechanic itself. A passed player can no longer Accept/Pool this
    # proposal, but keeps seeing it (see projections.project(), which no
    # longer omits a proposal from a player who has passed it).
    proposal.passed_player_ids.add(actor_game_player_id)
    events = [_emit(game, now, EventType.PROPOSAL_PASSED, actor=actor_game_player_id, payload={"proposal_id": proposal.proposal_id})]

    # Passing the base also removes this player from every public Pool's
    # accepter set. That can make an independently-tracked public Pool
    # mathematically dead even though this player never passed that Pool
    # directly.
    for pool in game.pools.values():
        if (
            pool.base_proposal_id == proposal.proposal_id
            and pool.status == ResolutionStatus.OPEN
            and pool.visibility is PoolVisibility.PUBLIC
            and _all_pool_accepters_passed(game, pool, proposal)
        ):
            events.append(_resolve_pool(game, pool, PoolResolutionReason.EXPIRED_ALL_PASSED, None, now))

    # By construction, no Pool can be open here: a passed player can never
    # hold one (blocked above) and can never create a new one (proposals
    # are only poolable by players who haven't passed -- see
    # _handle_create_pool). So this is a pure status flip, never a cascade.
    if _all_others_passed(game, proposal):
        events += _resolve_proposal(game, proposal, ProposalResolutionReason.EXPIRED_ALL_PASSED, None, now)
    return events


def _handle_accept_proposal(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    _require_no_pending_boost_draw(game, actor_game_player_id)
    if proposal.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("cannot accept your own proposal")
    if actor_game_player_id in proposal.passed_player_ids:
        raise IllegalCommandError("you passed this proposal and can no longer accept it")

    events: list[GameEvent] = []
    events += _execute_swap(game, proposal.swap, now, exclude_proposal_id=proposal.proposal_id)
    events += _resolve_proposal(game, proposal, ProposalResolutionReason.EXECUTED, actor_game_player_id, now)
    events += resolve_sibling_pools(game, proposal, actor_game_player_id, now)
    return events


def _handle_create_pool(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    proposal = _require_open_proposal(game, payload["proposal_id"])
    _require_no_active_arbitration(proposal)
    _require_no_pending_boost_draw(game, actor_game_player_id)
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
    if _is_protected_reversal(game, entity_c, entity_d):
        raise IllegalCommandError("this pair was just Force Swapped and is locked against a direct reverse")

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

    pool = Pool(
        pool_id=new_id(),
        base_proposal_id=proposal.proposal_id,
        swap=SwapIntent(
            entity_a=entity_c,
            entity_b=entity_d,
            initiator_player_id=actor_game_player_id,
            rising_entity_id=_rising_entity(game, entity_c, entity_d),
        ),
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
    pool = _require_open_pool(game, payload["pool_id"])
    _require_no_active_arbitration_on_pool(game, pool)
    _require_no_pending_boost_draw(game, actor_game_player_id)
    if pool.swap.initiator_player_id != actor_game_player_id:
        raise IllegalCommandError("only the pool's initiator can withdraw it")
    return [_resolve_pool(game, pool, PoolResolutionReason.WITHDRAWN_BY_INITIATOR, actor_game_player_id, now)]


def _handle_make_pool_public(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    pool = _require_open_pool(game, payload["pool_id"])
    _require_no_active_arbitration_on_pool(game, pool)
    _require_no_pending_boost_draw(game, actor_game_player_id)
    if pool.swap.initiator_player_id != actor_game_player_id:
        raise IllegalCommandError("only the pool's initiator can make it public")
    if pool.visibility is PoolVisibility.PUBLIC:
        raise IllegalCommandError("pool is already public")
    pool.visibility = PoolVisibility.PUBLIC
    return [_emit(game, now, EventType.POOL_MADE_PUBLIC, actor=actor_game_player_id, payload={"pool_id": pool.pool_id})]


def _handle_pass_pool(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    pool = _require_open_pool(game, payload["pool_id"])
    base = _require_open_proposal(game, pool.base_proposal_id)
    _require_no_active_arbitration(base)
    _require_no_pending_boost_draw(game, actor_game_player_id)
    if pool.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("cannot pass your own Pool; withdraw it instead")
    if actor_game_player_id in base.passed_player_ids:
        raise IllegalCommandError("you passed the base proposal and can no longer act on its Pools")
    if pool.visibility is PoolVisibility.PRIVATE and base.swap.initiator_player_id != actor_game_player_id:
        raise IllegalCommandError("only the base proposer may pass a private Pool")
    if actor_game_player_id in pool.passed_player_ids:
        raise IllegalCommandError("you already passed this Pool")

    pool.passed_player_ids.add(actor_game_player_id)
    events = [_emit(game, now, EventType.POOL_PASSED, actor=actor_game_player_id, payload={"pool_id": pool.pool_id})]
    if _all_pool_accepters_passed(game, pool, base):
        events.append(_resolve_pool(game, pool, PoolResolutionReason.EXPIRED_ALL_PASSED, actor_game_player_id, now))
    return events


def _handle_accept_pool(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    pool = _require_open_pool(game, payload["pool_id"])
    base = _require_open_proposal(game, pool.base_proposal_id)
    _require_no_pending_boost_draw(game, actor_game_player_id)

    if pool.swap.initiator_player_id == actor_game_player_id:
        raise IllegalCommandError("cannot accept your own pool")
    if pool.visibility is PoolVisibility.PRIVATE and actor_game_player_id != base.swap.initiator_player_id:
        raise IllegalCommandError("only the base proposer may accept a private pool")
    if actor_game_player_id in base.passed_player_ids:
        raise IllegalCommandError("you passed this proposal and can no longer accept a Pool on it")
    if actor_game_player_id in pool.passed_player_ids:
        raise IllegalCommandError("you passed this Pool and can no longer accept it")

    events: list[GameEvent] = []
    events += _execute_swap(game, base.swap, now, exclude_proposal_id=base.proposal_id, exclude_pool_id=pool.pool_id)
    events += _execute_swap(game, pool.swap, now, exclude_proposal_id=base.proposal_id, exclude_pool_id=pool.pool_id)
    events += _resolve_proposal(game, base, ProposalResolutionReason.EXECUTED, actor_game_player_id, now)
    events.append(_resolve_pool(game, pool, PoolResolutionReason.EXECUTED, actor_game_player_id, now))
    events += resolve_sibling_pools(game, base, actor_game_player_id, now)
    return events


def _handle_call_arbitration(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    """Either of the final two active participants may call it; the other
    has no veto. Irreversible -- there is no UNCALL, and _require_no_active
    _arbitration blocks every other command that could otherwise alter the
    candidate set out from under it. Forces the one eligible Pool (if
    still private) public on the way in -- jurors need to know what's
    actually on the table for a secret vote to mean anything."""
    _require_negotiation(game)
    if game.active_proposal_id is None:
        raise IllegalCommandError("no active negotiation")
    proposal = game.proposals[game.active_proposal_id]
    _require_no_active_arbitration(proposal)
    _require_no_pending_boost_draw(game, actor_game_player_id)

    active_responders = _active_responder_ids(game, proposal)
    if len(active_responders) != 1:
        raise IllegalCommandError("arbitration requires the negotiation to have narrowed to exactly two active participants")
    remaining_responder = next(iter(active_responders))
    final_two = {proposal.swap.initiator_player_id, remaining_responder}
    if actor_game_player_id not in final_two:
        raise IllegalCommandError("only the two active participants may call arbitration")

    caller_role = "originator" if actor_game_player_id == proposal.swap.initiator_player_id else "other"
    base_weights = dict(game.config.arbitration_base_weights[caller_role])

    events: list[GameEvent] = []
    eligible_pool_id = _eligible_arbitration_pool_id(game, proposal)
    if eligible_pool_id is None:
        # Nothing to renormalize yet -- the draw itself renormalizes
        # whatever's left. Just drop the illegal candidate outright.
        base_weights.pop("pool", None)
    else:
        pool = game.pools[eligible_pool_id]
        if pool.visibility is PoolVisibility.PRIVATE:
            pool.visibility = PoolVisibility.PUBLIC
            events.append(
                _emit(game, now, EventType.ARBITRATION_POOL_REVEALED, actor=actor_game_player_id, payload={"pool_id": eligible_pool_id})
            )

    proposal.pending_arbitration = PendingArbitration(
        arbitration_id=new_id(),
        called_by=actor_game_player_id,
        called_at=now,
        resolves_at=now + timedelta(seconds=game.config.arbitration_window_seconds),
        eligible_pool_id=eligible_pool_id,
        base_weights=base_weights,
    )
    events.append(
        _emit(
            game,
            now,
            EventType.ARBITRATION_CALLED,
            actor=actor_game_player_id,
            payload={
                "proposal_id": proposal.proposal_id,
                "caller_role": caller_role,
                "resolves_at": proposal.pending_arbitration.resolves_at.isoformat(),
            },
        )
    )
    return events


def _handle_cast_arbitration_vote(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    """Jury-only: exactly the players who have already passed this
    negotiation, per the locked rules -- the active pair settles by
    Accept, never by voting. Secret live (EVENT_VISIBILITY.ACTOR_ONLY;
    only "who has voted," never the content, is ever projected to anyone
    else -- see projections._project_proposal)."""
    _require_negotiation(game)
    if game.active_proposal_id is None:
        raise IllegalCommandError("no active negotiation")
    proposal = game.proposals[game.active_proposal_id]
    pending = proposal.pending_arbitration
    if pending is None:
        raise IllegalCommandError("no arbitration in progress")
    _require_no_pending_boost_draw(game, actor_game_player_id)
    if actor_game_player_id not in proposal.passed_player_ids:
        raise IllegalCommandError("only players who have passed this negotiation may serve on its jury")
    if actor_game_player_id in pending.votes:
        raise IllegalCommandError("you already voted")

    vote = payload["vote"]
    if vote not in pending.base_weights:
        # Covers both a genuinely illegal choice string and "pool" when no
        # Pool survived to eligibility -- a juror is never offered a vote
        # for an outcome that isn't actually on the table.
        raise IllegalCommandError("not a legal vote this cycle")

    pending.votes[actor_game_player_id] = vote
    return [
        _emit(
            game, now, EventType.ARBITRATION_VOTE_CAST, actor=actor_game_player_id, payload={"proposal_id": proposal.proposal_id, "vote": vote}
        )
    ]


def _handle_use_boost(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    """Single command, `boost_type`-discriminated (concentrate / force_swap
    / draw) -- matches the existing generic-envelope style (cf.
    CREATE_POOL's own `visibility` field). Concentrate and Force Swap
    resolve immediately; draw only *starts* a decision (see
    RESOLVE_BOOST_DRAW/DECLINE_BOOST_DRAW below) but the Boost itself is
    spent right here regardless of how that decision eventually goes."""
    _require_negotiation(game)
    _require_no_pending_boost_draw(game, actor_game_player_id)
    _require_boosts_active(game)
    player = game.player_by_id(actor_game_player_id)
    if player.boosts_remaining < 1:
        raise IllegalCommandError("no Boosts remaining")

    boost_type = payload["boost_type"]
    if boost_type == "concentrate":
        return _use_boost_concentrate(game, player, payload, now)
    if boost_type == "force_swap":
        return _use_boost_force_swap(game, player, payload, now)
    if boost_type == "draw":
        return _use_boost_draw(game, player, now)
    raise IllegalCommandError(f"unknown boost_type {boost_type!r}")


def _use_boost_concentrate(game: Game, player: GamePlayer, payload: dict, now: datetime) -> list[GameEvent]:
    """Deliberately increase an existing holding, up to the x3 cap
    (GameConfig.concentrate_max_copies): discard one holding, duplicate an
    entity the player already owns at least one copy of. Discarding and
    duplicating the SAME entity is legal and nets to no change in copy
    count -- not a cap violation just because it names the same entity
    twice."""
    discard_id = payload["holding_id_to_discard"]
    duplicate_entity_id = payload["entity_id_to_duplicate"]

    discard_holding = game.holdings.get(discard_id)
    if (
        discard_holding is None
        or discard_holding.owner_player_id != player.game_player_id
        or discard_holding.zone != HoldingZone.PORTFOLIO
    ):
        raise IllegalCommandError("not a holding you own")
    if not _owns(game, player.game_player_id, duplicate_entity_id):
        raise IllegalCommandError("you don't own that entity")

    current_count = sum(
        1
        for h in game.holdings.values()
        if h.owner_player_id == player.game_player_id and h.zone == HoldingZone.PORTFOLIO and h.entity_id == duplicate_entity_id
    )
    resulting_count = current_count - (1 if discard_holding.entity_id == duplicate_entity_id else 0) + 1
    if resulting_count > game.config.concentrate_max_copies:
        raise IllegalCommandError(f"would exceed the x{game.config.concentrate_max_copies} concentration cap")

    player.boosts_remaining -= 1
    discard_holding.zone = HoldingZone.DISCARDED
    new_holding = Holding(
        holding_id=new_id(), entity_id=duplicate_entity_id, owner_player_id=player.game_player_id, zone=HoldingZone.PORTFOLIO
    )
    game.holdings[new_holding.holding_id] = new_holding
    return [
        _emit(
            game,
            now,
            EventType.BOOST_CONCENTRATE_USED,
            actor=player.game_player_id,
            payload={"discarded_holding_id": discard_id, "duplicated_entity_id": duplicate_entity_id},
        )
    ]


def _use_boost_force_swap(game: Game, player: GamePlayer, payload: dict, now: datetime) -> list[GameEvent]:
    """Alter the market unilaterally -- Boost-gated, otherwise identical to
    a negotiated swap: reuses _execute_swap directly, so it correctly
    triggers crossing-invalidation against every other open negotiation.
    Deliberately never consults _is_protected_reversal -- Force Swap can
    always target any pair, including the currently-protected one (a
    Boost undoing a Boost is fair, same-cost play).

    Sets game.protected_pair to this pair, unconditionally, *after*
    _execute_swap runs -- _execute_swap's own generic clearing (see its
    docstring) may have already cleared whatever was protected before,
    including this exact pair if it's the one being re-swapped; either
    way, this call is what actually determines the new lock, overwriting
    any earlier value regardless of what _execute_swap just did to it."""
    entity_a, entity_b = payload["entity_a"], payload["entity_b"]
    if entity_a == entity_b:
        raise IllegalCommandError("must name two different entities")
    _require_entities_exist(game, entity_a, entity_b)

    player.boosts_remaining -= 1
    swap = SwapIntent(
        entity_a=entity_a,
        entity_b=entity_b,
        initiator_player_id=player.game_player_id,
        rising_entity_id=_rising_entity(game, entity_a, entity_b),
    )
    events = _execute_swap(game, swap, now)
    game.protected_pair = ProtectedPair(entity_a=entity_a, entity_b=entity_b)
    events.append(
        _emit(game, now, EventType.BOOST_FORCE_SWAP_USED, actor=player.game_player_id, payload={"entity_a": entity_a, "entity_b": entity_b})
    )
    return events


def _use_boost_draw(game: Game, player: GamePlayer, now: datetime) -> list[GameEvent]:
    """Introduce a genuinely new entity into the portfolio. Eligibility --
    every current market entity this player owns zero copies of -- is
    computed once, right here, and never recomputed once the decision is
    pending: see PendingBoostDraw's own docstring for why (discarding a
    holding during the decision must never retroactively make some other
    entity "the draw"). The Boost is spent the instant this succeeds,
    whether the player ultimately keeps the draw or not."""
    eligible = [entity_id for entity_id in game.market if not _owns(game, player.game_player_id, entity_id)]
    if not eligible:
        raise IllegalCommandError("no eligible entities to draw")

    player.boosts_remaining -= 1
    drawn_entity_id = random.Random().choice(eligible)
    original_holding_ids = [
        h.holding_id for h in game.holdings.values() if h.owner_player_id == player.game_player_id and h.zone == HoldingZone.PORTFOLIO
    ]
    deadline = now + timedelta(seconds=game.config.boost_draw_decision_seconds)
    pending = PendingBoostDraw(
        pending_boost_draw_id=new_id(),
        revealed_entity_id=drawn_entity_id,
        original_portfolio_holding_ids=original_holding_ids,
        started_at=now,
        decision_deadline_at=deadline,
    )
    # Frozen-view pattern (same as the old Pick Up flow): render this
    # player's own view BEFORE attaching the pending decision, then patch
    # in the reveal -- every read for the rest of the window serves this
    # exact snapshot back, rather than a live board moving on underneath
    # an undecided choice. Import kept local to dodge the projections <->
    # engine import cycle (projections already imports from entities/
    # events, not engine).
    from gotiate.domain.projections import PlayerAudience, project

    cached_view = project(game, PlayerAudience(player.game_player_id))
    cached_view["pending_boost_draw"] = {
        "pending_boost_draw_id": pending.pending_boost_draw_id,
        "revealed_entity_id": drawn_entity_id,
        "decision_deadline_at": deadline,
    }
    pending.cached_view = cached_view
    player.pending_boost_draw = pending

    return [
        _emit(
            game,
            now,
            EventType.BOOST_DRAW_STARTED,
            actor=player.game_player_id,
            payload={
                "pending_boost_draw_id": pending.pending_boost_draw_id,
                "revealed_entity_id": drawn_entity_id,
                "decision_deadline_at": deadline.isoformat(),
            },
        )
    ]


def _handle_resolve_boost_draw(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    player = game.player_by_id(actor_game_player_id)
    pending = player.pending_boost_draw
    if pending is None or pending.pending_boost_draw_id != payload["pending_boost_draw_id"]:
        raise IllegalCommandError("no matching pending boost draw")

    discard_id = payload["holding_id_to_discard"]
    if discard_id not in pending.original_portfolio_holding_ids:
        raise IllegalCommandError("must discard one of your original holdings")

    game.holdings[discard_id].zone = HoldingZone.DISCARDED
    new_holding = Holding(
        holding_id=new_id(), entity_id=pending.revealed_entity_id, owner_player_id=player.game_player_id, zone=HoldingZone.PORTFOLIO
    )
    game.holdings[new_holding.holding_id] = new_holding
    player.pending_boost_draw = None

    return [
        _emit(
            game,
            now,
            EventType.BOOST_DRAW_COMPLETED,
            actor=player.game_player_id,
            payload={
                "pending_boost_draw_id": pending.pending_boost_draw_id,
                "discarded_holding_id": discard_id,
                "revealed_entity_id": pending.revealed_entity_id,
            },
        )
    ]


def _handle_decline_boost_draw(game: Game, *, payload: dict, actor_game_player_id: str | None, now: datetime) -> list[GameEvent]:
    _require_negotiation(game)
    player = game.player_by_id(actor_game_player_id)
    pending = player.pending_boost_draw
    if pending is None or pending.pending_boost_draw_id != payload["pending_boost_draw_id"]:
        raise IllegalCommandError("no matching pending boost draw")
    return _fail_pending_boost_draw(game, player, BoostDrawFailureReason.DECLINED_BY_PLAYER, now)


def _fail_pending_boost_draw(game: Game, player: GamePlayer, reason: BoostDrawFailureReason, now: datetime) -> list[GameEvent]:
    pending = player.pending_boost_draw
    assert pending is not None
    player.pending_boost_draw = None
    return [
        _emit(
            game,
            now,
            EventType.BOOST_DRAW_FAILED,
            actor=player.game_player_id,
            payload={"pending_boost_draw_id": pending.pending_boost_draw_id, "reason": reason.value},
        )
    ]


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
    "KICK_PLAYER": _handle_kick_player,
    "START_GAME": _handle_start_game,
    "PROPOSE_SWAP": _handle_propose_swap,
    "PASS_PROPOSAL": _handle_pass_proposal,
    "ACCEPT_PROPOSAL": _handle_accept_proposal,
    "CREATE_POOL": _handle_create_pool,
    "WITHDRAW_POOL": _handle_withdraw_pool,
    "MAKE_POOL_PUBLIC": _handle_make_pool_public,
    "PASS_POOL": _handle_pass_pool,
    # Compatibility for older clients; the authoritative behavior is now
    # Pool Pass, including public Pools and public pass tracking.
    "DECLINE_POOL": _handle_pass_pool,
    "ACCEPT_POOL": _handle_accept_pool,
    "CALL_ARBITRATION": _handle_call_arbitration,
    "CAST_ARBITRATION_VOTE": _handle_cast_arbitration_vote,
    "USE_BOOST": _handle_use_boost,
    "RESOLVE_BOOST_DRAW": _handle_resolve_boost_draw,
    "DECLINE_BOOST_DRAW": _handle_decline_boost_draw,
    "SET_READY_TO_CLOSE": _handle_set_ready_to_close,
}


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _emit(game: Game, now: datetime, type_: EventType, *, actor: str | None, payload: dict) -> GameEvent:
    event = GameEvent(game_id=game.id, seq_no=game.next_seq_no, type=type_, actor_game_player_id=actor, payload=payload, created_at=now)
    game.next_seq_no += 1
    return event


def _resolve_proposal(
    game: Game, proposal: Proposal, reason: ProposalResolutionReason, resolved_by: str | None, now: datetime
) -> list[GameEvent]:
    """The single chokepoint every proposal resolution funnels through,
    regardless of reason -- this is where active_proposal_id is cleared
    (never set or cleared anywhere else) and, if Arbitration was still
    pending on this exact proposal, where it gets swept up too.

    A pending Arbitration only ever reaches this function still SET in
    two cases: a normal ACCEPT_PROPOSAL/ACCEPT_POOL settling it during the
    20s window (reason == EXECUTED -> ArbitrationResolutionReason
    .SETTLED_NORMALLY), or close_market's own generic MARKET_CLOSED loop
    preempting it before it could resolve on its own (any other reason ->
    ArbitrationResolutionReason.MARKET_CLOSED). The machine-draw path
    (_resolve_arbitration_via_machine) always clears pending_arbitration
    itself, *before* calling this, specifically so this auto-detection
    never double-fires for that path -- it emits its own, fuller
    ARBITRATION_RESOLVED first."""
    proposal.status = ResolutionStatus.RESOLVED
    proposal.resolved_at_seq_no = game.next_seq_no
    proposal.resolved_by_player_id = resolved_by
    proposal.resolution_reason = reason

    events: list[GameEvent] = []
    if proposal.pending_arbitration is not None:
        pending = proposal.pending_arbitration
        proposal.pending_arbitration = None
        arb_reason = (
            ArbitrationResolutionReason.SETTLED_NORMALLY
            if reason is ProposalResolutionReason.EXECUTED
            else ArbitrationResolutionReason.MARKET_CLOSED
        )
        events.append(
            _emit(
                game,
                now,
                EventType.ARBITRATION_RESOLVED,
                actor=resolved_by,
                payload={
                    "proposal_id": proposal.proposal_id,
                    "reason": arb_reason.value,
                    "base_weights": pending.base_weights,
                    "final_weights": None,  # no draw happened -- nothing to normalize
                    "votes": dict(pending.votes),
                },
            )
        )

    # The single place active_proposal_id ever gets cleared -- every
    # proposal resolution, of any reason, funnels through here. Never set
    # or cleared anywhere else in the engine.
    if game.active_proposal_id == proposal.proposal_id:
        game.active_proposal_id = None

    events.append(
        _emit(game, now, EventType.PROPOSAL_RESOLVED, actor=resolved_by, payload={"proposal_id": proposal.proposal_id, "reason": reason.value})
    )
    return events


def _resolve_pool(game: Game, pool: Pool, reason: PoolResolutionReason, resolved_by: str | None, now: datetime) -> GameEvent:
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


def _final_arbitration_weights(config: GameConfig, pending: PendingArbitration) -> dict[str, int]:
    """Each juror's secret vote is additive and cumulative, independent of
    every other juror's: +bonus to the voted choice, -penalty to each of
    the OTHER legal choices, floored at zero as it goes. Normalized only
    once, at draw time (by the caller) -- never here, so intermediate
    weights stay in the same "deliberately weights, not percentages"
    space the base weights are already in. Worked example matching the
    locked design exactly: base_weights 30/40/40 (base/pool/neither), one
    vote for "base" -> 40/35/35; a second vote for "pool" on top of that
    -> 35/45/30."""
    weights = dict(pending.base_weights)
    for vote in pending.votes.values():
        if vote not in weights:
            continue  # defensive only -- CAST_ARBITRATION_VOTE never lets an illegal choice in
        weights[vote] += config.arbitration_vote_bonus
        for choice in weights:
            if choice != vote:
                weights[choice] = max(0, weights[choice] - config.arbitration_vote_penalty)
    return weights


def _resolve_arbitration_via_machine(game: Game, proposal: Proposal, now: datetime, rng: random.Random) -> list[GameEvent]:
    """Fires once the 20-second window elapses with neither Accept having
    settled it normally (see apply_due_time_transitions). Influential,
    never determinative -- the jury can lean on the machine, never become
    it: normalization happens exactly once, right here, at the moment of
    the actual draw."""
    pending = proposal.pending_arbitration
    assert pending is not None
    weights = _final_arbitration_weights(game.config, pending)

    choices = list(weights.keys())
    outcome = rng.choices(choices, weights=[weights[c] for c in choices], k=1)[0]

    # Cleared BEFORE resolving -- so _resolve_proposal's own
    # pending_arbitration auto-detection (SETTLED_NORMALLY / MARKET_CLOSED)
    # never double-fires for this, already-machine-resolved, path.
    proposal.pending_arbitration = None

    events: list[GameEvent] = []
    if outcome == ArbitrationChoice.BASE:
        events += _execute_swap(game, proposal.swap, now, exclude_proposal_id=proposal.proposal_id)
        events += _resolve_proposal(game, proposal, ProposalResolutionReason.EXECUTED, None, now)
        arb_reason = ArbitrationResolutionReason.MACHINE_BASE
    elif outcome == ArbitrationChoice.POOL:
        assert pending.eligible_pool_id is not None
        pool = game.pools[pending.eligible_pool_id]
        events += _execute_swap(game, proposal.swap, now, exclude_proposal_id=proposal.proposal_id, exclude_pool_id=pool.pool_id)
        events += _execute_swap(game, pool.swap, now, exclude_proposal_id=proposal.proposal_id, exclude_pool_id=pool.pool_id)
        events += _resolve_proposal(game, proposal, ProposalResolutionReason.EXECUTED, None, now)
        events.append(_resolve_pool(game, pool, PoolResolutionReason.EXECUTED, None, now))
        arb_reason = ArbitrationResolutionReason.MACHINE_POOL
    else:
        events += _resolve_proposal(game, proposal, ProposalResolutionReason.ARBITRATION_NEITHER, None, now)
        arb_reason = ArbitrationResolutionReason.MACHINE_NEITHER

    # Nobody "chose" a machine outcome -- resolving_actor=None means any
    # surviving sibling (the eligible Pool, if outcome wasn't "pool")
    # falls through to PREEMPTED_BY_OTHER_ACTION, never
    # INVALIDATED_BY_INITIATOR_ACTION.
    events += resolve_sibling_pools(game, proposal, None, now)

    events.append(
        _emit(
            game,
            now,
            EventType.ARBITRATION_RESOLVED,
            actor=None,
            payload={
                "proposal_id": proposal.proposal_id,
                "reason": arb_reason.value,
                "base_weights": pending.base_weights,
                "final_weights": weights,
                "votes": dict(pending.votes),
            },
        )
    )
    return events


def _execute_swap(
    game: Game,
    swap: SwapIntent,
    now: datetime,
    *,
    exclude_proposal_id: str | None = None,
    exclude_pool_id: str | None = None,
) -> list[GameEvent]:
    """The sole choke point where two entities' positions ever change --
    every negotiated or unilateral swap funnels through here. After moving
    the market, scans every OTHER open negotiation referencing either moved
    entity for a crossed direction and voids it. exclude_proposal_id/
    exclude_pool_id skip the negotiation this very swap belongs to (if
    any) -- right after its own swap, a fresh direction check would always
    look "crossed" (the entity that was rising just took the better
    position), which is the negotiation succeeding as promised, not a
    violation.

    Also the single place `game.protected_pair` ever clears (Force Swap's
    own lock, see ProtectedPair): if either entity this swap actually
    moves is part of the currently-protected pair, the lock lifts --
    whatever "reverse" it was guarding against wouldn't even restore the
    original state anymore once one of the two has moved again through a
    negotiated deal. Checked generically here, not per-caller, so Accept
    /Pool/the Arbitration machine draw's own execution all clear it for
    free; Force Swap's own call site unconditionally re-sets
    protected_pair to its own pair immediately afterward regardless of
    what happens here."""
    a = _market_entity(game, swap.entity_a)
    b = _market_entity(game, swap.entity_b)
    a.position, b.position = b.position, a.position
    if game.protected_pair is not None and {swap.entity_a, swap.entity_b} & {game.protected_pair.entity_a, game.protected_pair.entity_b}:
        game.protected_pair = None
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
            events += _resolve_proposal(game, proposal, ProposalResolutionReason.VOIDED_MARKET_SWUNG, None, now)
            for pool in list(game.pools.values()):
                if pool.base_proposal_id == proposal.proposal_id and pool.status == ResolutionStatus.OPEN:
                    events.append(_resolve_pool(game, pool, PoolResolutionReason.BASE_PROPOSAL_VOIDED, None, now))

    for pool in list(game.pools.values()):
        if pool.status != ResolutionStatus.OPEN or pool.pool_id == exclude_pool_id:
            continue
        if {pool.swap.entity_a, pool.swap.entity_b} & moved and _direction_crossed(game, pool.swap):
            events.append(_resolve_pool(game, pool, PoolResolutionReason.VOIDED_MARKET_SWUNG, None, now))

    return events


def _direction_crossed(game: Game, swap: SwapIntent) -> bool:
    return _rising_entity(game, swap.entity_a, swap.entity_b) != swap.rising_entity_id


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
