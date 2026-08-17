"use client";

import { use, useEffect, useState, type FormEvent } from "react";
import Image from "next/image";
import { useRouter } from "next/navigation";
import { ensureAnonymousSession } from "@/lib/auth";
import { NamePicker } from "@/components/NamePicker";

export default function JoinPage({ params }: { params: Promise<{ code: string }> }) {
  const { code } = use(params);
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [name, setName] = useState<string | null>(null);

  useEffect(() => {
    ensureAnonymousSession()
      .then(() => setReady(true))
      .catch(() => setError("Couldn't start a session. Refresh and try again."));
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!name) {
      setError("Enter your name first.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/games/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ join_code: code, display_name: name }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : (data.detail?.message ?? "Couldn't join the game."));
      }
      router.push(`/game/${data.game_id}?code=${code}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center bg-zinc-50 px-6">
      <main className="w-full max-w-sm">
        <Image src="/gotiate-logo.png" alt="Gotiate" width={120} height={87} className="mb-4" priority />
        <h1 className="mb-2 text-2xl font-semibold text-zinc-900">Join game</h1>
        <p className="mb-8 font-mono text-lg tracking-widest text-zinc-500">{code}</p>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <NamePicker joinCode={code} onNameChange={setName} />
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button
            type="submit"
            disabled={!ready || submitting}
            className="rounded bg-zinc-900 px-4 py-2 text-white disabled:opacity-50"
          >
            {ready ? (submitting ? "Joining…" : "Join game") : "Starting session…"}
          </button>
        </form>
      </main>
    </div>
  );
}
