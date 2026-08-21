"""The event vocabulary — domain model §05. Every event is stored unredacted
in the ledger; visibility is applied at projection time, not at write time.

Cadence/economy redesign (prototype branch): checkpoint 1 removed every
event exclusive to Influence, Market Correction, the gameplay clock, and the
old Reserve/Pickup/unilateral-burn mechanic. Checkpoint 2 makes PASS public
and reintroduces the Haircut reveal event and a table-wide Boost-expiry
event, both now Move-driven rather than clock-driven. Checkpoint 3 adds
Arbitration's event family. Per-Boost-use events are added in checkpoint 4."""

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

    # Arbitration (checkpoint 3) -- see engine._handle_call_arbitration /
    # _resolve_arbitration_via_machine. Public: either active participant
    # already knows who called it, and this is the irreversible starting
    # gun for the whole 20s window.
    ARBITRATION_CALLED = "ARBITRATION_CALLED"
    # Fires only when the one eligible Pool was still private at call
    # time -- the forced-visibility exception jurors need to vote on
    # informed terms. Never fires for an already-public eligible Pool, or
    # when no Pool is eligible at all.
    ARBITRATION_POOL_REVEALED = "ARBITRATION_POOL_REVEALED"
    # ledger only, actor-visible live -- same shape as READY_TO_CLOSE_CHANGED:
    # the vote's own content lives in the payload, but EVENT_VISIBILITY
    # keeps it to the voter alone until Replay. Only "a juror has voted"
    # (never which one) is ever separately projected live -- see
    # projections._project_proposal's voted_player_ids.
    ARBITRATION_VOTE_CAST = "ARBITRATION_VOTE_CAST"
    # Public reason (settled_normally / machine_base / machine_pool /
    # machine_neither / market_closed) -- the fact of how it ended, and
    # any resulting SWAP_EXECUTED, are already publicly observable through
    # other events regardless. The weights and the jury's actual votes are
    # a bespoke live redaction in project_events -- never shown to any
    # live audience, including the two active participants -- unlocked
    # only for ReplayAudience. See the Market Correction precedent this
    # redaction shape is modeled on.
    ARBITRATION_RESOLVED = "ARBITRATION_RESOLVED"

    # Boosts (checkpoint 4) -- see engine._handle_use_boost and its three
    # sub-handlers. All five are ACTOR_ONLY: a Boost is a unilateral,
    # private action with no public market-visible symptom of its own
    # (Force Swap's own SWAP_EXECUTED already carries the public half of
    # that one), so only the acting player sees these in their own
    # activity log live; full detail survives for Replay regardless.
    BOOST_CONCENTRATE_USED = "BOOST_CONCENTRATE_USED"
    BOOST_FORCE_SWAP_USED = "BOOST_FORCE_SWAP_USED"
    # Draw/Refresh's three-event lifecycle -- STARTED fires the instant
    # USE_BOOST(draw) is submitted (the Boost is already spent at this
    # point, see PendingBoostDraw), then exactly one of COMPLETED
    # (RESOLVE_BOOST_DRAW) or FAILED (DECLINE_BOOST_DRAW, timeout, or a
    # market close forcing every pending decision closed) follows.
    BOOST_DRAW_STARTED = "BOOST_DRAW_STARTED"
    BOOST_DRAW_COMPLETED = "BOOST_DRAW_COMPLETED"
    BOOST_DRAW_FAILED = "BOOST_DRAW_FAILED"

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
