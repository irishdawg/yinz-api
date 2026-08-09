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
    GAME_STARTED = "GAME_STARTED"
    MARKET_INITIALIZED = "MARKET_INITIALIZED"
    PORTFOLIO_DEALT = "PORTFOLIO_DEALT"
    RESERVES_DEALT = "RESERVES_DEALT"
    WATERLINE_SELECTED = "WATERLINE_SELECTED"

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

    # Close & scoring
    UNILATERAL_WINDOW_CLOSED = "UNILATERAL_WINDOW_CLOSED"
    CLOSE_THRESHOLD_REACHED = "CLOSE_THRESHOLD_REACHED"
    MARKET_CLOSED = "MARKET_CLOSED"
    WATERLINE_REVEALED = "WATERLINE_REVEALED"
    PORTFOLIOS_REVEALED = "PORTFOLIOS_REVEALED"
    GAME_SCORED = "GAME_SCORED"
    GAME_ENDED = "GAME_ENDED"


class GameEvent(BaseModel):
    game_id: str
    seq_no: int
    type: EventType
    actor_game_player_id: str | None = None  # None for system-triggered events
    payload: dict = {}
    created_at: datetime
