"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface GamePlayerView {
  game_player_id: string;
  seat: number;
  display_name: string;
  is_golden_name: boolean;
  // Public roster facts (cadence/economy redesign) -- players need to see
  // who can still seize initiative (Moves) or still has a unilateral card
  // to play (Boosts). Decremented only by opening a negotiation / by
  // USE_BOOST, respectively; moves_remaining never refunds.
  moves_remaining: number;
  boosts_remaining: number;
  // Self-only while the game is live. Also present for every player once
  // the market closed via CloseReason READY_THRESHOLD -- the results
  // leaderboard uses it to mark who voted to close.
  ready_to_close?: boolean;
  // Self-only -- see the Haircut-risk design writeup. projected_value is
  // the unconditional linear-rank sum (what you'd score if nothing were
  // wiped); safe_value only appears once the profile is revealed (or the
  // game is scored), and only covers positions that cannot possibly be
  // wiped.
  projected_value?: number;
  safe_value?: number;
}

export interface HoldingView {
  holding_id: string;
  // Never null -- the cadence/economy redesign removed every unrevealed
  // -to-owner zone (the old reserve mechanic); a player's own holdings
  // list is always fully revealed to them.
  entity_id: string;
  owner_player_id: string;
  zone: "portfolio" | "discarded";
  display_name: string | null;
  ticker_symbol: string | null;
  logo_url: string | null;
}

export interface PendingArbitrationView {
  arbitration_id: string;
  called_by: string;
  caller_role: "originator" | "other";
  resolves_at: string;
  // Non-null only if the remaining active responder still has an eligible
  // Pool on this negotiation -- see engine._eligible_arbitration_pool_id.
  eligible_pool_id: string | null;
  // "Who has voted," never what -- the jury's actual votes stay server
  // -only/Replay-only, see EVENT_VISIBILITY's ARBITRATION_VOTE_CAST entry.
  voted_player_ids: string[];
}

export interface ProposalView {
  proposal_id: string;
  entity_a: string;
  entity_b: string;
  // Locked at PROPOSE_SWAP time, unconditionally public -- which of the
  // two entities is intended to rise. Never recompute this from current
  // positions; Visualize must stay pinned to it. See the
  // market-direction-reversal design writeup.
  rising_entity_id: string;
  proposer_id: string;
  status: "open" | "resolved";
  resolution_reason: "executed" | "market_closed" | "expired_all_passed" | "voided_market_swung" | "arbitration_neither" | null;
  // Fully public, identities and all (cadence/economy redesign) -- Pass is
  // a visible, intentional information leak that narrows the active
  // participant set for everyone to see, and marks jury eligibility for
  // Arbitration. Empty until anyone passes.
  passed_player_ids: string[];
  // Non-null once CALL_ARBITRATION has fired for this negotiation, cleared
  // the instant it resolves (any reason).
  pending_arbitration: PendingArbitrationView | null;
}

export interface PoolView {
  pool_id: string;
  base_proposal_id: string;
  visibility: "private" | "public";
  initiator_id: string;
  status: "open" | "resolved";
  resolution_reason:
    | "executed"
    | "withdrawn_by_initiator"
    | "invalidated_by_initiator_action"
    | "declined_by_target"
    | "expired_all_passed"
    | "preempted_by_other_action"
    | "market_closed"
    | "voided_market_swung"
    | "base_proposal_voided"
    | null;
  passed_player_ids: string[];
  // Present only when this audience can see the pool's contents (public
  // pool, or an insider) -- see projections._project_pool. Direction is
  // content, gated the same as entity_c/entity_d.
  entity_c?: string;
  entity_d?: string;
  rising_entity_id?: string;
}

