"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import Image from "next/image";
import { useRouter } from "next/navigation";
import { ensureAnonymousSession } from "@/lib/auth";
import { NamePicker } from "@/components/NamePicker";

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
  const [name, setName] = useState<string | null>(null);
  const [themeSets, setThemeSets] = useState<ThemeSetSummary[]>([]);
  const [themeSetId, setThemeSetId] = useState("");
  const [joinCodeInput, setJoinCodeInput] = useState("");
  const [joining, setJoining] = useState(false);
  const [joinError, setJoinError] = useState<string | null>(null);
  const nameSectionRef = useRef<HTMLDivElement>(null);

  // Typing a name is no longer optional-but-prefilled (see NamePicker) --
  // nothing auto-populates it anymore, so a name-less submit needs to say
  // so and point at the field, not just leave a button looking greyed out
  // for no visible reason (real playtest feedback: the join-code box and
  // the name box are far enough apart on screen that the connection
  // wasn't obvious).
  function scrollToNameSection() {
    nameSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  useEffect(() => {
    ensureAnonymousSession()
      .then(async () => {
        // A returning session with an existing active seat (any phase, not
        // just LOBBY -- e.g. a phone that slept mid-negotiation) routes
        // straight back into that game, before the create/join form ever
        // renders. Checked before setReady/requestInitial so there's no
        // flash of the create form on a session that's about to redirect.
        try {
          const activeResponse = await fetch("/api/games/active");
          if (activeResponse.ok) {
            const active: { game_id: string; join_code: string } = await activeResponse.json();
            router.push(`/game/${active.game_id}?code=${active.join_code}`);
            return;
          }
        } catch {
          // Non-fatal -- fall through to the normal create/join flow.
        }
        setReady(true);
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
  }, [router]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!name) {
      setError("Enter your name first.");
      scrollToNameSection();
      return;
    }
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
    if (!code) return;
    if (!name) {
      setJoinError("Enter your name above first.");
      scrollToNameSection();
      return;
    }
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
        // A collision is more likely now than when every name came from
        // the unscoped global pool -- two people typing "Tim" is a real
        // scenario. NamePicker owns the actual name state either way, so
        // there's nothing to auto-reroll here; just surface it and let
        // them pick a different name (retype, or switch modes) themselves.
        if (data.detail?.error_code === "name_taken") {
          throw new Error("That name's already taken in this game -- try a different one.");
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
        <Image src="/gotiate-logo.png" alt="Gotiate" width={120} height={87} className="mb-8" priority />
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div ref={nameSectionRef}>
            <NamePicker onNameChange={setName} />
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
          {error && <p className="text-sm text-red-600">{error}</p>}
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
            disabled={!ready || submitting}
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
              disabled={!joinCodeInput.trim() || joining}
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
