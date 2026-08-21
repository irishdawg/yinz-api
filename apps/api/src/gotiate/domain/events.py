"""The event vocabulary — domain model §05. Every event is stored unredacted
in the ledger; visibility is applied at projection time, not at write time.

Cadence/economy redesign (prototype branch): checkpoint 1 removed every
event exclusive to Influence, Market Correction, the gameplay clock, and the
old Reserve/Pickup/unilateral-burn mechanic. Checkpoint 2 makes PASS public
and reintroduces the Haircut reveal event and a table-wide Boost-expiry
event, both now Move-driven rather than clock-driven. Arbitration and
per-Boost-use events are added in later checkpoints, not here."""

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
    # Fired by engine._maybe_reveal_haircut once cumulative Moves consumed
    # first reaches or crosses 50% of the initial total Move allocation --
    # replaces the old clock-fraction trigger, same event name/shape. A
    # game that closes via Ready-to-Close (or Move exhaustion) before that
    # point never sees this live; project() reveals the profile
    # unconditionally once phase is SCORED regardless.
    HAIRCUT_RISK_REVEALED = "HAIRCUT_RISK_REVEALED"
    # Fired exactly once, table-wide, by engine._maybe_expire_boosts the
    # instant any single player's own moves_remaining first hits zero --
    # this is the unilateral cutoff now, expressed as game state rather
    # than a timer.
    BOOSTS_EXPIRED = "BOOSTS_EXPIRED"

    # Negotiation
    PROPOSAL_CREATED = "PROPOSAL_CREATED"
    PROPOSAL_RESOLVED = "PROPOSAL_RESOLVED"
    PRIVATE_POOL_CREATED = "PRIVATE_POOL_CREATED"
    PUBLIC_POOL_CREATED = "PUBLIC_POOL_CREATED"
    POOL_MADE_PUBLIC = "POOL_MADE_PUBLIC"
    POOL_RESOLVED = "POOL_RESOLVED"
    SWAP_EXECUTED = "SWAP_EXECUTED"
    READY_TO_CLOSE_CHANGED = "READY_TO_CLOSE_CHANGED"  # ledger only, actor-visible live
    # Fully public -- a Pass is now a visible, intentional information leak
    # that narrows the active participant set for everyone to see, not a
    # private signal to the proposer alone.
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
