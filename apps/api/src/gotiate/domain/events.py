"""The event vocabulary — domain model §05. Every event is stored unredacted
in the ledger; visibility is applied at projection time, not at write time."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class EventType(StrEnum):
    # Setup
    GAME_CREATED = "GAME_CREATED"
    PLAYER_JOINED = "PLAYER_JOINED"
    LOBBY_TIMER_EXTENDED = "LOBBY_TIMER_EXTENDED"
    GAME_STARTED = "GAME_STARTED"
    MARKET_INITIALIZED = "MARKET_INITIALIZED"
    PORTFOLIO_DEALT = "PORTFOLIO_DEALT"
    RESERVES_DEALT = "RESERVES_DEALT"
    HAIRCUT_PROFILE_SELECTED = "HAIRCUT_PROFILE_SELECTED"

    # Negotiation
    PROPOSAL_CREATED = "PROPOSAL_CREATED"
    PROPOSAL_RESOLVED = "PROPOSAL_RESOLVED"
    PRIVATE_POOL_CREATED = "PRIVATE_POOL_CREATED"
    PUBLIC_POOL_CREATED = "PUBLIC_POOL_CREATED"
    POOL_MADE_PUBLIC = "POOL_MADE_PUBLIC"
    POOL_RESOLVED = "POOL_RESOLVED"
    SWAP_EXECUTED = "SWAP_EXECUTED"
    PICKUP_STARTED = "PICKUP_STARTED"
    PICKUP_COMPLETED = "PICKUP_COMPLETED"
    PICKUP_FAILED = "PICKUP_FAILED"
    RESERVE_BURNED_FOR_SWAP = "RESERVE_BURNED_FOR_SWAP"
    READY_TO_CLOSE_CHANGED = "READY_TO_CLOSE_CHANGED"  # ledger only, actor-visible live
    # ledger only, actor-visible live -- see PASS_PROPOSAL. Never shown to
    # the proposer or anyone else; the proposer's only channel is the
    # aggregate passed_count on the live proposal projection.
    PROPOSAL_PASSED = "PROPOSAL_PASSED"
    # Fired by apply_due_time_transitions once haircut_reveal_fraction of
    # the clock has elapsed -- see the Haircut-risk design writeup. A game
    # that closes via the ready-threshold before this fires never sees it
    # live; project() reveals the profile anyway once phase is SCORED.
    HAIRCUT_RISK_REVEALED = "HAIRCUT_RISK_REVEALED"

    # Close & scoring
    UNILATERAL_WINDOW_CLOSED = "UNILATERAL_WINDOW_CLOSED"
    CLOSE_THRESHOLD_REACHED = "CLOSE_THRESHOLD_REACHED"
    MARKET_CLOSED = "MARKET_CLOSED"
    PORTFOLIOS_REVEALED = "PORTFOLIOS_REVEALED"
    GAME_SCORED = "GAME_SCORED"
    GAME_ENDED = "GAME_ENDED"
    GAME_CANCELLED = "GAME_CANCELLED"


class GameEvent(BaseModel):
    game_id: str
    seq_no: int
    type: EventType
    actor_game_player_id: str | None = None  # None for system-triggered events
    payload: dict = {}
    created_at: datetime
