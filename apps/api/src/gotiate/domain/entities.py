"""Domain entities — the conceptual shapes from the Gotiate domain model doc, §02/§03.

Pure data. No I/O, no FastAPI, no persistence. `Game` is the authoritative
aggregate: one instance per match, mutated only by `domain.engine`.

Cadence/economy redesign (prototype branch): Influence, the gameplay clock,
Market Correction, Accept Lock, and the old Reserve/Pickup/unilateral-burn
mechanic are removed (checkpoint 1). Moves gate opening a negotiation,
enforce a single active negotiation table-wide, and drive the Haircut
reveal/Boost-expiry/game-end triggers (checkpoint 2). Boosts and Arbitration
remain scaffolded, wired up for real in later checkpoints -- see the
redesign plan artifact for the full sequence.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


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
    PORTFOLIO = "portfolio"
    DISCARDED = "discarded"


class ResolutionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ProposalResolutionReason(StrEnum):
    EXECUTED = "executed"
    # NOTE (cadence/economy redesign): the opener can no longer withdraw --
    # spending a Move commits the table (rule 4). WITHDRAWN_BY_INITIATOR is
    # gone; there is no voluntary-withdrawal path left for a bare proposal.
    MARKET_CLOSED = "market_closed"
    # Every other seated player has PASS_PROPOSAL'd -- mathematically dead,
    # nobody rejected it. Fully public, unmasked -- Pass itself is public
    # now, so there is nothing left to hide about its consequence either.
    EXPIRED_ALL_PASSED = "expired_all_passed"
    # The market moved under this proposal's locked direction (see
    # SwapIntent.rising_entity_id) -- the opposite of EXPIRED_ALL_PASSED:
    # this is deliberately LOUD, never masked, for every live audience.
    # See the market-direction-reversal design writeup.
    VOIDED_MARKET_SWUNG = "voided_market_swung"
    # Arbitration's machine draw landed on "nothing happens" -- the
    # negotiation concludes with no deal and no market change. The Move
    # the opener spent to create it is still never refunded (rule 1).
    ARBITRATION_NEITHER = "arbitration_neither"


class PoolResolutionReason(StrEnum):
    EXECUTED = "executed"
    # The pool's own initiator withdrew it -- unaffected by the redesign;
    # Pools aren't Moves, so this stays fully legal (contrast the base
    # proposal, which can no longer be withdrawn at all).
    WITHDRAWN_BY_INITIATOR = "withdrawn_by_initiator"
    INVALIDATED_BY_INITIATOR_ACTION = "invalidated_by_initiator_action"
    DECLINED_BY_TARGET = "declined_by_target"
    # Every player who could still accept this Pool passed it. For a
    # private Pool that eligible set contains only the base proposer, so
    # their first Pass resolves it immediately.
    EXPIRED_ALL_PASSED = "expired_all_passed"
    PREEMPTED_BY_OTHER_ACTION = "preempted_by_other_action"
    # NOTE (cadence/economy redesign): BASE_PROPOSAL_WITHDRAWN is gone --
    # a base proposal can no longer be withdrawn, so a Pool attached to it
    # can never be cascaded this way again.
    MARKET_CLOSED = "market_closed"
    # This pool's own leg crossed -- mirrors ProposalResolutionReason's
    # value above, never masked. Distinct from BASE_PROPOSAL_VOIDED below:
    # this pool's own direction, not its base proposal's, is what broke.
    VOIDED_MARKET_SWUNG = "voided_market_swung"
    # Cascaded because this pool's base proposal voided (crossing
    # invalidation, not withdrawal).
    BASE_PROPOSAL_VOIDED = "base_proposal_voided"


class PoolVisibility(StrEnum):
    PRIVATE = "private"
    PUBLIC = "public"


class ArbitrationChoice(StrEnum):
    """The three machine outcomes -- and, identically, the three legal
    jury votes, since a vote just names which outcome a juror wants to
    lean the machine toward. Chaos/random-swap is deliberately not a
    member -- parked out of this prototype entirely, not merely unused."""

    BASE = "base"
    POOL = "pool"
    NEITHER = "neither"


class ArbitrationResolutionReason(StrEnum):
    # The active pair settled it themselves during the 20s window (a
    # normal ACCEPT_PROPOSAL/ACCEPT_POOL) -- the machine never drew.
    SETTLED_NORMALLY = "settled_normally"
    MACHINE_BASE = "machine_base"
    MACHINE_POOL = "machine_pool"
    MACHINE_NEITHER = "machine_neither"
    # Ready-to-Close (or any other close_market trigger) preempted the
    # whole thing before it could resolve on its own -- outranks a
    # pending machine draw outright, no draw ever happens.
    MARKET_CLOSED = "market_closed"


class BoostDrawFailureReason(StrEnum):
    """Why a Draw/Refresh decision ended without the drawn entity landing
    in the player's portfolio -- the Boost itself is spent regardless (see
    PendingBoostDraw), these only distinguish *why* nothing was kept."""

    DECLINED_BY_PLAYER = "declined_by_player"
    DECISION_TIMEOUT = "decision_timeout"
    MARKET_CLOSED = "market_closed"


class CloseReason(StrEnum):
    READY_THRESHOLD = "READY_THRESHOLD"
    # NOTE (cadence/economy redesign): TIME_EXPIRED is gone -- there is no
    # gameplay clock. The game now also ends once every seated player's own
    # moves_remaining has hit zero AND there is no active negotiation left
    # to resolve -- deliberately not the same instant as boosts_expired
    # (that fires on the *first* player to hit zero); see
    # engine._maybe_close_on_moves_exhausted.
    MOVES_EXHAUSTED = "MOVES_EXHAUSTED"
    # No OPTIONALITY_EXHAUSTED for V1 — deliberately deferred, see decision log §10.
    # A real-play gap found post-redesign: removing the gameplay clock also
    # removed the only force-close path that never depended on a player
    # doing something. Fires once negotiation_abandonment_seconds has
    # passed since the last successfully handled command for this game --
    # see Game.last_activity_at and engine._maybe_close_on_abandonment.
    # Deliberately NOT a revived gameplay clock: nobody sees a countdown,
    # nobody races it, it never affects strategy -- purely a safety valve,
    # same spirit as the pre-existing LOBBY abandonment auto-cancel
    # (lobby_reminder_deadline_at/lobby_reminder_grace_seconds).
    ABANDONED = "ABANDONED"


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
    # Target risky depth = round(market_size * risk_depth_fraction) -- see
    # the worked table in the design writeup (11->4, 13->5, 15->5, 17->6).
    risk_depth_fraction: float = 0.35
    portfolio_shape: list[int] = Field(default_factory=lambda: [2, 1, 1, 1])
    allow_public_pools: bool = True
    allow_private_pools: bool = True
    ready_to_close_revealed_in_replay: bool = False

    # --- Cadence/economy redesign: Moves & Boosts (checkpoint 1 schema,
    # checkpoint 2/4 wire up the actual gating logic) ---
    starting_moves: int = 5
    starting_boosts: int = 2
    concentrate_max_copies: int = 3
    # Draw/Refresh's own short local decision window -- distinct from, and
    # much shorter than, the Arbitration window; both are the deliberate
    # exceptions to an otherwise clockless negotiation cadence.
    boost_draw_decision_seconds: float = 20.0

    # --- Cadence/economy redesign: Arbitration (checkpoint 3) ---
    arbitration_window_seconds: float = 20.0
    # Deliberately weights, not percentages -- they don't need to sum to
    # 100, which keeps the additive jury-vote math (below) clean. Keyed by
    # caller role: the active participant who calls Arbitration shifts the
    # odds slightly away from the outcome they themselves triggered it
    # from, on the theory that calling it is itself a signal about what
    # you think you'd get from a normal negotiated settlement instead.
    arbitration_base_weights: dict[str, dict[str, int]] = Field(
        default_factory=lambda: {
            "originator": {"base": 30, "pool": 40, "neither": 40},
            "other": {"base": 40, "pool": 30, "neither": 40},
        }
    )
    # Each juror's secret vote is additive and cumulative, independent of
    # every other juror's: +bonus to the voted choice, -penalty to each of
    # the other legal choices, floored at zero. Normalized only once, at
    # draw time -- see engine._resolve_arbitration_via_machine. Influential,
    # never determinative: the jury can lean on the machine, never become it.
    arbitration_vote_bonus: int = 10
    arbitration_vote_penalty: int = 5

    # NEGOTIATION-phase abandonment backstop (see CloseReason.ABANDONED) --
    # the game force-closes once this many seconds pass with no command
    # successfully handled for it. Deliberately short: this cadence is
    # designed around serial attention with no dead air, so a long silence
    # is a much stronger abandonment signal than it would be under the old
    # clocked design.
    negotiation_abandonment_seconds: float = 600.0

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


class ProtectedPair(BaseModel):
    """The two entities from the most recently Force-Swapped pair, if their
    direct reverse is still locked -- see engine._use_boost_force_swap and
    Game.protected_pair. A single global slot, not per-player, not a list
    -- unconditionally overwritten by the *next* Force Swap regardless of
    which entities it targets (a Boost undoing a Boost is always fair,
    same-cost symmetric play; only the cheaper, Move-only negotiated
    reversal route is what this blocks). Order is not meaningful --
    engine._is_protected_reversal compares as an unordered pair."""

    entity_a: str
    entity_b: str


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


class PendingArbitration(BaseModel):
    """Exists only for the negotiation's own final two active
    participants (the opener plus its one remaining non-passed
    responder). Constructed once, at CALL_ARBITRATION time, and never
    recomputed -- `base_weights` locks the caller-role-dependent starting
    weights and, if no Pool survived to eligibility, drops "pool" from
    the candidate set entirely rather than assigning it a weight of zero
    (so a juror is never offered an illegal vote for an outcome that
    isn't on the table). `votes` accumulates secretly; nobody (not even
    the two active participants) sees a vote's contents live -- see
    projections.py's redaction. Cleared the instant this negotiation
    resolves, one way or another; the full detail survives permanently in
    the ARBITRATION_RESOLVED event for Replay, not here."""

    arbitration_id: str
    called_by: str
    called_at: datetime
    resolves_at: datetime  # called_at + GameConfig.arbitration_window_seconds
    # The one Pool eligible for the "pool" outcome, if any -- see the
    # engine's own "which Pool" derivation: at most one can structurally
    # exist once eligibility (exactly one active responder) is reached.
    eligible_pool_id: str | None
    # {"base": int, "pool": int (only if eligible_pool_id is set),
    # "neither": int} -- keys ARE the legal choice set, both for casting
    # a vote and for the machine's own draw.
    base_weights: dict[str, int]
    # juror_game_player_id -> ArbitrationChoice value. Secret live; see
    # projections.py -- only "who has voted" (not what) is ever projected
    # to a live audience, and only ReplayAudience sees this dict itself.
    votes: dict[str, str] = Field(default_factory=dict)


class Proposal(BaseModel):
    proposal_id: str
    swap: SwapIntent
    # PROPOSAL_CREATED's own timestamp, duplicated onto the proposal itself.
    created_at: datetime
    status: ResolutionStatus = ResolutionStatus.OPEN
    resolved_at_seq_no: int | None = None
    resolved_by_player_id: str | None = None
    resolution_reason: ProposalResolutionReason | None = None
    # Add-only, permanent per player -- see PASS_PROPOSAL. Fully public
    # (projections._project_proposal exposes it unconditionally): this is
    # the narrowing active-participant-set mechanic itself, and a passed
    # player is Arbitration jury-eligible the instant it's called. A
    # player in this set can no longer Accept/Pool/Pass this proposal
    # again, but still sees it -- narrowing is visible, not an omission.
    passed_player_ids: set[str] = Field(default_factory=set)
    # None until CALL_ARBITRATION, cleared the instant this negotiation
    # resolves (any reason) -- see engine._resolve_proposal, the single
    # chokepoint that clears it exactly like it clears active_proposal_id.
    pending_arbitration: PendingArbitration | None = None


class PendingBoostDraw(BaseModel):
    """A single player's own frozen Draw/Refresh decision window --
    entirely private to them (never a jury, unlike PendingArbitration).
    `revealed_entity_id` was drawn uniformly from the set of entities this
    player owned zero copies of *at the moment USE_BOOST(draw) was
    submitted*; that eligibility set is never recomputed, so discarding a
    holding during the decision can't retroactively make some other
    entity "the draw" (see engine._handle_boost_draw). The Boost itself
    is already spent the instant this is created -- declining or timing
    out never refunds it, only RESOLVE_BOOST_DRAW's discard choice
    differs from DECLINE_BOOST_DRAW/timeout in whether the drawn entity
    actually lands in the portfolio. `cached_view` is this player's own
    project() output, snapshotted before this was attached, and served
    back verbatim for every read while pending -- the same frozen-view
    pattern the old Pick Up flow used, so a client mid-decision can never
    observe a live board state it hasn't been asked to react to yet."""

    pending_boost_draw_id: str
    revealed_entity_id: str
    original_portfolio_holding_ids: list[str]
    started_at: datetime
    decision_deadline_at: datetime
    cached_view: dict = Field(default_factory=dict)


