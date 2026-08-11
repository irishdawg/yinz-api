"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ensureAnonymousSession } from "@/lib/auth";
import { usePlayerName } from "@/lib/usePlayerName";

export default function Home() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [expectedPlayerCount, setExpectedPlayerCount] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const { name, isGolden, loading: nameLoading, error: nameError, requestInitial, reroll } = usePlayerName();

  useEffect(() => {
    ensureAnonymousSession()
      .then(() => {
        setReady(true);
        requestInitial();
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
    try {
      const response = await fetch("/api/games", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_name: name,
          expected_player_count: expectedPlayerCount ? Number(expectedPlayerCount) : undefined,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : (data.detail?.message ?? "Couldn't create the game."));
      }
      router.push(`/game/${data.game_id}?code=${data.join_code}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center bg-zinc-50 px-6">
      <main className="w-full max-w-sm">
        <h1 className="mb-8 text-2xl font-semibold text-zinc-900">Gotiate</h1>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium text-zinc-700">Your name</span>
            <div className={`flex items-center justify-between rounded border px-3 py-2 ${isGolden ? "border-amber-400 bg-amber-50" : "border-zinc-300 bg-white"}`}>
              <span data-testid="assigned-name" className={`font-medium ${isGolden ? "text-amber-700" : "text-zinc-900"}`}>
                {nameLoading ? "…" : (name ?? "—")}
                {isGolden && !nameLoading && (
                  <span className="ml-2 rounded-full bg-amber-400 px-2 py-0.5 text-xs font-bold text-white">GOLDEN</span>
                )}
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
          <label className="flex flex-col gap-1">
            <span className="text-sm font-medium text-zinc-700">Expected players (optional)</span>
            <input
              type="number"
              min={2}
              max={6}
              value={expectedPlayerCount}
              onChange={(event) => setExpectedPlayerCount(event.target.value)}
              className="rounded border border-zinc-300 px-3 py-2 text-zinc-900"
            />
          </label>
          {(error || nameError) && <p className="text-sm text-red-600">{error ?? nameError}</p>}
          <button
            type="submit"
            disabled={!ready || !name || submitting}
            className="rounded bg-zinc-900 px-4 py-2 text-white disabled:opacity-50"
          >
            {ready ? (submitting ? "Creating…" : "Create game") : "Starting session…"}
          </button>
        </form>
      </main>
    </div>
  );
}
