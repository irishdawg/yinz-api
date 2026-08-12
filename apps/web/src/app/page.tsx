"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ensureAnonymousSession } from "@/lib/auth";
import { usePlayerName } from "@/lib/usePlayerName";

interface ThemeSetSummary {
  theme_set_id: string;
  name: string;
}

export default function Home() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [existingGameId, setExistingGameId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const { name, loading: nameLoading, error: nameError, requestInitial, reroll } = usePlayerName();
  const [themeSets, setThemeSets] = useState<ThemeSetSummary[]>([]);
  const [themeSetId, setThemeSetId] = useState("");
  const [joinCodeInput, setJoinCodeInput] = useState("");
  const [joining, setJoining] = useState(false);
  const [joinError, setJoinError] = useState<string | null>(null);

  useEffect(() => {
    ensureAnonymousSession()
      .then(() => {
        setReady(true);
        requestInitial();
        fetch("/api/theme-sets")
          .then((response) => response.json())
          .then((data: ThemeSetSummary[]) => {
            setThemeSets(data);
            if (data.length > 0) setThemeSetId(data[0].theme_set_id);
          })
          .catch(() => {
            // Non-fatal -- create_game just falls back to the server's default theme set.
          });
      })
      .catch(() => setError("Couldn't start a session. Refresh and try again."));
    // requestInitial is stable (no joinCode on this page) -- fine to omit re-runs on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!name) return;
    setSubmitting(true);
    setError(null);
    setExistingGameId(null);
    try {
      const response = await fetch("/api/games", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_name: name,
          theme_set_id: themeSetId || undefined,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        // active_game_exists carries the existing game's id -- surface a
        // way back into it instead of just a dead-end error string, since
        // there's nothing else on this page that could tell you it exists.
        if (data.detail?.error_code === "active_game_exists" && data.detail?.game_id) {
          setExistingGameId(data.detail.game_id);
        }
        throw new Error(typeof data.detail === "string" ? data.detail : (data.detail?.message ?? "Couldn't create the game."));
      }
      router.push(`/game/${data.game_id}?code=${data.join_code}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleJoinByCode() {
    const code = joinCodeInput.trim().toUpperCase();
    if (!code || !name) return;
    setJoining(true);
    setJoinError(null);
    try {
      const response = await fetch("/api/games/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ join_code: code, display_name: name }),
      });
      const data = await response.json();
      if (!response.ok) {
        // The name shown here was drawn from the unscoped global pool, not
        // excluding this specific game's roster (that only happens once a
        // code is known) -- a collision is rare but real. Reroll and let
        // them try again with one more click, instead of a dead-end error.
        if (data.detail?.error_code === "name_taken") {
          await reroll();
          throw new Error("That name's already taken in this game -- got you a new one, try again.");
        }
        throw new Error(typeof data.detail === "string" ? data.detail : (data.detail?.message ?? "Couldn't join that game."));
      }
      router.push(`/game/${data.game_id}?code=${code}`);
    } catch (err) {
      setJoinError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setJoining(false);
    }
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center bg-zinc-50 px-6">
      <main className="w-full max-w-sm">
        <h1 className="mb-8 text-2xl font-semibold text-zinc-900">Gotiate</h1>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium text-zinc-700">Your name</span>
            <div className="flex items-center justify-between rounded border border-zinc-300 bg-white px-3 py-2">
              <span data-testid="assigned-name" className="font-medium text-zinc-900">
                {nameLoading ? "…" : (name ?? "—")}
              </span>
              <button
                type="button"
                onClick={reroll}
                disabled={!name || nameLoading}
                className="text-sm font-medium text-zinc-600 underline disabled:opacity-50"
              >
                Change name
              </button>
            </div>
          </div>
          {themeSets.length > 0 && (
            <label className="flex flex-col gap-1">
              <span className="text-sm font-medium text-zinc-700">Theme</span>
              <select
                value={themeSetId}
                onChange={(event) => setThemeSetId(event.target.value)}
                className="rounded border border-zinc-300 bg-white px-3 py-2 text-zinc-900"
              >
                {themeSets.map((t) => (
                  <option key={t.theme_set_id} value={t.theme_set_id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {(error || nameError) && <p className="text-sm text-red-600">{error ?? nameError}</p>}
          {existingGameId && (
            <button
              type="button"
              onClick={() => router.push(`/game/${existingGameId}`)}
              className="text-sm font-medium text-zinc-700 underline"
            >
              Go to your existing game
            </button>
          )}
          <button
            type="submit"
            disabled={!ready || !name || submitting}
            className="rounded bg-zinc-900 px-4 py-2 text-white disabled:opacity-50"
          >
            {ready ? (submitting ? "Creating…" : "Create game") : "Starting session…"}
          </button>
        </form>

        <div className="mt-6 flex flex-col gap-2 border-t border-zinc-200 pt-6">
          <span className="text-sm font-medium text-zinc-700">Have a code?</span>
          <div className="flex gap-2">
            <input
              type="text"
              placeholder="Join code"
              value={joinCodeInput}
              onChange={(event) => setJoinCodeInput(event.target.value)}
              maxLength={7}
              className="flex-1 rounded border border-zinc-300 px-3 py-2 uppercase text-zinc-900 placeholder:normal-case placeholder:text-zinc-400"
            />
            <button
              type="button"
              onClick={handleJoinByCode}
              disabled={!joinCodeInput.trim() || !name || joining}
              className="rounded border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-700 disabled:opacity-50"
            >
              {joining ? "Joining…" : "Join game"}
            </button>
          </div>
          {joinError && <p className="text-sm text-red-600">{joinError}</p>}
        </div>
      </main>
    </div>
  );
}
