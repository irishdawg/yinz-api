"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { QRCodeSVG } from "qrcode.react";
import { ensureAnonymousSession } from "@/lib/auth";
import { useGameView } from "@/lib/useGameView";
import { commandErrorMessage, submitCommand } from "@/lib/submitCommand";

export default function GamePage({ params }: { params: Promise<{ id: string }> }) {
  const { id: gameId } = use(params);
  const codeFromUrl = useSearchParams().get("code");

  const [sessionReady, setSessionReady] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);

  useEffect(() => {
    ensureAnonymousSession()
      .then(() => setSessionReady(true))
      .catch(() => setSessionError("Couldn't start a session. Refresh and try again."));
  }, []);

  const { view, error, refetch } = useGameView(gameId, { enabled: sessionReady });

  if (sessionError || error) {
    return <CenteredMessage showHomeLink>{sessionError ?? error}</CenteredMessage>;
  }
  if (!view) {
    return <CenteredMessage>Loading…</CenteredMessage>;
  }
  const isHost = view.you !== null && view.you === view.host_player_id;

  if (view.phase === "CANCELLED") {
    return <CenteredMessage showHomeLink>This game was cancelled.</CenteredMessage>;
  }
  if (view.phase === "NEGOTIATION") {
    return <MarketView gameId={gameId} view={view} isHost={isHost} onChanged={refetch} />;
  }
  if (view.phase !== "LOBBY") {
    // Stages 6-8 (close/scoring/replay) aren't built yet -- this keeps the
    // flow from dead-ending once the market closes, without pretending
    // there's a results screen here.
    return (
      <CenteredMessage showHomeLink>Game is live (phase: {view.phase}). The close/scoring screen is coming in a later build.</CenteredMessage>
    );
  }

  const joinCode = view.join_code ?? codeFromUrl;

  return (
    <LobbyRoom gameId={gameId} view={view} joinCode={joinCode} isHost={isHost} onChanged={refetch} />
  );
}

