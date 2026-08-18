"use client";

import { useCallback, useEffect, useState } from "react";

/** Private per-player "who does this remind me of" scratchpad -- a market
 * card right-clicked (or long-pressed on touch) offers a list of seated
 * players' names; picking one sticks that name on the card, up to two per
 * card, until toggled off or cleared. Purely a personal memory aid, never
 * sent to the server: localStorage only, scoped to this browser, this
 * game. Keyed by entity_id -> game_player_id[] (not display_name) so a
 * rename mid-game -- doesn't happen today, but names are read fresh at
 * render time regardless -- can't orphan a note. */
const _MAX_NOTES_PER_ENTITY = 2;

function storageKey(gameId: string): string {
  return `gotiate:entity-notes:${gameId}`;
}

function readNotes(gameId: string): Record<string, string[]> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(storageKey(gameId));
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function useEntityNotes(gameId: string) {
  const [notes, setNotes] = useState<Record<string, string[]>>({});

  useEffect(() => {
    // Querying an external system (localStorage, a browser-only API) on
    // mount, not deriving state from props -- only set post-mount to
    // avoid an SSR/client render mismatch, same precedent as
    // ShareLinkButton's effect in page.tsx.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNotes(readNotes(gameId));
  }, [gameId]);

  const persist = useCallback(
    (next: Record<string, string[]>) => {
      try {
        window.localStorage.setItem(storageKey(gameId), JSON.stringify(next));
      } catch {
        // Storage full/unavailable (private browsing, etc.) -- the note
        // just won't persist this session; not worth surfacing an error
        // for a purely cosmetic memory aid.
      }
    },
    [gameId],
  );

  // Toggle, not set-or-clear -- picking an already-tagged name removes
  // it, picking a new one adds it (silently capped at
  // _MAX_NOTES_PER_ENTITY; the menu itself disables further picks once
  // full so this cap is a backstop, not the primary UI).
  const toggleNote = useCallback(
    (entityId: string, playerId: string) => {
      setNotes((prev) => {
        const current = prev[entityId] ?? [];
        const already = current.includes(playerId);
        const updated = already ? current.filter((id) => id !== playerId) : [...current, playerId].slice(0, _MAX_NOTES_PER_ENTITY);
        const next = { ...prev };
        if (updated.length > 0) next[entityId] = updated;
        else delete next[entityId];
        persist(next);
        return next;
      });
    },
    [persist],
  );

  const clearNotes = useCallback(
    (entityId: string) => {
      setNotes((prev) => {
        if (!(entityId in prev)) return prev;
        const next = { ...prev };
        delete next[entityId];
        persist(next);
        return next;
      });
    },
    [persist],
  );

  return { notes, toggleNote, clearNotes, maxNotesPerEntity: _MAX_NOTES_PER_ENTITY };
}
