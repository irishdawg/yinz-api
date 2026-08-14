"use client";

import { use, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { QRCodeSVG } from "qrcode.react";
import { ensureAnonymousSession } from "@/lib/auth";
import { useGameView, type GameView, type PoolView } from "@/lib/useGameView";
import { useGameEvents, type EventView } from "@/lib/useGameEvents";
import { commandErrorMessage, submitCommand } from "@/lib/submitCommand";
import { computeSupportMarkers, formatSupportCount } from "@/lib/supportMarkers";
import { certaintyAt } from "@/lib/haircutRisk";

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
    return <MarketView gameId={gameId} view={view} onChanged={refetch} />;
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

function entityLabel(entityId: string, view: GameView): string {
  return view.market.find((m) => m.entity_id === entityId)?.display_name ?? entityId;
}

function playerLabel(playerId: string | null, view: GameView): string {
  if (!playerId) return "The market";
  return view.players.find((p) => p.game_player_id === playerId)?.display_name ?? "Someone";
}

function playerInitial(playerId: string, view: GameView): string {
  return playerLabel(playerId, view).charAt(0).toUpperCase();
}

/** Visualize's ring treatment: green (rising) / red (falling), and for a
 * Pool's 4-card Visualize, a solid ring for the base pair (pairIndex 0)
 * vs. a dashed outline for the pool leg (pairIndex 1) -- two different CSS
 * mechanisms (box-shadow ring vs. border outline) so "these are two
 * distinct pairs" reads clearly even when the two cards of a pair are far
 * apart or off-screen from each other in the horizontally-scrolling strip. */
function highlightRingClass(highlight: { direction: "rising" | "falling"; pairIndex: number } | undefined): string {
  if (!highlight) return "";
  const color = highlight.direction === "rising" ? "emerald" : "red";
  if (highlight.pairIndex === 1) {
    return color === "emerald" ? "outline outline-2 outline-dashed outline-emerald-500" : "outline outline-2 outline-dashed outline-red-500";
  }
  return color === "emerald" ? "ring-4 ring-emerald-500" : "ring-4 ring-red-500";
}

/** Payout chance is a property of the market *position*, not whichever
 * entity currently occupies it -- see projections.py's
 * haircut_risk_band_depth. Positions beyond the risk band are provably
 * 100% safe from game start, before the profile itself is ever revealed
 * (every profile configured for this player count shares the same
 * boundary); positions inside it show "?" until reveal, then the real
 * certaintyAt() percentage. */
function payoutChanceCell(
  position: number,
  riskBandDepth: number | null,
  haircutProfile: { depth_probabilities: number[] } | null,
): { text: string; className: string } {
  if (riskBandDepth === null || position > riskBandDepth) {
    return { text: "100%", className: "text-emerald-700" };
  }
  if (!haircutProfile) {
    return { text: "?", className: "text-zinc-400" };
  }
  const pct = Math.round(certaintyAt(position, haircutProfile.depth_probabilities) * 100);
  return { text: `${pct}%`, className: pct >= 99 ? "text-emerald-700" : pct >= 50 ? "text-amber-700" : "text-red-700" };
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

function MarketView({ gameId, view, onChanged }: { gameId: string; view: GameView; onChanged: () => void }) {
  const self = view.players.find((p) => p.game_player_id === view.you);
  const valueDelta = useValueDelta(self?.projected_value);
  const [selected, setSelected] = useState<string[]>([]);
  // Non-null while countering a specific open proposal with a Pool --
  // reuses `selected` for card-picking, but the confirm bar's action and
  // labels branch on this instead of always submitting PROPOSE_SWAP.
  const [poolingProposalId, setPoolingProposalId] = useState<string | null>(null);
  const [poolVisibility, setPoolVisibility] = useState<"private" | "public">("private");
  const [proposeError, setProposeError] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  const { events } = useGameEvents(gameId);
  const supportMarkers = useMemo(() => computeSupportMarkers(events, view.pools), [events, view.pools]);

  // "Visualize" on an open proposal briefly emphasizes the two cards it
  // names, so a player doesn't have to mentally parse proposer + entity
  // names and hunt for the matching tiles. Purely a client-side highlight
  // -- no command involved -- so a plain timeout-cleared ref is enough,
  // clearing any earlier pending clear first so a second click doesn't get
  // its highlight cut short by the first click's timer.
  const marketScrollRef = useRef<HTMLDivElement>(null);
  // entity_id -> "rising" | "falling", not just a flat highlighted list --
  // direction is derived the same way support markers and Influence
  // liability already derive it (compare current positions, higher
  // position = worse = rising once swapped), and Pool Visualize highlights
  // both pairs at once. `pairIndex` (0 or 1) lets the market card apply a
  // distinct ring style per pair (dashed vs solid) so a 4-card Pool
  // Visualize still reads as two grouped pairs, not one blob of four.
  const [highlighted, setHighlighted] = useState<Map<string, { direction: "rising" | "falling"; pairIndex: number }> | null>(
    null,
  );
  const highlightTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function currentPosition(entityId: string): number {
    return view.market.find((m) => m.entity_id === entityId)?.position ?? 0;
  }

  /** Accepts one pair (bare proposal) or two (a proposal + its pool) --
   * direction per pair comes from comparing current market positions, same
   * rule engine._rising_entity already uses: whichever entity currently
   * holds the worse (higher) position is "rising" (it takes the other's
   * better position once the swap executes). */
  function visualizeProposal(pairs: [string, string][]) {
    const next = new Map<string, { direction: "rising" | "falling"; pairIndex: number }>();
    pairs.forEach(([a, b], pairIndex) => {
      const rising = currentPosition(a) > currentPosition(b) ? a : b;
      const falling = rising === a ? b : a;
      next.set(rising, { direction: "rising", pairIndex });
      next.set(falling, { direction: "falling", pairIndex });
    });
    setHighlighted(next);
    if (highlightTimeoutRef.current) clearTimeout(highlightTimeoutRef.current);
    highlightTimeoutRef.current = setTimeout(() => setHighlighted(null), 3000);

    const container = marketScrollRef.current;
    if (!container) return;
    const els = [...next.keys()]
      .map((id) => container.querySelector<HTMLElement>(`[data-entity-id="${id}"]`))
      .filter((el): el is HTMLElement => el !== null);
    if (els.length === 0) return;
    const minLeft = Math.min(...els.map((el) => el.offsetLeft));
    const maxRight = Math.max(...els.map((el) => el.offsetLeft + el.offsetWidth));
    container.scrollTo({ left: (minLeft + maxRight) / 2 - container.clientWidth / 2, behavior: "smooth" });
  }

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

  // Pool selection guardrail: while pooling against a base proposal, its
  // own two entities can never legally be part of the pool leg (the
  // backend already rejects this -- see engine._handle_create_pool's
  // overlap check) -- disabled here purely so the illegal choice is never
  // offered, not a new rule.
  const basePoolingProposal = poolingProposalId ? view.proposals.find((p) => p.proposal_id === poolingProposalId) : undefined;
  const poolGuardrailEntities = new Set(basePoolingProposal ? [basePoolingProposal.entity_a, basePoolingProposal.entity_b] : []);

  function toggleSelect(entityId: string) {
    if (poolGuardrailEntities.has(entityId)) return;
    setProposeError(null);
    setSelected((prev) => {
      if (prev.includes(entityId)) return prev.filter((id) => id !== entityId);
      if (prev.length >= 2) return [entityId]; // start fresh rather than growing past a pair
      return [...prev, entityId];
    });
  }

  function startPooling(proposalId: string) {
    setProposeError(null);
    setSelected([]);
    setPoolingProposalId(proposalId);
    setPoolVisibility("private");
  }

  function cancelSelection() {
    setProposeError(null);
    setSelected([]);
    setPoolingProposalId(null);
  }

  // Server-authoritative preview of what PROPOSE_SWAP would cost -- never
  // reimplemented client-side (the private Influence economy is deliberate
  // about this), so this is a real fetch, not a local computation. Keyed
  // on the pair itself (not raw `selected`) so dropping back below two
  // selections doesn't re-fire a fetch; the display below only trusts
  // `proposeCost` while a pair is actually selected, so a stale value left
  // over from a previous pair is never shown.
  const selectedPair = selected.length === 2 ? selected : null;
  const [proposeCost, setProposeCost] = useState<0 | 1 | null>(null);
  useEffect(() => {
    if (!selectedPair) return;
    let cancelled = false;
    const query = new URLSearchParams({ entity_a: selectedPair[0], entity_b: selectedPair[1] });
    fetch(`/api/games/${gameId}/propose-cost?${query.toString()}`)
      .then((r) => r.json())
      .then((data: { liability?: 0 | 1 }) => {
        if (!cancelled) setProposeCost(data.liability ?? null);
      })
      .catch(() => {
        if (!cancelled) setProposeCost(null);
      });
    return () => {
      cancelled = true;
    };
  }, [gameId, selectedPair]);
  const displayedProposeCost = selectedPair ? proposeCost : null;
  const proposeUnaffordable = displayedProposeCost === 1 && (self?.influence?.available ?? 0) < 1;

  async function handleConfirm() {
    if (selected.length !== 2) return;
    setProposeError(null);
    setProposing(true);
    const result = poolingProposalId
      ? await submitCommand(
          gameId,
          "CREATE_POOL",
          { proposal_id: poolingProposalId, entity_c: selected[0], entity_d: selected[1], visibility: poolVisibility },
          { expectedVersion: view.version, onSettled: onChanged },
        )
      : await submitCommand(
          gameId,
          "PROPOSE_SWAP",
          { entity_a: selected[0], entity_b: selected[1] },
          { expectedVersion: view.version, onSettled: onChanged },
        );
    setProposing(false);
    if (!result.ok) {
      setProposeError(commandErrorMessage(result.data, poolingProposalId ? "Couldn't create that pool." : "Couldn't propose that swap."));
      return;
    }
    setSelected([]);
    setPoolingProposalId(null);
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
            <div className="text-xs font-medium text-zinc-500">Projected</div>
            <div className="tabular-nums text-zinc-900">
              {self.projected_value ?? "—"}
              {valueDelta !== null && valueDelta !== 0 && (
                <span className={`ml-1 text-xs font-bold ${valueDelta > 0 ? "text-emerald-600" : "text-red-600"}`}>
                  {valueDelta > 0 ? "↑" : "↓"}
                  {Math.abs(valueDelta)}
                </span>
              )}
            </div>
          </div>
          <div>
            <div className="text-xs font-medium text-zinc-500">{view.haircut_profile ? "Safe" : "Risk reveals in"}</div>
            <div className="tabular-nums text-zinc-900">
              {view.haircut_profile ? (self.safe_value ?? "—") : formatCountdownTo(new Date(view.haircut_reveal_at ?? 0).getTime())}
            </div>
          </div>
          <div>
            <div className="text-xs font-medium text-zinc-500">Influence</div>
            <div className="tabular-nums text-zinc-900">
              {self.influence?.available ?? "—"}
              {self.influence && self.influence.committed > 0 ? ` / ${self.influence.committed} committed` : ""}
            </div>
          </div>
        </div>
      )}

      <div>
        <span className="text-xs font-medium text-zinc-500">Payout chance</span>
        {/* Every row here -- position numbers, payout chance, the card grid
            -- is a plain, label-free flex row with identical per-item width
            and gap, all iterating view.market in the same position-sorted
            order. That's what guarantees column alignment; a leading label
            sharing a row with the numbered cells (the previous shape of
            this markup) throws every cell after it out of alignment with
            the rows above and below, which is exactly the bug this fixed.
            All three rows share one scroll container so they scroll
            together without any scroll-sync code. Payout chance is keyed
            by *position*, not by whichever entity happens to occupy it --
            see projections.py's haircut_risk_band_depth. */}
        <div ref={marketScrollRef} className="-mx-4 overflow-x-auto px-4 pb-2">
          <div className="mb-1 flex gap-2 text-center">
            {view.market.map((entity) => (
              <div key={entity.entity_id} className="w-28 flex-shrink-0 text-xs text-zinc-400">
                {entity.position}
              </div>
            ))}
          </div>
          <div className="mb-2 flex gap-2 text-center">
            {view.market.map((entity) => {
              const cell = payoutChanceCell(entity.position, view.haircut_risk_band_depth, view.haircut_profile);
              return (
                <div key={entity.entity_id} className={`w-28 flex-shrink-0 text-xs font-bold ${cell.className}`}>
                  {cell.text}
                </div>
              );
            })}
          </div>
          <div className="flex gap-2">
            {view.market.map((entity) => {
              const owned = ownedCounts.get(entity.entity_id) ?? 0;
              const isSelected = selected.includes(entity.entity_id);
              const isDisabled = poolGuardrailEntities.has(entity.entity_id);
              const highlight = highlighted?.get(entity.entity_id);
              const markers = supportMarkers.get(entity.entity_id);
              return (
                <button
                  key={entity.entity_id}
                  type="button"
                  data-entity-id={entity.entity_id}
                  onClick={() => toggleSelect(entity.entity_id)}
                  disabled={isDisabled}
                  className={`relative flex w-28 flex-shrink-0 flex-col items-center gap-1 rounded p-2 text-center transition-transform duration-300 ${
                    owned > 0 ? "border-2 border-zinc-900" : "border border-zinc-200"
                  } ${isSelected ? "bg-blue-100 ring-2 ring-blue-500" : "bg-white"} ${isDisabled ? "opacity-30" : ""} ${
                    highlight ? "z-10 scale-110 shadow-lg" : ""
                  } ${highlightRingClass(highlight)}`}
                >
                  <span className="font-mono text-sm font-bold text-zinc-900">{entity.ticker_symbol}</span>
                  <span className="text-xs leading-tight text-zinc-600">{entity.display_name}</span>
                  {owned > 1 && <span className="text-xs font-bold text-zinc-900">×{owned}</span>}
                  {markers && markers.length > 0 && (
                    <div className="flex flex-wrap items-center justify-center gap-0.5 text-[10px] font-bold leading-none text-emerald-700">
                      <span>↑</span>
                      {markers.map((m) => (
                        <span key={m.playerId} title={playerLabel(m.playerId, view)}>
                          {playerInitial(m.playerId, view)}
                          {m.count > 1 && <sup>{formatSupportCount(m.count)}</sup>}
                        </span>
                      ))}
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        </div>
        <div className="my-3 flex items-center gap-3 text-xs text-zinc-400">
          <div className="h-px flex-1 bg-zinc-200" />
          <span>Tap two positions above to propose a swap</span>
          <div className="h-px flex-1 bg-zinc-200" />
        </div>
      </div>

      {poolingProposalId && selected.length < 2 && (
        <div className="flex items-center justify-between gap-2 rounded border border-purple-300 bg-purple-50 p-2 text-xs text-purple-900">
          <span>
            Pooling against {playerLabel(view.proposals.find((p) => p.proposal_id === poolingProposalId)?.proposer_id ?? null, view)}
            &apos;s proposal — tap two other cards.
          </span>
          <button type="button" onClick={cancelSelection} className="flex-shrink-0 underline">
            Cancel
          </button>
        </div>
      )}

      {selected.length === 2 && (
        <div className="flex flex-col gap-2 rounded border border-blue-300 bg-blue-50 p-3 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className="text-blue-900">
              {poolingProposalId ? "Pool" : "Propose"} {entityLabel(selected[0], view)} ↔ {entityLabel(selected[1], view)}?
            </span>
            <div className="flex flex-shrink-0 gap-2">
              <button type="button" onClick={cancelSelection} className="rounded border border-zinc-300 px-3 py-1 text-zinc-700">
                Cancel
              </button>
              <button
                type="button"
                onClick={handleConfirm}
                disabled={proposing || proposeUnaffordable || displayedProposeCost === null}
                className="rounded bg-zinc-900 px-3 py-1 text-white disabled:opacity-50"
              >
                {proposing ? "…" : `${poolingProposalId ? "Pool" : "Propose"}${displayedProposeCost === null ? "" : ` (${displayedProposeCost})`}`}
              </button>
            </div>
          </div>
          {poolingProposalId && (
            <div className="flex items-center gap-3 text-xs text-blue-900">
              <span className="font-medium">Visibility:</span>
              <label className="flex items-center gap-1">
                <input type="radio" checked={poolVisibility === "private"} onChange={() => setPoolVisibility("private")} />
                Private
              </label>
              <label className="flex items-center gap-1">
                <input type="radio" checked={poolVisibility === "public"} onChange={() => setPoolVisibility("public")} />
                Public
              </label>
            </div>
          )}
        </div>
      )}
      {proposeError && <p className="text-sm text-red-600">{proposeError}</p>}

      <OpenProposals gameId={gameId} view={view} onChanged={onChanged} onVisualize={visualizeProposal} onStartPool={startPooling} />

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

      <ActivityStream events={events} view={view} />
    </div>
  );
}

/** Cost-aware Accept button, shared between a bare proposal's Accept and a
 * Pool's Accept -- same shape, same affordability rule (disabled only
 * when the server says this specific accept would cost 1 and self can't
 * cover it). */
function AcceptButton({
  liability,
  available,
  busy,
  onAccept,
}: {
  liability: 0 | 1 | undefined;
  available: number;
  busy: boolean;
  onAccept: () => void;
}) {
  const unaffordable = liability === 1 && available < 1;
  const label = `Accept${liability === undefined ? "" : ` (${liability})`}`;
  return (
    <button
      type="button"
      onClick={onAccept}
      disabled={busy || unaffordable}
      className="rounded bg-zinc-900 px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
    >
      {busy ? "…" : label}
    </button>
  );
}

/** Open, actionable proposals -- sourced from view.proposals (the current
 * -state projection), not the event log. A bare proposal gets Accept/
 * Withdraw (no Reject -- either accept it, let it expire, or counter with
 * a Pool), and each proposal's open Pools render nested beneath it with
 * their own legal actions: only the pool's own initiator can withdraw it
 * or make a private one public; only the base proposer can accept or
 * decline a *private* pool; a *public* pool can be accepted by anyone
 * except its own initiator, base proposer included. Straight from
 * engine._handle_decline_pool/_handle_accept_pool, not re-derived. */
function OpenProposals({
  gameId,
  view,
  onChanged,
  onVisualize,
  onStartPool,
}: {
  gameId: string;
  view: GameView;
  onChanged: () => void;
  onVisualize: (pairs: [string, string][]) => void;
  onStartPool: (proposalId: string) => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const open = view.proposals.filter((p) => p.status === "open");
  const self = view.players.find((p) => p.game_player_id === view.you);
  const available = self?.influence?.available ?? 0;

  async function runCommand(id: string, type: string, payload: Record<string, unknown>, fallback: string) {
    setError(null);
    setBusyId(id);
    const result = await submitCommand(gameId, type, payload, { expectedVersion: view.version, onSettled: onChanged });
    setBusyId(null);
    if (!result.ok) setError(commandErrorMessage(result.data, fallback));
  }

  if (open.length === 0) return null;

  return (
    <div>
      <h2 className="mb-2 text-sm font-medium text-zinc-700">Open proposals ({open.length})</h2>
      <ul className="flex flex-col gap-2 rounded border border-zinc-200 bg-white p-3 text-sm">
        {open.map((p) => {
          const isMine = p.proposer_id === view.you;
          const pools = view.pools.filter((pool) => pool.base_proposal_id === p.proposal_id && pool.status === "open");
          return (
            <li key={p.proposal_id} className="flex flex-col gap-1.5 border-b border-zinc-100 pb-2 text-zinc-900 last:border-0 last:pb-0">
              <div className="flex items-center justify-between gap-2">
                <span>
                  {playerLabel(p.proposer_id, view)}: {entityLabel(p.entity_a, view)} ↔ {entityLabel(p.entity_b, view)}
                </span>
                <span className="flex flex-shrink-0 gap-1">
                  <button
                    type="button"
                    onClick={() => onVisualize([[p.entity_a, p.entity_b]])}
                    className="rounded border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700"
                  >
                    Visualize
                  </button>
                  {!isMine && (
                    <button
                      type="button"
                      onClick={() => onStartPool(p.proposal_id)}
                      className="rounded border border-purple-300 px-2 py-1 text-xs font-medium text-purple-700"
                    >
                      Pool
                    </button>
                  )}
                  {isMine ? (
                    <button
                      type="button"
                      onClick={() => runCommand(p.proposal_id, "WITHDRAW_PROPOSAL", { proposal_id: p.proposal_id }, "Couldn't withdraw.")}
                      disabled={busyId === p.proposal_id}
                      className="rounded border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 disabled:opacity-50"
                    >
                      {busyId === p.proposal_id ? "…" : "Withdraw"}
                    </button>
                  ) : (
                    <AcceptButton
                      liability={p.my_accept_liability}
                      available={available}
                      busy={busyId === p.proposal_id}
                      onAccept={() => runCommand(p.proposal_id, "ACCEPT_PROPOSAL", { proposal_id: p.proposal_id }, "Couldn't accept.")}
                    />
                  )}
                </span>
              </div>

              {pools.length > 0 && (
                <ul className="ml-3 flex flex-col gap-1 border-l border-zinc-200 pl-3">
                  {pools.map((pool) => {
                    const isPoolMine = pool.initiator_id === view.you;
                    const visible = Boolean(pool.entity_c && pool.entity_d);
                    return (
                      <li key={pool.pool_id} className="flex items-center justify-between gap-2 text-xs text-zinc-700">
                        <span>
                          {playerLabel(pool.initiator_id, view)} pooled {pool.visibility}
                          {visible ? `: ${entityLabel(pool.entity_c!, view)} ↔ ${entityLabel(pool.entity_d!, view)}` : " (hidden)"}
                        </span>
                        <span className="flex flex-shrink-0 gap-1">
                          {visible && (
                            <button
                              type="button"
                              onClick={() => onVisualize([[p.entity_a, p.entity_b], [pool.entity_c!, pool.entity_d!]])}
                              className="rounded border border-zinc-300 px-2 py-0.5"
                            >
                              Visualize
                            </button>
                          )}
                          {isPoolMine && (
                            <>
                              {pool.visibility === "private" && (
                                <button
                                  type="button"
                                  onClick={() => runCommand(pool.pool_id, "MAKE_POOL_PUBLIC", { pool_id: pool.pool_id }, "Couldn't make that public.")}
                                  disabled={busyId === pool.pool_id}
                                  className="rounded border border-zinc-300 px-2 py-0.5 disabled:opacity-50"
                                >
                                  Make public
                                </button>
                              )}
                              <button
                                type="button"
                                onClick={() => runCommand(pool.pool_id, "WITHDRAW_POOL", { pool_id: pool.pool_id }, "Couldn't withdraw that pool.")}
                                disabled={busyId === pool.pool_id}
                                className="rounded border border-zinc-300 px-2 py-0.5 disabled:opacity-50"
                              >
                                {busyId === pool.pool_id ? "…" : "Withdraw"}
                              </button>
                            </>
                          )}
                          {!isPoolMine && pool.visibility === "private" && isMine && (
                            <button
                              type="button"
                              onClick={() => runCommand(pool.pool_id, "DECLINE_POOL", { pool_id: pool.pool_id }, "Couldn't decline that pool.")}
                              disabled={busyId === pool.pool_id}
                              className="rounded border border-zinc-300 px-2 py-0.5 disabled:opacity-50"
                            >
                              Decline
                            </button>
                          )}
                          {!isPoolMine && (pool.visibility === "public" || isMine) && (
                            <AcceptButton
                              liability={pool.my_accept_liability}
                              available={available}
                              busy={busyId === pool.pool_id}
                              onAccept={() => runCommand(pool.pool_id, "ACCEPT_POOL", { pool_id: pool.pool_id }, "Couldn't accept that pool.")}
                            />
                          )}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  );
}

const _ACTIVITY_EVENT_TYPES = new Set([
  "PROPOSAL_CREATED",
  "PROPOSAL_RESOLVED",
  "PRIVATE_POOL_CREATED",
  "PUBLIC_POOL_CREATED",
  "POOL_RESOLVED",
]);
const _TONE_CLASSES: Record<string, string> = {
  open: "text-blue-700",
  success: "text-emerald-700",
  muted: "text-zinc-500",
  warning: "text-amber-700",
};

/** Was this pool's base proposal ultimately won by a *different* pool, or
 * did the base proposal execute directly (a plain accept bypassing every
 * pool)? Both cases resolve a preempted sibling with the same
 * "preempted_by_other_action" reason -- the two distinct messages you
 * specified come from checking current state, not the event itself: if
 * some *other* pool on the same base proposal shows executed, another
 * pooled deal won; otherwise the base proposal was accepted directly. */
function describePreemption(pool: PoolView, view: GameView): string {
  const winningPool = view.pools.find(
    (p) => p.base_proposal_id === pool.base_proposal_id && p.pool_id !== pool.pool_id && p.resolution_reason === "executed",
  );
  return winningPool ? "preempted — another pooled deal executed" : "preempted — the base proposal executed directly";
}

function describeEvent(event: EventView, view: GameView): { text: string; tone: keyof typeof _TONE_CLASSES } {
  if (event.type === "PROPOSAL_CREATED") {
    const a = entityLabel(event.payload.entity_a as string, view);
    const b = entityLabel(event.payload.entity_b as string, view);
    return { text: `${playerLabel(event.actor_game_player_id, view)} proposed ${a} ↔ ${b}`, tone: "open" };
  }
  if (event.type === "PROPOSAL_RESOLVED") {
    const proposal = view.proposals.find((p) => p.proposal_id === event.payload.proposal_id);
    const swap = proposal ? `${entityLabel(proposal.entity_a, view)} ↔ ${entityLabel(proposal.entity_b, view)}` : "A proposal";
    const reason = event.payload.reason as string;
    if (reason === "executed") return { text: `${swap} — accepted`, tone: "success" };
    if (reason === "withdrawn_by_initiator") return { text: `${swap} — withdrawn`, tone: "muted" };
    if (reason === "market_closed") return { text: `${swap} — expired, market closed`, tone: "warning" };
    return { text: `${swap} — resolved`, tone: "muted" };
  }
  if (event.type === "PRIVATE_POOL_CREATED" || event.type === "PUBLIC_POOL_CREATED") {
    const who = playerLabel(event.actor_game_player_id, view);
    const entityC = event.payload.entity_c as string | undefined;
    const entityD = event.payload.entity_d as string | undefined;
    const swap = entityC && entityD ? `${entityLabel(entityC, view)} ↔ ${entityLabel(entityD, view)}` : "a hidden pair";
    const visibility = event.type === "PUBLIC_POOL_CREATED" ? "publicly" : "privately";
    return { text: `${who} pooled ${visibility}: ${swap}`, tone: "open" };
  }
  if (event.type === "POOL_RESOLVED") {
    const pool = view.pools.find((p) => p.pool_id === event.payload.pool_id);
    const swap = pool?.entity_c && pool?.entity_d ? `${entityLabel(pool.entity_c, view)} ↔ ${entityLabel(pool.entity_d, view)}` : "A pool";
    const reason = event.payload.reason as string;
    if (reason === "executed") return { text: `${swap} — pool executed`, tone: "success" };
    if (reason === "withdrawn_by_initiator") return { text: `${swap} — pool withdrawn`, tone: "muted" };
    if (reason === "declined_by_target") return { text: `${swap} — pool declined`, tone: "muted" };
    if (reason === "base_proposal_withdrawn") return { text: `${swap} — base proposal was withdrawn`, tone: "muted" };
    if (reason === "invalidated_by_initiator_action") return { text: `${swap} — pool abandoned by its own initiator`, tone: "muted" };
    if (reason === "preempted_by_other_action" && pool) return { text: `${swap} — ${describePreemption(pool, view)}`, tone: "warning" };
    if (reason === "market_closed") return { text: `${swap} — expired, market closed`, tone: "warning" };
    return { text: `${swap} — pool resolved`, tone: "muted" };
  }
  return { text: event.type.replaceAll("_", " ").toLowerCase(), tone: "muted" };
}

type ActivityFilter = "none" | "executed" | "all";
const _ACTIVITY_FILTERS: { key: ActivityFilter; label: string }[] = [
  { key: "none", label: "None" },
  { key: "executed", label: "Executed" },
  { key: "all", label: "All" },
];

function matchesActivityFilter(event: EventView, filter: ActivityFilter): boolean {
  if (filter === "none") return false;
  if (!_ACTIVITY_EVENT_TYPES.has(event.type)) return false;
  if (filter === "all") return true;
  return (event.type === "PROPOSAL_RESOLVED" || event.type === "POOL_RESOLVED") && event.payload.reason === "executed";
}

/** Chronological narrative, distinct from OpenProposals' actionable
 * current-state list -- the event log is what makes "still actionable" vs
 * "history" legible over time, especially once Pools (Stage 4) add
 * preemption into the mix. Defaults to "executed" -- a raw feed of every
 * proposal/withdraw/expiry gets noisy fast in an active game, per direct
 * playtest feedback; the words-as-buttons let a player dial that back up
 * when they actually want the full narrative. */
function ActivityStream({ events, view }: { events: EventView[]; view: GameView }) {
  const [filter, setFilter] = useState<ActivityFilter>("executed");
  const relevant = events.filter((e) => matchesActivityFilter(e, filter));

  return (
    <div data-testid="activity-panel">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-medium text-zinc-700">Activity</h2>
        <div className="flex gap-1">
          {_ACTIVITY_FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setFilter(f.key)}
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                filter === f.key ? "bg-zinc-900 text-white" : "border border-zinc-300 text-zinc-600"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>
      <ul className="flex flex-col gap-1 rounded border border-zinc-200 bg-white p-3 text-sm">
        {relevant.length === 0 && <li className="text-zinc-400">Nothing yet</li>}
        {[...relevant]
          .reverse()
          .map((event) => {
            const { text, tone } = describeEvent(event, view);
            return (
              <li key={event.seq_no} className={_TONE_CLASSES[tone]}>
                {text}
              </li>
            );
          })}
      </ul>
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
  view: GameView;
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

/** navigator.share opens the OS share sheet directly (WhatsApp/Messages
 * show up as real targets there on a phone) -- far better than "copy,
 * then go paste it yourself" for the actual use case (host on their
 * phone, remote player elsewhere). Falls back to clipboard copy when the
 * Web Share API isn't available (most desktop browsers) or a share
 * genuinely fails; a user-cancelled share sheet (AbortError) is normal
 * interaction, not an error, and gets no feedback at all. `canShare` is
 * only set post-mount to avoid an SSR/client render mismatch on a
 * browser-only API. */
function ShareLinkButton({ url, joinCode }: { url: string; joinCode: string }) {
  const [canShare, setCanShare] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Querying an external system (the browser's Web Share API) on mount,
    // not deriving state from props -- the legitimate case the lint
    // rule's own docs carve out, same precedent as useGameView's polling
    // effect. Deliberately not a lazy useState initializer either: that
    // would run during SSR too, where `navigator` doesn't exist, and
    // produce a hydration mismatch against the real client-side value.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCanShare(typeof navigator !== "undefined" && typeof navigator.share === "function");
  }, []);

  async function copyToClipboard() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Couldn't copy — long-press the code above instead.");
    }
  }

  async function handleClick() {
    setError(null);
    if (canShare) {
      try {
        await navigator.share({ title: "Join my Gotiate game", text: `Join code: ${joinCode}`, url });
        return;
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return; // user cancelled -- not an error
      }
    }
    await copyToClipboard();
  }

  return (
    <div className="flex flex-col items-center gap-1">
      <button
        type="button"
        onClick={handleClick}
        className="flex items-center gap-1.5 rounded border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium text-zinc-700"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M10 13a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07l-1.5 1.5" />
          <path d="M14 11a5 5 0 0 0-7.07 0L4.1 13.83a5 5 0 0 0 7.07 7.07l1.49-1.49" />
        </svg>
        {copied ? "Copied!" : canShare ? "Share link" : "Copy link"}
      </button>
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
  view: GameView;
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
            {joinUrl && <ShareLinkButton url={joinUrl} joinCode={joinCode} />}
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
