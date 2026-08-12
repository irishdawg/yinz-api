"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface EventView {
  seq_no: number;
  type: string;
  actor_game_player_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

/** Polls GET /api/games/[id]/events, accumulating incrementally via
 * since_seq rather than re-fetching the whole ledger every tick -- a
 * negotiation-heavy game can accumulate a lot of events over an 8-16
 * minute window. Visibility is already fully resolved server-side
 * (project_events) by the time this ever sees a payload. */
export function useGameEvents(gameId: string, options: { intervalMs?: number; enabled?: boolean } = {}) {
  const { intervalMs = 1000, enabled = true } = options;
  const [events, setEvents] = useState<EventView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const maxSeqRef = useRef(0);
  const inFlight = useRef(false);

  const refetch = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const response = await fetch(`/api/games/${gameId}/events?since_seq=${maxSeqRef.current + 1}`);
      const data: EventView[] = await response.json();
      if (!response.ok) {
        throw new Error("Couldn't load activity.");
      }
      if (data.length > 0) {
        setEvents((prev) => [...prev, ...data]);
        maxSeqRef.current = Math.max(maxSeqRef.current, ...data.map((e) => e.seq_no));
      }
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      inFlight.current = false;
    }
  }, [gameId]);

  useEffect(() => {
    if (!enabled) return;
    refetch();
    const id = setInterval(refetch, intervalMs);
    return () => clearInterval(id);
  }, [enabled, intervalMs, refetch]);

  return { events, error };
}
