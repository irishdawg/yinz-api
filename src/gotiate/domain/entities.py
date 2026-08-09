"""Domain entities — the conceptual shapes from the Gotiate domain model doc, §02/§03.

Pure data. No I/O, no FastAPI, no persistence. `Game` is the authoritative
aggregate: one instance per match, mutated only by `domain.engine`.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class GamePhase(StrEnum):
    LOBBY = "LOBBY"
    NEGOTIATION = "NEGOTIATION"
    CLOSING = "CLOSING"
    SCORED = "SCORED"


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


class PoolResolutionReason(StrEnum):
    EXECUTED = "executed"
    WITHDRAWN_BY_INITIATOR = "withdrawn_by_initiator"
    INVALIDATED_BY_INITIATOR_ACTION = "invalidated_by_initiator_action"
    DECLINED_BY_TARGET = "declined_by_target"
    PREEMPTED_BY_OTHER_ACTION = "preempted_by_other_action"
    BASE_PROPOSAL_WITHDRAWN = "base_proposal_withdrawn"
    MARKET_CLOSED = "market_closed"


class PoolVisibility(StrEnum):
    PRIVATE = "private"
    PUBLIC = "public"


class PickupFailureReason(StrEnum):
    DECISION_TIMEOUT = "decision_timeout"
    MARKET_CLOSED = "market_closed"


class CloseReason(StrEnum):
    TIME_EXPIRED = "TIME_EXPIRED"
    READY_THRESHOLD = "READY_THRESHOLD"
    # No OPTIONALITY_EXHAUSTED for V1 — deliberately deferred, see decision log §10.


class CommandStatus(StrEnum):
    APPLIED = "applied"
    REJECTED_STALE_VERSION = "rejected_stale_version"
    REJECTED_ILLEGAL = "rejected_illegal"
    REJECTED_DUPLICATE_REPLAY = "rejected_duplicate_replay"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class GameConfig(BaseModel):
    """Frozen onto Game at START_GAME. Later tuning changes never retroactively
    affect a live game — see domain model §09."""

    market_size_by_players: dict[int, int] = Field(
        default_factory=lambda: {2: 9, 3: 11, 4: 13, 5: 15, 6: 17}
    )
    max_clock_seconds_by_players: dict[int, int] = Field(
        default_factory=lambda: {2: 480, 3: 720, 4: 960, 5: 1200, 6: 1440}
    )
    starting_influence: int = 10
    unilateral_cutoff_fraction: float = 0.10
    portfolio_shape: list[int] = Field(default_factory=lambda: [2, 1, 1, 1])
    reserve_count: int = 2
    pickup_decision_seconds: float = 5.0
    pickup_transport_grace_ms: int = 500
    allow_public_pools: bool = True
    allow_private_pools: bool = True
    ready_to_close_revealed_in_replay: bool = False

    theme_set_id: str = "fictional_companies_v1"
    theme_set_version: int = 1
    valuation_policy_id: str = "linear_rank_v1"
    valuation_policy_version: int = 1
    final_scoring_policy_id: str = "waterline_baseline_v1"
    final_scoring_policy_version: int = 1
    waterline_selection_policy_id: str = "uniform_random_v1"
    waterline_selection_policy_version: int = 1

    def close_threshold(self, player_count: int) -> int:
        if player_count <= 3:
            return player_count
        return math.ceil(0.75 * player_count)


# --------------------------------------------------------------------------
# Market / holdings
# --------------------------------------------------------------------------


class MarketEntity(BaseModel):
    entity_id: str
    theme_key: str  # display name for now — real ThemeSet/version resolution is future work
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


class Proposal(BaseModel):
    proposal_id: str
    swap: SwapIntent
    status: ResolutionStatus = ResolutionStatus.OPEN
    resolved_at_seq_no: int | None = None
    resolved_by_player_id: str | None = None
    resolution_reason: ProposalResolutionReason | None = None


class Pool(BaseModel):
    pool_id: str
    base_proposal_id: str
    swap: SwapIntent
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
    join_code: str
    host_player_id: str | None = None
    config: GameConfig = Field(default_factory=GameConfig)

    started_at: datetime | None = None
    max_duration_s: int | None = None
    unilateral_cutoff_at: datetime | None = None
    unilateral_window_closed_at: datetime | None = None
    close_threshold: int | None = None

    closed_at: datetime | None = None
    close_reason: CloseReason | None = None
    scored_at: datetime | None = None

    market: dict[str, MarketEntity] = Field(default_factory=dict)  # entity_id -> MarketEntity
    players: list[GamePlayer] = Field(default_factory=list)
    holdings: dict[str, Holding] = Field(default_factory=dict)  # holding_id -> Holding
    proposals: dict[str, Proposal] = Field(default_factory=dict)
    pools: dict[str, Pool] = Field(default_factory=dict)

    waterline_entity_id: str | None = None
    waterline_revealed: bool = False

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
