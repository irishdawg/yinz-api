"use client";

import { useCallback, useState } from "react";

interface PlayerNameState {
  name: string | null;
  loading: boolean;
  error: string | null;
}

/** No free-text display names anywhere in the UI -- this is the only way
 * a component gets a name to show. `joinCode`, when set, makes every
 * request exclude names already held by players seated in that game (see
 * apps/web/src/app/api/player-names/route.ts). Never golden -- gold is
 * rolled server-side at actual create/join time, not at preview time, so
 * this hook has nothing golden to report; check a roster entry's
 * `is_golden_name` after joining instead. */
export function usePlayerName(joinCode?: string) {
  const [state, setState] = useState<PlayerNameState>({ name: null, loading: false, error: null });

  const fetchName = useCallback(
    async (exclude?: string) => {
      setState((s) => ({ ...s, loading: true, error: null }));
      try {
        const params = new URLSearchParams();
        if (exclude) params.set("exclude", exclude);
        if (joinCode) params.set("join_code", joinCode);
        const response = await fetch(`/api/player-names?${params.toString()}`);
        const data = await response.json();
        if (!response.ok) {
          throw new Error(typeof data.detail === "string" ? data.detail : (data.detail?.message ?? "Couldn't get a name."));
        }
        setState({ name: data.name, loading: false, error: null });
      } catch (err) {
        setState((s) => ({ ...s, loading: false, error: err instanceof Error ? err.message : "Something went wrong." }));
      }
    },
    [joinCode],
  );

  const requestInitial = useCallback(() => fetchName(), [fetchName]);
  const reroll = useCallback(() => {
    if (state.name) fetchName(state.name);
  }, [fetchName, state.name]);

  return { ...state, requestInitial, reroll };
}
