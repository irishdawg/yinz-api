"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface GamePlayerView {
  game_player_id: string;
  seat: number;
  display_name: string;
  is_golden_name: boolean;
  influence: { available: number; committed: number; spent: number };
  reserve_count_remaining: number;
  ready_to_close?: boolean;
  portfolio_value?: number;
}

export interface GameView {
  game_id: string;
  version: number;
  phase: "LOBBY" | "NEGOTIATION" | "CLOSING" | "SCORED" | "CANCELLED";
  join_code: string | null;
  expected_player_count: number | null;
  host_player_id: string | null;
  you: string | null;
  // Set together at START_GAME -- always non-null once phase is NEGOTIATION
  // or later, always null in LOBBY.
  started_at: string | null;
  max_duration_s: number | null;
  unilateral_cutoff_at: string | null;
  market: Array<{ entity_id: string; theme_key: string; position: number; display_name: string; ticker_symbol: string; logo_url: string | null }>;
  players: GamePlayerView[];
  proposals: unknown[];
  pools: unknown[];
  holdings?: unknown[];
  waterline_entity_id?: string | null;
}

/** Polls GET /api/games/[id] on a fixed interval -- the authoritative
 * view already carries every audience-scoped field (own holdings,
 * ready_to_close, etc.), so there's nothing client-side to merge or
 * reconcile, just replace. `refetch` is exposed directly so callers
 * (submitCommand's `onSettled`) can force an immediate refresh instead of
 * waiting for the next tick. */
export function useGameView(gameId: string, options: { intervalMs?: number; enabled?: boolean } = {}) {
  const { intervalMs = 1000, enabled = true } = options;
  const [view, setView] = useState<GameView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

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
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      inFlight.current = false;
    }
  }, [gameId]);

  useEffect(() => {
    if (!enabled) return;
    // Fetching from an external system (our own API) on mount + interval,
    // not deriving state from props -- the legitimate case the lint rule's
    // own docs carve out.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refetch();
    const id = setInterval(refetch, intervalMs);
    return () => clearInterval(id);
  }, [enabled, intervalMs, refetch]);

  return { view, error, refetch };
}