function formatCountdown(startedAt: string | null, maxDurationS: number | null): string {
  if (!startedAt || maxDurationS === null) return "--:--";
  const elapsedS = (Date.now() - new Date(startedAt).getTime()) / 1000;
  const remainingS = Math.max(0, Math.round(maxDurationS - elapsedS));
  const minutes = Math.floor(remainingS / 60);
  const seconds = remainingS % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

/** Host-only, any pre-SCORED phase -- the one escape hatch out of the
 * one-active-game-per-host rule short of playing a game all the way
 * through. Confirmed with a native dialog since it ends the game for
 * every seated player, not just the host. */
function CancelGameButton({ gameId, version, onChanged }: { gameId: string; version: number; onChanged: () => void }) {
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleCancel() {
    if (!window.confirm("Cancel this game for everyone?")) return;
    setError(null);
    setCancelling(true);
    const result = await submitCommand(gameId, "CANCEL_GAME", {}, { expectedVersion: version, onSettled: onChanged });
    setCancelling(false);
    if (!result.ok) setError(commandErrorMessage(result.data, "Couldn't cancel the game."));
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button type="button" onClick={handleCancel} disabled={cancelling} className="text-sm text-red-600 underline disabled:opacity-50">
        {cancelling ? "Cancelling…" : "Cancel game"}
      </button>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}

function MarketView({
  gameId,
  view,
  isHost,
  onChanged,
}: {
  gameId: string;
  view: import("@/lib/useGameView").GameView;
  isHost: boolean;
  onChanged: () => void;
}) {
  const self = view.players.find((p) => p.game_player_id === view.you);

  return (
    <div className="flex flex-1 flex-col gap-6 bg-zinc-50 px-4 py-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-zinc-900">Market</h1>
        <span className="font-mono text-2xl font-bold tabular-nums text-zinc-900">{formatCountdown(view.started_at, view.max_duration_s)}</span>
      </div>
      {isHost && <CancelGameButton gameId={gameId} version={view.version} onChanged={onChanged} />}

      {self && (
        <div className="flex gap-4 rounded border border-zinc-200 bg-white p-3 text-sm">
          <div>
            <div className="text-xs font-medium text-zinc-500">Influence</div>
            <div className="tabular-nums text-zinc-900">
              {self.influence.available} avail &middot; {self.influence.committed} committed &middot; {self.influence.spent} spent
            </div>
          </div>
          <div>
            <div className="text-xs font-medium text-zinc-500">Portfolio value</div>
            <div className="tabular-nums text-zinc-900">{self.portfolio_value ?? "—"}</div>
          </div>
        </div>
      )}

      <div>
        <h2 className="mb-2 text-sm font-medium text-zinc-700">Scale ({view.market.length})</h2>
        <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-2">
          {view.market.map((entity) => (
            <div
              key={entity.entity_id}
              className="flex w-24 flex-shrink-0 flex-col items-center gap-1 rounded border border-zinc-200 bg-white p-2 text-center"
            >
              <span className="text-xs text-zinc-400">{entity.position}</span>
              <span className="font-mono text-sm font-bold text-zinc-900">{entity.ticker_symbol}</span>
              <span className="text-xs leading-tight text-zinc-600">{entity.display_name}</span>
            </div>
          ))}
        </div>
      </div>

      <div>
        <h2 className="mb-2 text-sm font-medium text-zinc-700">Players</h2>
        <ul className="flex flex-col gap-1 rounded border border-zinc-200 bg-white p-3">
          {view.players.map((p) => (
            <li key={p.game_player_id} className="flex items-center justify-between text-sm text-zinc-900">
              <span>
                <span className={p.is_golden_name ? "font-medium text-amber-700" : undefined}>{p.display_name}</span>
                {p.game_player_id === view.you && <span className="ml-2 text-xs font-medium text-zinc-500">YOU</span>}
              </span>
              <span className="tabular-nums text-zinc-500">{p.reserve_count_remaining} reserve</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function CenteredMessage({ children, showHomeLink = false }: { children: React.ReactNode; showHomeLink?: boolean }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 bg-zinc-50 px-6">
      <p className="text-center text-zinc-600">{children}</p>
      {showHomeLink && (
        <Link href="/" className="text-sm font-medium text-zinc-700 underline">
          Return to start
        </Link>
      )}
    </div>
  );
}

function LobbyRoom({
  gameId,
  view,
  joinCode,
  isHost,
  onChanged,
}: {
  gameId: string;
  view: import("@/lib/useGameView").GameView;
  joinCode: string | null;
  isHost: boolean;
  onChanged: () => void;
}) {
  const [expectedInput, setExpectedInput] = useState(view.expected_player_count?.toString() ?? "");
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [updatingCount, setUpdatingCount] = useState(false);

  const joinUrl = joinCode ? `${process.env.NEXT_PUBLIC_SITE_URL ?? ""}/join/${joinCode}` : null;
  const playerCount = view.players.length;
  const canStart = playerCount >= 2 && playerCount <= 6;

  async function handleUpdateExpectedCount(event: React.FormEvent) {
    event.preventDefault();
    setActionError(null);
    setUpdatingCount(true);
    const result = await submitCommand(
      gameId,
      "SET_EXPECTED_PLAYER_COUNT",
      { expected_player_count: expectedInput ? Number(expectedInput) : null },
      { expectedVersion: view.version, onSettled: onChanged },
    );
    setUpdatingCount(false);
    if (!result.ok) setActionError(commandErrorMessage(result.data, "Couldn't update expected player count."));
  }

  async function handleStart() {
    setActionError(null);
    setStarting(true);
    const result = await submitCommand(gameId, "START_GAME", {}, { expectedVersion: view.version, onSettled: onChanged });
    setStarting(false);
    if (!result.ok) setActionError(commandErrorMessage(result.data, "Couldn't start the game."));
  }

  return (
    <div className="flex flex-1 flex-col items-center gap-8 bg-zinc-50 px-6 py-10">
      <div className="flex flex-col items-center gap-4">
        <h1 className="text-2xl font-semibold text-zinc-900">Waiting for players</h1>
        {joinCode && (
          <>
            <p className="font-mono text-4xl font-bold tracking-widest text-zinc-900">{joinCode}</p>
            {joinUrl && (
              <div className="rounded bg-white p-4 shadow">
                <QRCodeSVG value={joinUrl} size={160} />
              </div>
            )}
          </>
        )}
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-2 text-sm font-medium text-zinc-700">
          Players ({playerCount}
          {view.expected_player_count ? ` of ${view.expected_player_count}` : ""})
        </h2>
        <ul className="flex flex-col gap-1 rounded border border-zinc-200 bg-white p-3">
          {view.players.map((p) => (
            <li key={p.game_player_id} className="flex items-center justify-between text-zinc-900">
              <span>
                <span className={p.is_golden_name ? "font-medium text-amber-700" : undefined}>{p.display_name}</span>
                {p.is_golden_name && (
                  <span className="ml-2 rounded-full bg-amber-400 px-2 py-0.5 text-xs font-bold text-white">GOLDEN</span>
                )}
                {p.game_player_id === view.host_player_id && <span className="ml-2 text-xs font-medium text-zinc-500">HOST</span>}
                {p.game_player_id === view.you && <span className="ml-2 text-xs font-medium text-zinc-500">YOU</span>}
              </span>
            </li>
          ))}
        </ul>
      </div>

      {isHost && (
        <div className="flex w-full max-w-sm flex-col gap-4">
          <form onSubmit={handleUpdateExpectedCount} className="flex items-end gap-2">
            <label className="flex flex-1 flex-col gap-1">
              <span className="text-sm font-medium text-zinc-700">Expected players (optional)</span>
              <input
                type="number"
                min={2}
                max={6}
                value={expectedInput}
                onChange={(event) => setExpectedInput(event.target.value)}
                className="rounded border border-zinc-300 px-3 py-2 text-zinc-900"
              />
            </label>
            <button type="submit" disabled={updatingCount} className="rounded border border-zinc-300 px-3 py-2 text-sm text-zinc-700 disabled:opacity-50">
              Update
            </button>
          </form>

          <button
            type="button"
            onClick={handleStart}
            disabled={!canStart || starting}
            className="rounded bg-zinc-900 px-4 py-2 text-white disabled:opacity-50"
          >
            {starting ? "Starting…" : canStart ? "Start game" : "Need 2-6 players"}
          </button>

          <CancelGameButton gameId={gameId} version={view.version} onChanged={onChanged} />
        </div>
      )}

      {actionError && <p className="text-sm text-red-600">{actionError}</p>}
    </div>
  );
}
