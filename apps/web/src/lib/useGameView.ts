"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface GamePlayerView {
  game_player_id: string;
  seat: number;
  display_name: string;
  is_golden_name: boolean;
  // Self-only -- Influence is a private tax on beneficial actions, not
  // public information (see the private Influence economy design). Never
  // present for any other player's roster entry.
  influence?: { available: number; committed: number; spent: number };
  reserve_count_remaining: number;
  ready_to_close?: boolean;
  // Self-only, same reasoning as influence -- see the Haircut-risk design
  // writeup. projected_value is the unconditional linear-rank sum (what
  // you'd score if nothing were wiped); safe_value only appears once the
  // profile is revealed (or the game is scored), and only covers positions
  // that cannot possibly be wiped.
  projected_value?: number;
  safe_value?: number;
}

export interface HoldingView {
  holding_id: string;
  // null when this holding's identity hasn't been revealed to its owner
  // yet (an unrevealed or burned-unseen reserve) -- redacted at the
  // projection layer, not something the client ever has to withhold
  // itself. Portfolio holdings are always revealed.
  entity_id: string | null;
  owner_player_id: string;
  zone: "reserve_unrevealed" | "pickup_pending" | "portfolio" | "discarded" | "pickup_surrendered" | "surrendered_unused" | "burned_unseen";
  display_name: string | null;
  ticker_symbol: string | null;
  logo_url: string | null;
}

export interface ProposalView {
  proposal_id: string;
  entity_a: string;
  entity_b: string;
  proposer_id: string;
  status: "open" | "resolved";
  resolution_reason: "executed" | "withdrawn_by_initiator" | "market_closed" | null;
  // Self-only, mutually exclusive: present on your own authored proposal
  // (the liability locked at PROPOSE_SWAP time -- never changes) or, while
  // open, a live preview of what accepting would cost you right now.
  my_influence_liability?: 0 | 1;
  my_accept_liability?: 0 | 1;
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
    | "preempted_by_other_action"
    | "base_proposal_withdrawn"
    | "market_closed"
    | null;
  // Present only when this audience can see the pool's contents (public
  // pool, or an insider) -- see projections._project_pool.
  entity_c?: string;
  entity_d?: string;
  // Same self-only, mutually exclusive pair as ProposalView, but for
  // accepting this pool (see engine._handle_accept_pool's combine rule).
  my_influence_liability?: 0 | 1;
  my_accept_liability?: 0 | 1;
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
  // Set together at START_GAME -- always non-null once phase is NEGOTIATION
  // or later, always null in LOBBY.
  started_at: string | null;
  max_duration_s: number | null;
  unilateral_cutoff_at: string | null;
  // Public once set -- the deadline itself isn't secret, only the
  // profile's contents are until it passes (or the game is scored).
  haircut_reveal_at: string | null;
  haircut_profile: { depth_probabilities: number[] } | null;
  // Public from game start, unlike haircut_profile itself -- every
  // configured profile for this player count shares the same max_depth,
  // so "positions 1..N carry some risk" is structural, not profile
  // -specific. Positions beyond this depth are 100% safe immediately, not
  // just after reveal.
  haircut_risk_band_depth: number | null;
  // Only meaningful once phase is CANCELLED.
  cancellation_reason: "HOST_INITIATED" | "LOBBY_TIMEOUT" | null;
  market: Array<{ entity_id: string; theme_key: string; position: number; display_name: string; ticker_symbol: string; logo_url: string | null }>;
  players: GamePlayerView[];
  proposals: ProposalView[];
  pools: PoolView[];
  holdings?: HoldingView[];
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