class Pool(BaseModel):
    pool_id: str
    base_proposal_id: str
    swap: SwapIntent
    visibility: PoolVisibility
    status: ResolutionStatus = ResolutionStatus.OPEN
    resolved_at_seq_no: int | None = None
    resolved_by_player_id: str | None = None
    resolution_reason: PoolResolutionReason | None = None
    # Add-only and public, scoped to this Pool. Passing a Pool does not
    # pass the base proposal or any sibling Pool.
    passed_player_ids: set[str] = Field(default_factory=set)


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
    ready_to_close: bool = False
    # --- Cadence/economy redesign ---
    # Decremented only by opening a new bare negotiation, never refunded.
    moves_remaining: int
    # Decremented only by USE_BOOST. Boost legality also requires
    # `not Game.boosts_expired` and no Arbitration currently active on the
    # game's active negotiation -- see engine.py once checkpoint 4 lands.
    boosts_remaining: int
    # None except during this player's own Draw/Refresh decision window --
    # see PendingBoostDraw. Entirely private; never surfaced to any other
    # audience, live or Replay-adjacent, beyond "this player used a Boost."
    pending_boost_draw: PendingBoostDraw | None = None
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
    close_threshold: int | None = None

    # --- Cadence/economy redesign ---
    # At most one bare negotiation open table-wide at any time --
    # PROPOSE_SWAP is illegal whenever this is set. Cleared, centrally, by
    # engine._resolve_proposal the instant the matching proposal resolves
    # (any reason) -- never set/cleared anywhere else, so there is exactly
    # one place a future reviewer needs to check for "is this in sync."
    active_proposal_id: str | None = None
    # Flips False -> True exactly once, the instant any single player's own
    # moves_remaining first hits zero (not when everyone's does), and never
    # resets. See engine._maybe_expire_boosts.
    boosts_expired: bool = False
    # Set at create_game, bumped to `now` by handle_command on every
    # successfully handled command thereafter (system-triggered time
    # -transitions -- lobby auto-cancel, the Arbitration machine draw, a
    # Boost-draw timeout -- deliberately never bump it; only genuine player
    # action counts). Drives CloseReason.ABANDONED once
    # negotiation_abandonment_seconds passes with no activity during
    # NEGOTIATION -- see engine._maybe_close_on_abandonment.
    last_activity_at: datetime

    # Force Swap durability: the pair most recently Force-Swapped, still
    # locked against a direct (Move-only, negotiated) reverse -- see
    # ProtectedPair's own docstring, engine._is_protected_reversal, and
    # engine._use_boost_force_swap. None whenever no Force Swap has
    # happened yet, or the lock has since cleared (see engine._execute_swap).
    protected_pair: ProtectedPair | None = None

    # Chosen and locked at START_GAME, hidden until haircut_profile_revealed_at
    # (or until the game is SCORED, whichever comes first -- see project()) --
    # see the Haircut-risk design writeup. realized_haircut_depth is drawn
    # exactly once, at close_market, and persisted immediately; nothing
    # downstream (GAME_SCORED, project() at SCORED, replay) ever redraws
    # it, only reads it.
    #
    # NOTE (cadence/economy redesign): the live reveal trigger was
    # previously a fraction of the gameplay clock; the clock is gone. It
    # now fires the instant cumulative Moves consumed across the table
    # first reaches or crosses 50% of the initial total Move allocation --
    # see engine._maybe_reveal_haircut.
    haircut_profile: HaircutProfile | None = None
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