export interface GameView {
  game_id: string;
  version: number;
  phase: "LOBBY" | "NEGOTIATION" | "CLOSING" | "SCORED" | "CANCELLED";
  join_code: string | null;
  host_player_id: string | null;
  you: string | null;
  // LOBBY-only. Once now() passes the deadline, the host sees a "start or
  // ask for more time" prompt; lobby_reminder_grace_seconds after that
  // with no response, the game auto-cancels.
  lobby_reminder_deadline_at: string | null;
  lobby_reminder_grace_seconds: number;
  // Set at START_GAME -- always non-null once phase is NEGOTIATION or
  // later, always null in LOBBY. No gameplay clock derives from it
  // anymore (cadence/economy redesign) -- it's elapsed-time telemetry
  // only.
  started_at: string | null;
  // Hidden until revealed (project() gates its contents on
  // haircut_profile_revealed_at server-side, but never exposes that
  // timestamp itself) or unconditionally once scored. The live reveal
  // trigger is now Move-driven, not clock-driven -- it fires once
  // cumulative Moves consumed first crosses 50% of the table's total
  // allocation. That crossing point is a fixed count, surfaced as
  // haircut_reveal_in_moves below so the UI can show the countdown the
  // old gameplay clock used to.
  haircut_profile: { depth_probabilities: number[] } | null;
  // Moves the table must still burn before haircut_profile reveals.
  // Counts down to 0; null once revealed, once scored, or in LOBBY.
  haircut_reveal_in_moves: number | null;
  // Public from game start, unlike haircut_profile itself -- every
  // configured profile for this player count shares the same max_depth,
  // so "positions 1..N carry some risk" is structural, not profile
  // -specific. Positions beyond this depth are 100% safe immediately, not
  // just after reveal.
  haircut_risk_band_depth: number | null;
  // Only meaningful once phase is CANCELLED.
  cancellation_reason: "HOST_INITIATED" | "LOBBY_TIMEOUT" | null;
  // The *fact* of why/when the market closed, revealed only at the
  // instant it happens -- not a leading indicator. ready_to_close itself
  // (above, per-player) stays strictly self-only *while the game is live*
  // -- a secret trigger, not a public countdown -- and is only revealed
  // table-wide afterwards if this is "READY_THRESHOLD".
  close_reason: "READY_THRESHOLD" | "MOVES_EXHAUSTED" | "ABANDONED" | null;
  closed_at: string | null;
  // Public and unconditional -- at most one bare negotiation open
  // table-wide at any time; null whenever the table is open for anyone
  // with Moves left to seize initiative.
  active_proposal_id: string | null;
  // Public and unconditional, flips False -> True exactly once, the
  // instant any single player's own moves_remaining first hits zero --
  // every Boost control should disable table-wide the instant this is
  // true, in addition to the separate, negotiation-scoped Arbitration gate.
  boosts_expired: boolean;
  // Public config fact -- the max legal copy count for a single Concentrate
  // target, used to grey out an already-at-cap card in its own card-tap
  // picker rather than hardcoding the default.
  concentrate_max_copies: number;
  // Public and unconditional -- the pair most recently Force Swapped,
  // still locked against a direct (Move-only, negotiated) reverse via
  // PROPOSE_SWAP/CREATE_POOL. null whenever no pair is currently locked.
  // Another Force Swap (anywhere, any pair) is never blocked by this --
  // only used to proactively grey out/flag the locked pair in the UI.
  protected_pair: { entity_a: string; entity_b: string } | null;
  market: Array<{ entity_id: string; theme_key: string; position: number; display_name: string; ticker_symbol: string; logo_url: string | null }>;
  players: GamePlayerView[];
  proposals: ProposalView[];
  pools: PoolView[];
  holdings?: HoldingView[];
  // Self-only -- present exactly while this player has an active pending
  // Draw/Refresh decision, injected directly into the frozen cached_view
  // at USE_BOOST(draw) time (not computed by the normal projection path,
  // which never runs again until the decision resolves). Reuses the old
  // pending-pickup frozen-view pattern under a new name.
  pending_boost_draw?: {
    pending_boost_draw_id: string;
    revealed_entity_id: string;
    decision_deadline_at: string;
  };
  // Present once phase is SCORED -- compute_final_scores' result, merged
  // in by project(). See the Haircut-risk design writeup.
  realized_haircut_depth?: number;
  wiped_entity_ids?: string[];
  results?: Array<{ game_player_id: string; final_value: number }>;
  winners?: string[];
}

