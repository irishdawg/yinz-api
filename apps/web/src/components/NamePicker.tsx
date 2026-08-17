"use client";

import { useEffect, useState } from "react";
import { usePlayerName } from "@/lib/usePlayerName";

// Mirrors the server's own cap in routes.py's _resolve_submitted_name --
// this is a UX guardrail (stops mid-typing overflow), not the source of
// truth; the server still enforces it independently.
const _MAX_TYPED_NAME_LENGTH = 24;

/** Typed names are now primary -- in-person play wants real/familiar
 * names, not "wait, who's Mortia?" -- with the curated catalog kept as an
 * explicit fallback for anonymous play. Shared between the create-game
 * and join-game screens, which previously duplicated this block verbatim.
 * Reports the name to submit (or null while there's nothing valid yet) up
 * to the parent; never validates content itself beyond a client-side
 * length cap -- the server is still the only place that decides what's
 * actually accepted (see AGENTS.md's display-name note). */
export function NamePicker({ joinCode, onNameChange }: { joinCode?: string; onNameChange: (name: string | null) => void }) {
  const [mode, setMode] = useState<"typed" | "random">("typed");
  const [typedName, setTypedName] = useState("");
  const { name: randomName, loading: randomLoading, error: randomError, requestInitial, reroll } = usePlayerName(joinCode);

  // Lazy, not eager: a random name is only ever fetched (and only ever
  // marks a catalog name's usage server-side once actually submitted) if
  // the player actively switches to this fallback mode.
  useEffect(() => {
    if (mode === "random" && randomName === null && !randomLoading) requestInitial();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  useEffect(() => {
    onNameChange(mode === "typed" ? typedName.trim() || null : randomName);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, typedName, randomName]);

  if (mode === "random") {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-sm font-medium text-zinc-700">Your name</span>
        <div className="flex items-center justify-between rounded border border-zinc-300 bg-white px-3 py-2">
          <span data-testid="assigned-name" className="font-medium text-zinc-900">
            {randomLoading ? "…" : (randomName ?? "—")}
          </span>
          <button
            type="button"
            onClick={reroll}
            disabled={!randomName || randomLoading}
            className="text-sm font-medium text-zinc-600 underline disabled:opacity-50"
          >
            Change name
          </button>
        </div>
        <button type="button" onClick={() => setMode("typed")} className="self-start text-xs font-medium text-zinc-500 underline">
          Type your own name instead
        </button>
        {randomError && <p className="text-xs text-red-600">{randomError}</p>}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <span className="text-sm font-medium text-zinc-700">Your name</span>
      <input
        type="text"
        value={typedName}
        onChange={(event) => setTypedName(event.target.value)}
        maxLength={_MAX_TYPED_NAME_LENGTH}
        placeholder="Type your name"
        data-testid="assigned-name"
        className="rounded border border-zinc-300 bg-white px-3 py-2 text-zinc-900 placeholder:text-zinc-400"
      />
      <button type="button" onClick={() => setMode("random")} className="self-start text-xs font-medium text-zinc-500 underline">
        Use a random name instead
      </button>
    </div>
  );
}
