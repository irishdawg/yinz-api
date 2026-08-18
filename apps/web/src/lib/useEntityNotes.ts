"use client";

import { useCallback, useEffect, useState } from "react";

/** Private per-player "who does this remind me of" scratchpad -- a market
 * card right-clicked (or long-pressed on touch) offers a list of seated
 * players' names; picking one sticks that name on the card until changed
 * or cleared. Purely a personal memory aid, never sent to the server:
 * localStorage only, scoped to this browser, this game. Keyed by entity_id
 * -> game_player_id (not display_name) so a rename mid-game -- doesn't
 * happen today, but names are read fresh at render time regardless --
 * can't orphan a note. */
function storageKey(gameId: string): string {
  return `gotiate:entity-notes:${gameId}`;
}

function readNotes(gameId: string): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(storageKey(gameId));
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function useEntityNotes(gameId: string) {
  const [notes, setNotes] = useState<Record<string, string>>({});

  useEffect(() => {
    // Querying an external system (localStorage, a browser-only API) on
    // mount, not deriving state from props -- only set post-mount to
    // avoid an SSR/client render mismatch, same precedent as
    // ShareLinkButton's effect in page.tsx.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNotes(readNotes(gameId));
  }, [gameId]);

  const setNote = useCallback(
    (entityId: string, playerId: string | null) => {
      setNotes((prev) => {
        const next = { ...prev };
        if (playerId) next[entityId] = playerId;
        else delete next[entityId];
        try {
          window.localStorage.setItem(storageKey(gameId), JSON.stringify(next));
        } catch {
          // Storage full/unavailable (private browsing, etc.) -- the note
          // just won't persist this session; not worth surfacing an error
          // for a purely cosmetic memory aid.
        }
        return next;
      });
    },
    [gameId],
  );

  return { notes, setNote };
}