// SCORED/CANCELLED are terminal -- there's no more live state to chase
// once a game reaches either. Polling forever past that point is exactly
// what let an abandoned tab on a finished game hammer the backend at 1Hz
// indefinitely (a real incident, not a hypothetical -- see the Haircut-risk
// design writeup's production postmortem note).
const _TERMINAL_PHASES = new Set(["SCORED", "CANCELLED"]);
// Backgrounded tabs still get the terminal-phase stop and error backoff,
// just at a slower cadence -- there's no reason to poll a hidden tab as
// aggressively as a visible one, but stopping entirely would mean missing
// the transition that should stop it for good.
const _HIDDEN_INTERVAL_MULTIPLIER = 5;
const _MAX_BACKOFF_MS = 15000;

/** Polls GET /api/games/[id] -- the authoritative view already carries
 * every audience-scoped field (own holdings, ready_to_close, etc.), so
 * there's nothing client-side to merge or reconcile, just replace.
 * `refetch` is exposed directly so callers (submitCommand's `onSettled`)
 * can force an immediate refresh instead of waiting for the next tick.
 *
 * The polling cadence itself follows a small priority order, cheapest
 * case first: a terminal game (SCORED/CANCELLED) stops polling entirely
 * after the one fetch that revealed the terminal phase -- deterministic,
 * not a backoff heuristic, since the client already knows there's nothing
 * left to change. Otherwise: a backgrounded tab polls at
 * intervalMs * _HIDDEN_INTERVAL_MULTIPLIER; consecutive fetch failures
 * back off exponentially (capped); a visible, live, healthy game polls at
 * the plain intervalMs. */
export function useGameView(gameId: string, options: { intervalMs?: number; enabled?: boolean } = {}) {
  const { intervalMs = 1000, enabled = true } = options;
  const [view, setView] = useState<GameView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const consecutiveFailuresRef = useRef(0);
  const terminalRef = useRef(false);

  const refetch = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const response = await fetch(`/api/games/${gameId}`);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : (data.detail?.message ?? "Couldn't load the game."));
      }
      setView(data);
      setError(null);
      consecutiveFailuresRef.current = 0;
      if (_TERMINAL_PHASES.has(data.phase)) terminalRef.current = true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      consecutiveFailuresRef.current += 1;
    } finally {
      inFlight.current = false;
    }
  }, [gameId]);

  useEffect(() => {
    if (!enabled) return;
    terminalRef.current = false;
    consecutiveFailuresRef.current = 0;
    let cancelled = false;

    async function tick() {
      await refetch();
      if (cancelled || terminalRef.current) return; // one final fetch already landed above -- stop the loop for good
      const backoffMs =
        consecutiveFailuresRef.current > 0
          ? Math.min(intervalMs * 2 ** consecutiveFailuresRef.current, _MAX_BACKOFF_MS)
          : intervalMs;
      const delay = document.visibilityState === "hidden" ? backoffMs * _HIDDEN_INTERVAL_MULTIPLIER : backoffMs;
      timeoutRef.current = setTimeout(tick, delay);
    }
    tick();

    function handleVisibilityChange() {
      // Wake up immediately on returning to the tab rather than waiting
      // out whatever throttled delay was scheduled while backgrounded --
      // inFlight/terminalRef both make a redundant concurrent call harmless.
      if (document.visibilityState === "visible" && !terminalRef.current) {
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        tick();
      }
    }
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      cancelled = true;
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [enabled, intervalMs, refetch]);

  return { view, error, refetch };
}
