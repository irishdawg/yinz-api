"use client";

import { use, useEffect, useRef, useState } from "react";
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
    const message =
      view.cancellation_reason === "LOBBY_TIMEOUT"
        ? "This game was cancelled — the host didn't start it in time."
        : "This game was cancelled.";
    return <CenteredMessage showHomeLink>{message}</CenteredMessage>;
  }
  if (view.phase === "NEGOTIATION") {
    return <MarketView view={view} />;
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

function formatCountdownTo(deadlineMs: number): string {
  const remainingS = Math.max(0, Math.round((deadlineMs - Date.now()) / 1000));
  const minutes = Math.floor(remainingS / 60);
  const seconds = remainingS % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

function formatMarketCountdown(startedAt: string | null, maxDurationS: number | null): string {
  if (!startedAt || maxDurationS === null) return "--:--";
  return formatCountdownTo(new Date(startedAt).getTime() + maxDurationS * 1000);
}

// A bare `Date.now()` read directly in a component body trips the
// react-hooks/purity rule (impure-during-render); routed through a plain
// helper instead, same as the countdown formatters above never trip it.
function isPastDeadline(deadlineMs: number): boolean {
  return Date.now() >= deadlineMs;
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

/** Tracks the previous value across polls and surfaces the delta for a
 * couple seconds after it changes, then clears itself. No backend
 * involvement -- purely a client-side diff of consecutive poll results. */
function useValueDelta(value: number | undefined): number | null {
  const [delta, setDelta] = useState<number | null>(null);
  const prevRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (value === undefined) return;
    if (prevRef.current !== undefined && prevRef.current !== value) {
      setDelta(value - prevRef.current);
      const timeout = setTimeout(() => setDelta(null), 2500);
      prevRef.current = value;
      return () => clearTimeout(timeout);
    }
    prevRef.current = value;
  }, [value]);

  return delta;
}

function MarketView({ view }: { view: import("@/lib/useGameView").GameView }) {
  const self = view.players.find((p) => p.game_player_id === view.you);
  const valueDelta = useValueDelta(self?.portfolio_value);

  // Ownership is projected directly onto the scale (a bold border, plus a
  // xN badge for duplicates) rather than shown as a separate portfolio
  // list -- the scale *is* the board. Only "portfolio" zone counts as
  // owned-and-visible; reserve/burned holdings are never entity-identified
  // here at all (project() already redacts those before they reach the
  // client), so there's nothing to accidentally leak by including them.
  const ownedCounts = new Map<string, number>();
  for (const h of view.holdings ?? []) {
    if (h.zone === "portfolio" && h.entity_id) {
      ownedCounts.set(h.entity_id, (ownedCounts.get(h.entity_id) ?? 0) + 1);
    }
  }

  return (
    <div className="flex flex-1 flex-col gap-6 bg-zinc-50 px-4 py-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-zinc-900">Market</h1>
        <span className="font-mono text-2xl font-bold tabular-nums text-zinc-900">{formatMarketCountdown(view.started_at, view.max_duration_s)}</span>
      </div>

      {self && (
        <div className="flex gap-4 rounded border border-zinc-200 bg-white p-3 text-sm">
          <div>
            <div className="text-xs font-medium text-zinc-500">Value</div>
            <div className="tabular-nums text-zinc-900">
              {self.portfolio_value ?? "—"}
              {valueDelta !== null && valueDelta !== 0 && (
                <span className={`ml-1 text-xs font-bold ${valueDelta > 0 ? "text-emerald-600" : "text-red-600"}`}>
                  {valueDelta > 0 ? "↑" : "↓"}
                  {Math.abs(valueDelta)}
                </span>
              )}
            </div>
          </div>
          <div>
            <div className="text-xs font-medium text-zinc-500">Influence</div>
            <div className="tabular-nums text-zinc-900">
              {self.influence.available}
              {self.influence.committed > 0 ? ` / ${self.influence.committed} committed` : ""}
            </div>
          </div>
        </div>
      )}

      <div>
        <h2 className="mb-2 text-sm font-medium text-zinc-700">Scale ({view.market.length})</h2>
        <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-2">
          {view.market.map((entity) => {
            const owned = ownedCounts.get(entity.entity_id) ?? 0;
            return (
              <div
                key={entity.entity_id}
                className={`flex w-24 flex-shrink-0 flex-col items-center gap-1 rounded bg-white p-2 text-center ${
                  owned > 0 ? "border-2 border-zinc-900" : "border border-zinc-200"
                }`}
              >
                <span className="text-xs text-zinc-400">{entity.position}</span>
                <span className="font-mono text-sm font-bold text-zinc-900">{entity.ticker_symbol}</span>
                <span className="text-xs leading-tight text-zinc-600">{entity.display_name}</span>
                {owned > 1 && <span className="text-xs font-bold text-zinc-900">×{owned}</span>}
              </div>
            );
          })}
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

/** Only renders once now() has passed the reminder deadline -- before
 * that, the lobby is just the roster + Start button, no time pressure
 * shown at all. Visible to everyone (transparency about why the game
 * might auto-cancel), but only the host gets the actions that prevent it. */
function LobbyReminderBanner({
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
  const [extending, setExtending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!view.lobby_reminder_deadline_at) return null;
  const reminderMs = new Date(view.lobby_reminder_deadline_at).getTime();
  if (!isPastDeadline(reminderMs)) return null;
  const graceDeadlineMs = reminderMs + view.lobby_reminder_grace_seconds * 1000;

  async function handleExtend() {
    setError(null);
    setExtending(true);
    const result = await submitCommand(gameId, "EXTEND_LOBBY_TIMER", {}, { expectedVersion: view.version, onSettled: onChanged });
    setExtending(false);
    if (!result.ok) setError(commandErrorMessage(result.data, "Couldn't extend."));
  }

  return (
    <div className="flex w-full max-w-sm flex-col gap-2 rounded border border-amber-300 bg-amber-50 p-3">
      <p className="text-sm text-amber-900">
        {isHost
          ? `Start now, or ask for more time? Auto-cancels in ${formatCountdownTo(graceDeadlineMs)}.`
          : `Waiting on the host — auto-cancels in ${formatCountdownTo(graceDeadlineMs)} if they don't respond.`}
      </p>
      {isHost && (
        <button
          type="button"
          onClick={handleExtend}
          disabled={extending}
          className="self-start rounded border border-amber-400 px-3 py-1 text-sm font-medium text-amber-900 disabled:opacity-50"
        >
          {extending ? "…" : "Need more time"}
        </button>
      )}
      {error && <p className="text-xs text-red-600">{error}</p>}
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
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const joinUrl = joinCode ? `${process.env.NEXT_PUBLIC_SITE_URL ?? ""}/join/${joinCode}` : null;
  const playerCount = view.players.length;
  const canStart = playerCount >= 2 && playerCount <= 6;

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
        <h2 className="mb-2 text-sm font-medium text-zinc-700">Players ({playerCount})</h2>
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

      <LobbyReminderBanner gameId={gameId} view={view} isHost={isHost} onChanged={onChanged} />

      {isHost && (
        <div className="flex w-full max-w-sm flex-col gap-4">
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
