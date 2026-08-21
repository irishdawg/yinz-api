"""The event vocabulary — domain model §05. Every event is stored unredacted
in the ledger; visibility is applied at projection time, not at write time.

Cadence/economy redesign (prototype branch), checkpoint 1: removes every
event exclusive to Influence, Market Correction, the gameplay clock, and the
old Reserve/Pickup/unilateral-burn mechanic. Arbitration, Boost, and
Move-driven events are added in later checkpoints, not here."""

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
    HAIRCUT_PROFILE_SELECTED = "HAIRCUT_PROFILE_SELECTED"

    # Negotiation
    PROPOSAL_CREATED = "PROPOSAL_CREATED"
    PROPOSAL_RESOLVED = "PROPOSAL_RESOLVED"
    PRIVATE_POOL_CREATED = "PRIVATE_POOL_CREATED"
    PUBLIC_POOL_CREATED = "PUBLIC_POOL_CREATED"
    POOL_MADE_PUBLIC = "POOL_MADE_PUBLIC"
    POOL_RESOLVED = "POOL_RESOLVED"
    SWAP_EXECUTED = "SWAP_EXECUTED"
    READY_TO_CLOSE_CHANGED = "READY_TO_CLOSE_CHANGED"  # ledger only, actor-visible live
    # ledger only, actor-visible live -- see PASS_PROPOSAL. Never shown to
    # the proposer or anyone else; the proposer's only channel is the
    # aggregate passed_count on the live proposal projection.
    #
    # NOTE (cadence/economy redesign): this stays ACTOR_ONLY in checkpoint 1
    # only because the visibility flip to PUBLIC is scoped to a later
    # checkpoint alongside the rest of the Pass/narrowing rework.
    PROPOSAL_PASSED = "PROPOSAL_PASSED"

    # Close & scoring
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
