"""Domain entities — the conceptual shapes from the Gotiate domain model doc, §02/§03.

Pure data. No I/O, no FastAPI, no persistence. `Game` is the authoritative
aggregate: one instance per match, mutated only by `domain.engine`.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class GamePhase(StrEnum):
    LOBBY = "LOBBY"
    NEGOTIATION = "NEGOTIATION"
    CLOSING = "CLOSING"
    SCORED = "SCORED"
    # Host-initiated, any phase before SCORED (CANCEL_GAME) -- terminal,
    # like SCORED, but deliberately distinct from it: no realized Haircut
    # depth, no winner, nothing to replay. Exists specifically so
    # find_active_game_hosted_by's one-active-game-per-host rule has an
    # escape hatch other than waiting out real gameplay to completion.
    CANCELLED = "CANCELLED"


class HoldingZone(StrEnum):
    RESERVE_UNREVEALED = "reserve_unrevealed"
    PICKUP_PENDING = "pickup_pending"
    PORTFOLIO = "portfolio"
    DISCARDED = "discarded"
    PICKUP_SURRENDERED = "pickup_surrendered"  # revealed via Pick Up, then failed
    SURRENDERED_UNUSED = "surrendered_unused"  # never touched, forced at close
    BURNED_UNSEEN = "burned_unseen"  # unilateral swap, never revealed to owner


class ResolutionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ProposalResolutionReason(StrEnum):
    EXECUTED = "executed"
    WITHDRAWN_BY_INITIATOR = "withdrawn_by_initiator"
    MARKET_CLOSED = "market_closed"
    # Every other seated player has PASS_PROPOSAL'd -- mathematically dead,
    # nobody rejected it. Never shown live as this value: projections.py's
    # _public_resolution_reason masks it back to WITHDRAWN_BY_INITIATOR for
    # every live audience, so Pass never becomes a public signal. See the
    # Pass design writeup.
    EXPIRED_ALL_PASSED = "expired_all_passed"
    # The market moved under this proposal's locked direction (see
    # SwapIntent.rising_entity_id) -- the opposite of EXPIRED_ALL_PASSED:
    # this is deliberately LOUD, never masked, for every live audience.
    # See the market-direction-reversal design writeup.
    VOIDED_MARKET_SWUNG = "voided_market_swung"


class PoolResolutionReason(StrEnum):
    EXECUTED = "executed"
    WITHDRAWN_BY_INITIATOR = "withdrawn_by_initiator"
    INVALIDATED_BY_INITIATOR_ACTION = "invalidated_by_initiator_action"
    DECLINED_BY_TARGET = "declined_by_target"
    PREEMPTED_BY_OTHER_ACTION = "preempted_by_other_action"
    BASE_PROPOSAL_WITHDRAWN = "base_proposal_withdrawn"
    MARKET_CLOSED = "market_closed"
    # This pool's own leg crossed -- mirrors ProposalResolutionReason's
    # value above, never masked. Distinct from BASE_PROPOSAL_VOIDED below:
    # this pool's own direction, not its base proposal's, is what broke.
    VOIDED_MARKET_SWUNG = "voided_market_swung"
    # Cascaded because this pool's base proposal voided -- mirrors
    # BASE_PROPOSAL_WITHDRAWN's existing cascade shape exactly, new reason.
    BASE_PROPOSAL_VOIDED = "base_proposal_voided"


class PoolVisibility(StrEnum):
    PRIVATE = "private"
    PUBLIC = "public"


class PickupFailureReason(StrEnum):
    DECISION_TIMEOUT = "decision_timeout"
    MARKET_CLOSED = "market_closed"
    # Player-triggered "Skip" -- same _fail_pending_pickup machinery as a
    # timeout, distinct reason so replay/analytics can tell "chose to
    # skip" from "ran out of time". See DECLINE_PICKUP.
    DECLINED_BY_PLAYER = "declined_by_player"


class CloseReason(StrEnum):
    TIME_EXPIRED = "TIME_EXPIRED"
    READY_THRESHOLD = "READY_THRESHOLD"
    # No OPTIONALITY_EXHAUSTED for V1 — deliberately deferred, see decision log §10.


class CancellationReason(StrEnum):
    HOST_INITIATED = "HOST_INITIATED"
    # The host let the lobby-reminder grace window lapse without starting
    # or asking for more time — see GameConfig.lobby_reminder_seconds.
    LOBBY_TIMEOUT = "LOBBY_TIMEOUT"


class CommandStatus(StrEnum):
    APPLIED = "applied"
    REJECTED_STALE_VERSION = "rejected_stale_version"
    REJECTED_ILLEGAL = "rejected_illegal"
    # No REJECTED_DUPLICATE_REPLAY — idempotent replay returns the *original*
    # receipt's status verbatim (see routes.submit_command), it never creates
    # a new receipt describing itself as a duplicate. That's the whole point
    # of idempotency: the second request doesn't have its own outcome.


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class SetupQualityConfig(BaseModel):
    """Tunable thresholds/weights for one player count's starting-state
    generation (domain.setup) -- every number here is data, not buried
    engine logic, specifically so retuning after a playtest is a config
    change, not an engine rewrite. See the initial-distribution-quality
    design writeup for the empirical survival-rate measurements behind
    these defaults."""

    # Hard rejects.
    max_pair_overlap: int
    reject_isolated_player: bool = True
    elite_concentration_limit: int = 3

    # Zone definitions for geometry scoring, as a fraction of market_size.
    elite_zone_fraction: float = 1 / 3
    basement_zone_fraction: float = 1 / 3

    # Soft-score weights. elite > basement is the deliberate asymmetry --
    # a top-heavy opening suppresses negotiation, a bottom-heavy one
    # creates urgency, they are not scored as equivalent.
    elite_penalty_weight: float = 2.0
    basement_penalty_weight: float = 1.0
    spread_penalty_weight: float = 0.5
    heaviest_pair_share_weight: float = 1.0
    connectivity_weight: float = 1.0

    # Candidate-generation volume/selection.
    topology_candidates: int = 500
    topology_shortlist_size: int = 75
    geometry_candidates_per_topology: int = 20
    selection_band_fraction: float = 0.15
    # A hard constraint that can't be satisfied within this many raw
    # draws fails the command explicitly -- see
    # errors.UnableToGenerateStartingStateError. Never silently relaxed.
    max_generation_attempts: int = 20000


class HaircutProfile(BaseModel):
    """A public-once-revealed probability distribution over `haircut_depth`
    (how many of the top market positions get wiped at scoring) -- see the
    Haircut-risk design writeup. `depth_probabilities[d]` is P(realized
    depth == d) for d = 0..K; index 0 means "nothing wiped." Certainty for
    market position p (1-indexed) is sum(depth_probabilities[0:p]) -- a
    position survives iff the realized depth is strictly less than it."""

    depth_probabilities: list[float]

    @field_validator("depth_probabilities")
    @classmethod
    def _validate_distribution(cls, v: list[float]) -> list[float]:
        if not v:
            raise ValueError("depth_probabilities must not be empty")
        if any(p < 0 for p in v):
            raise ValueError("depth_probabilities must be non-negative")
        if abs(sum(v) - 1.0) > 1e-6:
            raise ValueError(f"depth_probabilities must sum to 1.0, got {sum(v)}")
        return v

    @property
    def max_depth(self) -> int:
        """Highest depth with nonzero probability -- the boundary beyond
        which a market position is structurally safe (see safe_value)."""
        return max(i for i, p in enumerate(self.depth_probabilities) if p > 0)


class GameConfig(BaseModel):
    """Frozen onto Game at START_GAME. Later tuning changes never retroactively
    affect a live game — see domain model §09."""

    market_size_by_players: dict[int, int] = Field(
        default_factory=lambda: {2: 11, 3: 11, 4: 13, 5: 15, 6: 17}
    )
    # n=3 is deliberately stricter (max_pair_overlap=1, isolation is a
    # hard reject) than the rest -- a pair sharing 2 of 4 interests forms
    # a structurally dominant bloc against a lone third player in a way
    # it doesn't once there are 3+ other players to route around. n=2
    # has no isolation hard-reject (a disjoint pair still has a real
    # negotiation lever -- proposals aren't restricted to entities you
    # own) and a looser overlap cap (no bloc/third-wheel risk with only
    # one pair). See the initial-distribution-quality design writeup.
    setup_quality_by_players: dict[int, SetupQualityConfig] = Field(
        default_factory=lambda: {
            2: SetupQualityConfig(max_pair_overlap=2, reject_isolated_player=False),
            3: SetupQualityConfig(max_pair_overlap=1, reject_isolated_player=True),
            4: SetupQualityConfig(max_pair_overlap=2, reject_isolated_player=True),
            5: SetupQualityConfig(max_pair_overlap=2, reject_isolated_player=True),
            6: SetupQualityConfig(max_pair_overlap=2, reject_isolated_player=True),
        }
    )
    max_clock_seconds_by_players: dict[int, int] = Field(
        default_factory=lambda: {2: 480, 3: 720, 4: 960, 5: 1200, 6: 1440}
    )
    # Retires waterline_baseline_v1 entirely -- see the Haircut-risk design
    # writeup. Several monotonic profiles per player count (not one
    # universal curve) so players can't learn "position N is the real
    # optimum"; every profile's max_depth must equal
    # round(market_size_by_players[n] * risk_depth_fraction) for that same
    # n, enforced by the model_validator below so config and profile data
    # can never silently drift apart.
    #
    # Five named shapes per player count, spanning TWO independent axes,
    # not just "how scary is #1" -- severity at the top AND how deep the
    # danger zone cascades. Cliff/Deep burn/Brutal plateau are all "#1 is
    # dangerous" but disagree sharply on whether #2/#3 recover quickly or
    # stay dangerous too; Moderate/Mild are calmer at the top with their
    # own distinct decay shapes. Deliberately NOT tuned so #2/#3 are
    # reliably safe -- a deep, cascading danger zone is intended: it's
    # what stops "push everything up" from being a context-free good
    # move ("you can have #2, I don't want it"). See the playtest-driven
    # Haircut-range design writeup for the worked cumulative-certainty
    # curves these were derived from (Cliff/Deep burn/Brutal
    # plateau/Moderate/Mild, by position: roughly 6/32/61/79/90,
    # 6/15/31/54/73, 9/18/29/43/62, 25/43/61/76/87, 55/69/80/88/94 --
    # truncated or extended per player count's own max_depth).
    haircut_profiles_by_players: dict[int, list[HaircutProfile]] = Field(
        default_factory=lambda: {
            2: [  # identical to 3 -- both round to max_depth=4 now that n=2's
                # market_size is 11, same existing precedent as 4/5 sharing
                # profiles. See the Market-Correction market-size writeup.
                HaircutProfile(depth_probabilities=[0.06, 0.26, 0.29, 0.18, 0.21]),
                HaircutProfile(depth_probabilities=[0.06, 0.09, 0.16, 0.23, 0.46]),
                HaircutProfile(depth_probabilities=[0.09, 0.09, 0.11, 0.14, 0.57]),
                HaircutProfile(depth_probabilities=[0.25, 0.18, 0.18, 0.15, 0.24]),
                HaircutProfile(depth_probabilities=[0.55, 0.14, 0.11, 0.08, 0.12]),
            ],
            3: [
                HaircutProfile(depth_probabilities=[0.06, 0.26, 0.29, 0.18, 0.21]),
                HaircutProfile(depth_probabilities=[0.06, 0.09, 0.16, 0.23, 0.46]),
                HaircutProfile(depth_probabilities=[0.09, 0.09, 0.11, 0.14, 0.57]),
                HaircutProfile(depth_probabilities=[0.25, 0.18, 0.18, 0.15, 0.24]),
                HaircutProfile(depth_probabilities=[0.55, 0.14, 0.11, 0.08, 0.12]),
            ],
            4: [
                HaircutProfile(depth_probabilities=[0.06, 0.26, 0.29, 0.18, 0.11, 0.10]),
                HaircutProfile(depth_probabilities=[0.06, 0.09, 0.16, 0.23, 0.19, 0.27]),
                HaircutProfile(depth_probabilities=[0.09, 0.09, 0.11, 0.14, 0.19, 0.38]),
                HaircutProfile(depth_probabilities=[0.25, 0.18, 0.18, 0.15, 0.11, 0.13]),
                HaircutProfile(depth_probabilities=[0.55, 0.14, 0.11, 0.08, 0.06, 0.06]),
            ],
            5: [  # identical to 4 -- both round to max_depth=5, same existing precedent
                HaircutProfile(depth_probabilities=[0.06, 0.26, 0.29, 0.18, 0.11, 0.10]),
                HaircutProfile(depth_probabilities=[0.06, 0.09, 0.16, 0.23, 0.19, 0.27]),
                HaircutProfile(depth_probabilities=[0.09, 0.09, 0.11, 0.14, 0.19, 0.38]),
                HaircutProfile(depth_probabilities=[0.25, 0.18, 0.18, 0.15, 0.11, 0.13]),
                HaircutProfile(depth_probabilities=[0.55, 0.14, 0.11, 0.08, 0.06, 0.06]),
            ],
            6: [
                HaircutProfile(depth_probabilities=[0.06, 0.26, 0.29, 0.18, 0.11, 0.06, 0.04]),
                HaircutProfile(depth_probabilities=[0.06, 0.09, 0.16, 0.23, 0.19, 0.14, 0.13]),
                HaircutProfile(depth_probabilities=[0.09, 0.09, 0.11, 0.14, 0.19, 0.16, 0.22]),
                HaircutProfile(depth_probabilities=[0.25, 0.18, 0.18, 0.15, 0.11, 0.07, 0.06]),
                HaircutProfile(depth_probabilities=[0.55, 0.14, 0.11, 0.08, 0.06, 0.03, 0.03]),
            ],
        }
    )
    # The profile is chosen and locked at START_GAME but stays hidden until
    # this fraction of the negotiation clock has elapsed -- see
    # engine._handle_start_game / apply_due_time_transitions.
    haircut_reveal_fraction: float = 0.5
    # Target risky depth = round(market_size * risk_depth_fraction) -- see
    # the worked table in the design writeup (11->4, 13->5, 15->5, 17->6).
    risk_depth_fraction: float = 0.35
    starting_influence: int = 10
    unilateral_cutoff_fraction: float = 0.10
    portfolio_shape: list[int] = Field(default_factory=lambda: [2, 1, 1, 1])
    reserve_count: int = 2
    # 5s was too short to read and decide, especially on a phone -- real
    # playtest feedback. See the Stage 5 Reserve UX overhaul design writeup.
    pickup_decision_seconds: float = 12.0
    pickup_transport_grace_ms: int = 500
    # Blocks ACCEPT_PROPOSAL (and ACCEPT_POOL on a public pool, gated to
    # the base proposal's own lock) for this long after PROPOSAL_CREATED --
    # gives the room a moment to actually read a new proposal before
    # someone already at the keyboard snap-accepts it. Nothing else is
    # blocked -- withdraw/pass/pool/private-pool-accept all still work
    # immediately, see engine._handle_accept_proposal / _handle_accept_pool.
    accept_lock_seconds: float = 4.0
    # Applied once, flat, the instant every seated player's
    # influence_available hits 0 at the same time -- see
    # engine._maybe_topup_zero_influence. Keeps the negotiation from
    # stalling out entirely once Influence is fully spent everywhere.
    zero_influence_topup_amount: int = 2
    allow_public_pools: bool = True
    allow_private_pools: bool = True
    ready_to_close_revealed_in_replay: bool = False
    # Undecided on purpose (see private Influence economy discussion) --
    # defaults to still-private in replay, trivially flippable later
    # without a schema change.
    influence_revealed_in_replay: bool = False

    # 2-player-only anti-stagnation mechanic -- flat, not player-count
    # -keyed, since this never applies to any other count; a per-count
    # dict would be premature. Grounded in the real 2p clock/market
    # (480s, 11-slot -- widened from 9 specifically to give the
    # correction's mutually-unowned-destination search more room; see
    # the Market Correction market-size writeup) -- see the Market
    # Correction design writeup. 90s ~= 18.75% of the 480s 2p clock --
    # long enough that active negotiation never trips it, short enough
    # to matter within an 8-minute game.
    market_correction_stagnation_seconds: float = 90.0
    # 15s, not the originally-drafted 45s -- "that turns the grenade
    # into patio furniture."
    market_correction_offer_seconds: float = 15.0
    # Prevents a permanently-stalled game from re-offering the instant
    # the last one lapses -- without this, "time since last negotiated
    # deal" alone would regenerate a correction continuously once
    # crossed once.
    market_correction_cooldown_seconds: float = 90.0
    # Severity formula (see engine._market_correction_target_displacements):
    # spread = max_spread * min(1, gap / gap_saturation); leader gets
    # base - spread/2, trailer gets base + spread/2, both clamped to
    # [1, market_size - 1].
    market_correction_base_displacement_fraction: float = 0.5
    market_correction_max_spread_fraction: float = 0.4
    market_correction_gap_saturation_fraction: float = 1.0

    join_code_length: int = 7
    # Bounds the brute-force window on a live join code independent of how
    # long the lobby happens to sit open. 30 is a placeholder default, not a
    # locked number — flagged for confirmation, easy to tighten (e.g. to 5)
    # or loosen with a one-line config change.
    join_code_lifetime_minutes: int = 30

    # The only way LOBBY -> NEGOTIATION happens is the host hitting Start --
    # no auto-start, no headcount target. This pair exists purely so an
    # abandoned lobby doesn't sit open forever: once lobby_reminder_seconds
    # elapses, the host sees a "start now or ask for more time" prompt
    # (EXTEND_LOBBY_TIMER pushes the deadline out by this same amount
    # again, uncapped); if lobby_reminder_grace_seconds passes after that
    # with no response, the game auto-cancels (CancellationReason.LOBBY_TIMEOUT).
    lobby_reminder_seconds: int = 180
    lobby_reminder_grace_seconds: int = 60

    # The only way LOBBY -> NEGOTIATION happens is the host hitting Start --
    # no auto-start, no headcount target. This pair exists purely so an
    # abandoned lobby doesn't sit open forever: once lobby_reminder_seconds
    # elapses, the host sees a "start now or ask for more time" prompt
    # (EXTEND_LOBBY_TIMER pushes the deadline out by this same amount
    # again, uncapped); if lobby_reminder_grace_seconds passes after that
    # with no response, the game auto-cancels (CancellationReason.LOBBY_TIMEOUT).
    lobby_reminder_seconds: int = 180
    lobby_reminder_grace_seconds: int = 60

    theme_set_id: str = "fictional_companies_v1"
    theme_set_version: int = 1
    valuation_policy_id: str = "linear_rank_v1"
    valuation_policy_version: int = 1
    final_scoring_policy_id: str = "haircut_risk_v1"
    final_scoring_policy_version: int = 1

    def close_threshold(self, player_count: int) -> int:
        if player_count <= 3:
            return player_count
        return math.ceil(0.75 * player_count)

    @model_validator(mode="after")
    def _validate_haircut_profiles_match_risk_band(self) -> "GameConfig":
        # Enforced invariant, not just convention -- see the Haircut-risk
        # design writeup's config/profile-drift guard. Fails loudly at
        # construction time (import time, for the module-level default
        # GameConfig) rather than at game-start.
        for n, profiles in self.haircut_profiles_by_players.items():
            market_size = self.market_size_by_players.get(n)
            if market_size is None:
                continue
            expected_depth = round(market_size * self.risk_depth_fraction)
            for profile in profiles:
                if profile.max_depth != expected_depth:
                    raise ValueError(
                        f"haircut_profiles_by_players[{n}] has a profile with max_depth="
                        f"{profile.max_depth}, expected {expected_depth} "
                        f"(round({market_size} * {self.risk_depth_fraction}))"
                    )
        return self


# --------------------------------------------------------------------------
# Market / holdings
# --------------------------------------------------------------------------


class MarketEntity(BaseModel):
    entity_id: str
    theme_key: str  # stable key into the game's ThemeSet — display name is resolved at projection time, not stored here
    position: int  # 1..N, unique — mutates only via SWAP_EXECUTED


class Holding(BaseModel):
    holding_id: str
    entity_id: str
    owner_player_id: str | None = None
    zone: HoldingZone
    revealed_to_owner: bool = False
    revealed_at_seq_no: int | None = None


class SwapIntent(BaseModel):
    """Shape, not a table — the common core of Proposal and Pool."""

    entity_a: str
    entity_b: str
    initiator_player_id: str
    # Locked once, at authoring time, via engine._rising_entity -- never
    # recomputed. A live re-derivation that disagrees with this means the
    # market crossed the original premise; see
    # engine._invalidate_crossed_negotiations and the market-direction
    # -reversal design writeup.
    rising_entity_id: str


class Proposal(BaseModel):
    proposal_id: str
    swap: SwapIntent
    # PROPOSAL_CREATED's own timestamp, duplicated onto the proposal itself
    # -- project() only ever sees the current Game snapshot, never the
    # event ledger, so the accept-lock (GameConfig.accept_lock_seconds)
    # needs this stored here to be checkable/displayable at all.
    created_at: datetime
    # Locked once, at PROPOSE_SWAP time, from the proposer's holdings at
    # that moment -- 1 iff they own the swap's rising entity. Never
    # recomputed later, even if their holdings or the market change before
    # this resolves -- see domain model discussion on private Influence
    # economy. Settled (committed->spent or committed->available) by
    # _resolve_proposal, not mutated directly by any handler.
    initiator_influence_liability: int = 0
    status: ResolutionStatus = ResolutionStatus.OPEN
    resolved_at_seq_no: int | None = None
    resolved_by_player_id: str | None = None
    resolution_reason: ProposalResolutionReason | None = None
    # Add-only, permanent per player -- see PASS_PROPOSAL. A player in this
    # set never sees this proposal (or any Pool on it) in their own live
    # view again; see projections.project()'s omission filter.
    passed_player_ids: set[str] = Field(default_factory=set)


class Pool(BaseModel):
    pool_id: str
    base_proposal_id: str
    swap: SwapIntent
    # Same locking rule as Proposal.initiator_influence_liability, but
    # against the pool's own swap (entity_c/entity_d) and the pooler's
    # holdings at CREATE_POOL time.
    initiator_influence_liability: int = 0
    visibility: PoolVisibility
    status: ResolutionStatus = ResolutionStatus.OPEN
    resolved_at_seq_no: int | None = None
    resolved_by_player_id: str | None = None
    resolution_reason: PoolResolutionReason | None = None


class PendingPickup(BaseModel):
    pending_pickup_id: str
    reserve_holding_id: str
    original_portfolio_holding_ids: list[str]
    revealed_entity_id: str
    started_at: datetime
    decision_deadline_at: datetime  # the pure pickup_decision_seconds figure — grace applied at check time only
    market_version_at_start: int
    cached_view: dict = Field(default_factory=dict)


class MarketCorrectionMove(BaseModel):
    """One corrective leg -- the targeted player's own holding (falling)
    swapped against a mutually-unowned neutral entity (rising). `swap` is
    a real SwapIntent with its own locked rising_entity_id, exactly like
    a proposal's -- routed through the same engine._execute_swap ->
    _invalidate_crossed_negotiations choke point, so an intervening
    action that crosses it is caught by the exact infrastructure that
    already protects ordinary proposals. See the Market Correction
    design writeup."""

    target_player_id: str
    swap: SwapIntent


class MarketCorrectionResolutionReason(StrEnum):
    TRIGGERED = "triggered"
    EXPIRED = "expired"
    # Any negotiated deal (accepted proposal/pool) executed while this was
    # pending -- unconditional, regardless of whether that deal touched
    # either move's entities. Distinct from INVALIDATED below: the market
    # resuming is sufficient on its own, it's not "this got structurally
    # broken." See _handle_accept_proposal/_handle_accept_pool.
    MARKET_RESUMED = "market_resumed"
    # A move's locked direction crossed, or a move's source/destination
    # ownership changed -- burns and DISCARD_HOLDING only, never
    # negotiated deals (which always resolve as MARKET_RESUMED instead).
    # See _market_correction_still_valid.
    INVALIDATED = "invalidated"


class PendingMarketCorrection(BaseModel):
    """Hidden until triggered or resolved -- see projections.project(),
    which exposes only {correction_id, expires_at} while this is pending,
    never `moves`. Constructed once, in full, before either player
    chooses; never recomputed underneath an acceptance -- see the
    invalidation rules in engine.py. 2-player-only mechanic."""

    correction_id: str
    offered_at: datetime
    expires_at: datetime
    moves: list[MarketCorrectionMove]  # always exactly 2, one per player


# --------------------------------------------------------------------------
# Players
# --------------------------------------------------------------------------


class GamePlayer(BaseModel):
    """A participant in *this* game — not the same thing as an account.
    `auth_user_id` links to Supabase auth (PlayerAccount is deferred, see §02)."""

    game_player_id: str
    auth_user_id: str
    seat: int
    display_name: str
    # Rolled once, server-side, at the moment this player was actually
    # seated (create_game's host / join_game's joiner) -- never at the
    # free-preview/reroll stage, so farming it means actually consuming one
    # of a game's <=6 seats, not just reloading a page. See player_names.py.
    is_golden_name: bool = False
    influence_available: int
    influence_committed: int = 0
    influence_spent: int = 0
    ready_to_close: bool = False
    pending_pickup: PendingPickup | None = None
    # Connection/presence deliberately NOT here — that's Supabase Realtime
    # Presence, infrastructure, never authoritative state.


# --------------------------------------------------------------------------
# The aggregate root
# --------------------------------------------------------------------------


class Game(BaseModel):
    id: str
    version: int = 0
    next_seq_no: int = 1
    phase: GamePhase = GamePhase.LOBBY
    created_at: datetime
    join_code: str
    host_player_id: str | None = None
    config: GameConfig = Field(default_factory=GameConfig)

    # LOBBY-only. Set at create_game to created_at + lobby_reminder_seconds;
    # pushed forward by the same amount, uncapped, by EXTEND_LOBBY_TIMER.
    # See apply_due_time_transitions for the reminder/grace/auto-cancel logic.
    lobby_reminder_deadline_at: datetime | None = None

    started_at: datetime | None = None
    max_duration_s: int | None = None
    unilateral_cutoff_at: datetime | None = None
    unilateral_window_closed_at: datetime | None = None
    close_threshold: int | None = None

    # 2-player-only anti-stagnation mechanic -- see the Market Correction
    # design writeup. last_negotiated_execution_at is set at START_GAME
    # to started_at and updated only by an accepted proposal/pool
    # (never a burn, never the correction itself); market_correction_cooldown_until
    # is also set to started_at initially (no extra grace beyond the
    # stagnation threshold itself), then pushed forward every time a
    # correction is offered.
    pending_market_correction: PendingMarketCorrection | None = None
    last_negotiated_execution_at: datetime | None = None
    market_correction_cooldown_until: datetime | None = None

    # Chosen and locked at START_GAME, hidden until haircut_reveal_at (or
    # until the game is SCORED, whichever comes first -- see project()) --
    # see the Haircut-risk design writeup. realized_haircut_depth is drawn
    # exactly once, at close_market, and persisted immediately; nothing
    # downstream (GAME_SCORED, project() at SCORED, replay) ever redraws
    # it, only reads it.
    haircut_profile: HaircutProfile | None = None
    haircut_reveal_at: datetime | None = None
    haircut_profile_revealed_at: datetime | None = None
    realized_haircut_depth: int | None = None

    closed_at: datetime | None = None
    close_reason: CloseReason | None = None
    scored_at: datetime | None = None
    cancellation_reason: CancellationReason | None = None

    market: dict[str, MarketEntity] = Field(default_factory=dict)  # entity_id -> MarketEntity
    players: list[GamePlayer] = Field(default_factory=list)
    holdings: dict[str, Holding] = Field(default_factory=dict)  # holding_id -> Holding
    proposals: dict[str, Proposal] = Field(default_factory=dict)
    pools: dict[str, Pool] = Field(default_factory=dict)

    def player_by_id(self, game_player_id: str | None) -> GamePlayer:
        for p in self.players:
            if p.game_player_id == game_player_id:
                return p
        from gotiate.domain.errors import NotFoundError

        raise NotFoundError(f"no such player {game_player_id!r} in this game")

    def player_by_auth_id(self, auth_user_id: str) -> GamePlayer | None:
        return next((p for p in self.players if p.auth_user_id == auth_user_id), None)


class CommandReceipt(BaseModel):
    """Is this exact request new, or a retry? See domain model §02/§08."""

    command_id: str
    game_id: str
    actor_game_player_id: str | None
    command_type: str
    expected_game_version: int | None
    received_at: datetime
    resulting_game_version: int | None = None
    status: CommandStatus
    error_code: str | None = None
    error_message: str | None = None
    result_event_seq_start: int | None = None
    result_event_seq_end: int | None = None
