"use client";

import { use, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import Image from "next/image";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { QRCodeSVG } from "qrcode.react";
import { ensureAnonymousSession } from "@/lib/auth";
import { useGameView, type GameView, type HoldingView, type PoolView } from "@/lib/useGameView";
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
  if (view.phase === "CLOSING") {
    // close_market freezes/reveals/scores in one synchronous server-side
    // pass, so a poll can trivially observe NEGOTIATION on one tick and
    // SCORED on the next -- this may render for well under a second, or
    // never at all. Not worth a built-out screen of its own.
    return <CenteredMessage>Closing the market…</CenteredMessage>;
  }
  if (view.phase === "SCORED") {
    return <ResultsView gameId={gameId} view={view} />;
  }
  if (view.phase !== "LOBBY") {
    // Defensive fallback -- every real phase is handled above.
    return (
      <CenteredMessage showHomeLink>Game is live (phase: {view.phase}).</CenteredMessage>
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

function positionOf(entityId: string, view: GameView): number | null {
  return view.market.find((m) => m.entity_id === entityId)?.position ?? null;
}

function playerLabel(playerId: string | null, view: GameView): string {
  if (!playerId) return "The market";
  return view.players.find((p) => p.game_player_id === playerId)?.display_name ?? "Someone";
}

function playerInitial(playerId: string, view: GameView): string {
  return playerLabel(playerId, view).charAt(0).toUpperCase();
}

/** A just-resolved proposal/pool, held onto client-side just long enough to
 * overlay its *own, still-in-place* row with a frozen banner before it
 * fades -- see MarketView's lingeringDeals state. Keyed by proposal_id/
 * pool_id specifically so OpenProposals can look this up per-row and
 * transform that exact row in place, rather than removing it and
 * rendering a separate entry elsewhere (which used to make an accepted
 * deal visually jump to a different position in the list).
 *
 * Detected from view.proposals/view.pools' own open->resolved transitions
 * (see MarketView's prevResolutionStatusRef), not from the event log --
 * driving this from the *same* poll that also decides row visibility is
 * what makes it race-free (an events-poll-driven version of this raced
 * against the view poll: whichever landed first decided whether the row
 * survived long enough to ever get tagged, so the overlay would silently
 * no-show for an arbitrary subset of players/deals depending on network
 * timing -- a real bug found via live playtesting, not hypothetical).
 * The events log is still consulted, but only to *enrich* an
 * already-created "accepted" entry with the accepter's name once that
 * event arrives (view.proposals/view.pools have no such field) -- never to
 * create the entry or decide whether the row is still shown.
 *
 * "everyone_passed" exists because EXPIRED_ALL_PASSED is masked to the
 * identical withdrawn_by_initiator reason as a genuine self-withdraw for
 * every live audience, proposer included (see the Pass design writeup).
 * Only the proposer's own client can even attempt to tell the two apart
 * (via myExplicitWithdrawalsRef, tracking their own Withdraw clicks) --
 * every other player just sees "withdrawn", which is also the correct
 * default for the proposer's own client when it can't prove otherwise. */
interface LingeringDeal {
  key: string;
  kind: "accepted" | "everyone_passed" | "withdrawn";
  accepterLabel?: string; // only ever set for kind "accepted", and only once the enriching event arrives
  fading: boolean;
}

/** A proposal I just PASS_PROPOSAL'd on -- unlike every other resolution,
 * Pass omits the proposal from *my own* view.proposals entirely, live (see
 * the Pass design writeup), so there's no server-projected row left to
 * overlay the way lingeringDeals does. This snapshots just enough to keep
 * rendering the row myself, client-only, for the same linger-then-fade
 * beat -- captured at the moment of the click, from data OpenProposals
 * already has in scope, no event-log correlation needed (I already know
 * I'm the one who passed). */
interface PassLingeringEntry {
  proposalId: string;
  entityA: string;
  entityB: string;
  proposerId: string;
  fading: boolean;
}

/** Influence as a glanceable fuel gauge, not just a number -- the exact
 * count still renders alongside it (transaction costs matter), but the bar
 * is what you read at a glance mid-negotiation. `total` (the gauge's max)
 * is derived, not fetched: available + committed + spent is exactly
 * starting_influence, since nothing ever creates or destroys Influence
 * mid-game, only moves it between those three buckets. */
function InfluenceGauge({ influence }: { influence: { available: number; committed: number; spent: number } | undefined }) {
  if (!influence) {
    return (
      <div>
        <div className="text-xs font-medium text-zinc-500">Influence</div>
        <div className="tabular-nums text-zinc-900">—</div>
      </div>
    );
  }
  const total = influence.available + influence.committed + influence.spent;
  const pct = total > 0 ? Math.round((influence.available / total) * 100) : 0;
  const barColor = pct > 50 ? "bg-emerald-500" : pct > 20 ? "bg-amber-500" : "bg-red-500";
  return (
    <div>
      <div className="text-xs font-medium text-zinc-500">Influence</div>
      <div className="flex items-center gap-1.5">
        <div className="h-2 w-14 overflow-hidden rounded-full bg-zinc-200">
          <div className={`h-full rounded-full transition-[width] duration-500 ${barColor}`} style={{ width: `${pct}%` }} />
        </div>
        <span className="tabular-nums text-zinc-900">
          {influence.available}
          {influence.committed > 0 ? ` (+${influence.committed}c)` : ""}
        </span>
      </div>
    </div>
  );
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
      // 5s, not the original 2.5s -- playtesting found the badge vanished
      // before anyone could actually read it.
      const timeout = setTimeout(() => setDelta(null), 5000);
      prevRef.current = value;
      return () => clearTimeout(timeout);
    }
    prevRef.current = value;
  }, [value]);

  return delta;
}

// w-28 (112px) + gap-2 (8px) -- the market card row's fixed per-item
// horizontal step, both set by Tailwind classes on the row below.
const _MARKET_CARD_STEP_PX = 120;

/** Manual FLIP (First-Last-Invert-Play) reorder animation for the market
 * card row -- so the eye can follow an entity sliding from one position to
 * another instead of the whole row jumping instantly. No animation library:
 * cards are keyed by stable entity_id and the row's per-item width/gap are
 * fixed, so the horizontal delta between two polls is plain index
 * arithmetic, not a DOM measurement. Returns a per-entity style getter; a
 * card with no pending delta gets `{}` (no inline transform at all, so it
 * never fights the Visualize-highlight scale/ring classes applied to the
 * same button element). */
function useMarketReorderFlip(entityIds: string[]): (entityId: string) => CSSProperties {
  const prevIndexRef = useRef<Map<string, number>>(new Map(entityIds.map((id, i) => [id, i])));
  const [deltas, setDeltas] = useState<Map<string, number>>(new Map());
  const [settled, setSettled] = useState(true);
  const key = entityIds.join(",");

  useLayoutEffect(() => {
    const prev = prevIndexRef.current;
    const nextIndex = new Map<string, number>();
    const nextDeltas = new Map<string, number>();
    entityIds.forEach((id, i) => {
      nextIndex.set(id, i);
      const prevI = prev.get(id);
      if (prevI !== undefined && prevI !== i) nextDeltas.set(id, (prevI - i) * _MARKET_CARD_STEP_PX);
    });
    prevIndexRef.current = nextIndex;
    if (nextDeltas.size === 0) return;

    setDeltas(nextDeltas);
    setSettled(false);
    const raf = requestAnimationFrame(() => setSettled(true)); // next frame: "play" -- animate back to the natural position
    const timeout = setTimeout(() => setDeltas(new Map()), 650); // past the transition's own duration -- stop paying for inline styles once it's done
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(timeout);
    };
    // `key` is entityIds' real dependency -- the array itself gets a new identity every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return (entityId: string) => {
    const delta = deltas.get(entityId);
    if (delta === undefined) return {};
    return {
      transform: settled ? "translateX(0)" : `translateX(${delta}px)`,
      transition: settled ? "transform 500ms ease" : "none",
    };
  };
}

function MarketView({ gameId, view, onChanged }: { gameId: string; view: GameView; onChanged: () => void }) {
  const self = view.players.find((p) => p.game_player_id === view.you);
  const valueDelta = useValueDelta(self?.projected_value);
  const [selected, setSelected] = useState<string[]>([]);
  // Non-null while countering a specific open proposal with a Pool --
  // reuses `selected` for card-picking, but the confirm bar's action and
  // labels branch on this instead of always submitting PROPOSE_SWAP.
  const [poolingProposalId, setPoolingProposalId] = useState<string | null>(null);
  // Non-null while Stage 5's unilateral reserve-for-swap is picking its
  // two market entities -- same `selected` card-picking reuse as pooling,
  // a third confirm-bar branch below. Mutually exclusive with
  // poolingProposalId (starting one clears the other, see cancelSelection).
  const [burningReserveId, setBurningReserveId] = useState<string | null>(null);
  const [proposeError, setProposeError] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  const { events } = useGameEvents(gameId);
  const supportMarkers = useMemo(() => computeSupportMarkers(events, view.pools), [events, view.pools]);
  const getFlipStyle = useMarketReorderFlip(view.market.map((e) => e.entity_id));

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

  /** Accepts one pair (bare proposal) or two (a proposal + its pool) --
   * direction per pair is the entity's LOCKED rising_entity_id (see the
   * market-direction-reversal design writeup), never recomputed from
   * current market positions. A negotiation's arrows stay pinned to what
   * was authored right up until it executes or voids -- if the market
   * crosses that relationship, the negotiation voids rather than the
   * arrows silently flipping. */
  function visualizeProposal(pairs: [string, string, string][]) {
    const next = new Map<string, { direction: "rising" | "falling"; pairIndex: number }>();
    pairs.forEach(([a, b, risingId], pairIndex) => {
      const falling = risingId === a ? b : a;
      next.set(risingId, { direction: "rising", pairIndex });
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

  // A just-resolved proposal/pool used to just vanish from the open list
  // the instant it resolved -- easy to miss who did what. Instead its own
  // row (still in its original list position -- see OpenProposals, which
  // looks this map up per-proposal/pool-id and renders straight from
  // view.proposals/view.pools rather than any separately-positioned ghost
  // entry) freezes under a translucent overlay for a beat, then fades.
  const [lingeringDeals, setLingeringDeals] = useState<LingeringDeal[]>([]);
  // Populated the instant *this* player clicks Withdraw on their own
  // proposal (see OpenProposals' onSelfWithdrawProposal) -- the only way to
  // tell "I withdrew this" apart from "everyone passed and it auto
  // -expired" once both collapse to the same masked resolution reason.
  const myExplicitWithdrawalsRef = useRef<Set<string>>(new Set());

  function addLingeringDeal(key: string, kind: LingeringDeal["kind"], accepterLabel?: string) {
    setLingeringDeals((prev) => (prev.some((d) => d.key === key) ? prev : [...prev, { key, kind, accepterLabel, fading: false }]));
    setTimeout(() => setLingeringDeals((prev) => prev.map((d) => (d.key === key ? { ...d, fading: true } : d))), 2200);
    setTimeout(() => setLingeringDeals((prev) => prev.filter((d) => d.key !== key)), 2900);
  }

  function markSelfWithdrawnProposal(proposalId: string) {
    myExplicitWithdrawalsRef.current.add(proposalId);
  }

  // Detection lives on view.proposals/view.pools' own open->resolved
  // transitions -- the *same* poll that also decides whether a row is still
  // "open" -- specifically so there is no window where a row is neither
  // open nor yet tagged lingering. An earlier version drove this off the
  // event log (a *separate* poll, on its own independent ~1s cycle); a
  // proposal/pool resolving between the two polls landing was a real race
  // -- whichever poll updated first decided whether the row got dropped
  // before it was ever tagged, so the overlay silently no-showed for an
  // arbitrary subset of players and deals depending on network timing (a
  // bug found via live playtesting, not hypothetical). The event log is
  // still consulted below, but only to enrich an already-created "accepted"
  // entry with the accepter's name -- view.proposals/view.pools have no
  // such field -- never to decide whether the row survives.
  const prevProposalStatusRef = useRef<Map<string, string> | null>(null);
  const prevPoolStatusRef = useRef<Map<string, string> | null>(null);
  useEffect(() => {
    const currentProposalStatus = new Map(view.proposals.map((p) => [p.proposal_id, p.status]));
    const prevProposalStatus = prevProposalStatusRef.current;
    prevProposalStatusRef.current = currentProposalStatus;
    // First observation (mount, or a passer's proposal appearing for the
    // first time) is a baseline only -- nothing "just now" resolved.
    // Syncing local lingering state with the external system (server's
    // resolved-proposal/pool set) -- same carve-out/precedent as
    // ShareLinkButton's effect further down. addLingeringDeal wraps
    // setLingeringDeals internally; every call below is covered.
    /* eslint-disable react-hooks/set-state-in-effect */
    if (prevProposalStatus) {
      for (const p of view.proposals) {
        if (p.status !== "resolved" || prevProposalStatus.get(p.proposal_id) !== "open") continue;
        if (p.resolution_reason === "executed") {
          addLingeringDeal(p.proposal_id, "accepted");
          visualizeProposal([[p.entity_a, p.entity_b, p.rising_entity_id]]);
        } else if (p.resolution_reason === "withdrawn_by_initiator") {
          const isMasked = p.proposer_id === view.you && !myExplicitWithdrawalsRef.current.has(p.proposal_id);
          addLingeringDeal(p.proposal_id, isMasked ? "everyone_passed" : "withdrawn");
        }
        // market_closed / voided_market_swung: no lingering treatment, same as before.
      }
    }

    const currentPoolStatus = new Map(view.pools.map((pool) => [pool.pool_id, pool.status]));
    const prevPoolStatus = prevPoolStatusRef.current;
    prevPoolStatusRef.current = currentPoolStatus;
    if (prevPoolStatus) {
      for (const pool of view.pools) {
        if (pool.status !== "resolved" || prevPoolStatus.get(pool.pool_id) !== "open") continue;
        if (pool.resolution_reason === "executed") {
          addLingeringDeal(pool.pool_id, "accepted");
          if (pool.entity_c && pool.entity_d && pool.rising_entity_id) {
            const base = view.proposals.find((p) => p.proposal_id === pool.base_proposal_id);
            const pairs: [string, string, string][] = [[pool.entity_c, pool.entity_d, pool.rising_entity_id]];
            if (base) pairs.unshift([base.entity_a, base.entity_b, base.rising_entity_id]);
            visualizeProposal(pairs);
          }
        } else if (pool.resolution_reason === "withdrawn_by_initiator") {
          // Never masked for pools -- Pass doesn't apply to them, so this is
          // always a genuine self-withdraw, no proposer-only disambiguation needed.
          addLingeringDeal(pool.pool_id, "withdrawn");
        }
        // Every other pool reason (declined/preempted/invalidated/base
        // -proposal-withdrawn-or-voided) -- no lingering treatment, same as before.
      }
    }
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [view.proposals, view.pools, view.you]);

  // Enrichment only: fills in *who* accepted, once the event carrying that
  // identity (view.proposals/view.pools have no such field) arrives --
  // never creates a lingering entry and never decides whether a row is
  // shown, see the detection effect above.
  const processedSeqRef = useRef(0);
  const enrichmentInitializedRef = useRef(false);
  useEffect(() => {
    // useGameEvents fetches the *entire* backlog on mount (since_seq=1) --
    // a mid-game page reload must not re-enrich (or, worse, re-lookup a
    // long-gone) entry as if it just happened. First run only marks the
    // backlog seen.
    if (!enrichmentInitializedRef.current) {
      enrichmentInitializedRef.current = true;
      processedSeqRef.current = events.length > 0 ? Math.max(...events.map((e) => e.seq_no)) : 0;
      return;
    }
    const freshEvents = events.filter((e) => e.seq_no > processedSeqRef.current);
    if (freshEvents.length === 0) return;
    processedSeqRef.current = Math.max(processedSeqRef.current, ...freshEvents.map((e) => e.seq_no));

    for (const e of freshEvents) {
      if (e.type === "PROPOSAL_RESOLVED" && e.payload.reason === "executed") {
        const key = e.payload.proposal_id as string;
        setLingeringDeals((prev) => prev.map((d) => (d.key === key ? { ...d, accepterLabel: playerLabel(e.actor_game_player_id, view) } : d)));
      } else if (e.type === "POOL_RESOLVED" && e.payload.reason === "executed") {
        const key = e.payload.pool_id as string;
        setLingeringDeals((prev) => prev.map((d) => (d.key === key ? { ...d, accepterLabel: playerLabel(e.actor_game_player_id, view) } : d)));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- view is read fresh each run without re-triggering it themselves; `events` (growing every poll) is the real driver.
  }, [events]);

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

  // The base proposal can resolve (someone else accepts/withdraws it, or it
  // voids on a market swing) at any moment while a player is still mid-Pool
  // composition against it -- their own poll picks that up on the very next
  // tick. Without this, the composer stays open against a proposal that no
  // longer exists, and CREATE_POOL just fails server-side with a generic
  // error instead of the UI recognizing its own reason for being is gone.
  useEffect(() => {
    if (poolingProposalId && (!basePoolingProposal || basePoolingProposal.status !== "open")) {
      // Syncing local composer state with the external system (the
      // server's polled game state), not deriving it from props -- same
      // carve-out/precedent as ShareLinkButton's effect below.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSelected([]);
      setPoolingProposalId(null);
    }
  }, [poolingProposalId, basePoolingProposal]);

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
    setBurningReserveId(null);
  }

  function cancelSelection() {
    setProposeError(null);
    setSelected([]);
    setPoolingProposalId(null);
    setBurningReserveId(null);
  }

  function startBurning(holdingId: string) {
    setProposeError(null);
    setSelected([]);
    setPoolingProposalId(null);
    setBurningReserveId(holdingId);
  }

  // Server-authoritative preview of what PROPOSE_SWAP would cost -- never
  // reimplemented client-side (the private Influence economy is deliberate
  // about this), so this is a real fetch, not a local computation. Keyed
  // on the pair itself (not raw `selected`) so dropping back below two
  // selections doesn't re-fire a fetch; the display below only trusts
  // `proposeCost` while a pair is actually selected, so a stale value left
  // over from a previous pair is never shown.
  // Burn-for-swap costs no Influence at all (see engine._handle_burn_reserve_for_swap
  // -- no liability logic in it whatsoever), so there's nothing to preview.
  const selectedPair = selected.length === 2 && !burningReserveId ? selected : null;
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

  // Dedicated decision mode -- every hook above still runs every render
  // (rules of hooks), but nothing below this point ever executes or
  // renders while a pickup is pending. In particular `events` (from
  // useGameEvents, a *separate* poll with no concept of freezing) never
  // reaches the decision view -- it's a fully self-contained component
  // reading only from `view`/`pending`, so there's nothing live to leak
  // through the activity ticker or support markers by construction, not
  // just by discipline. See the Stage 5 Reserve UX design writeup.
  if (view.pending_pickup) {
    return <PendingPickupDecisionView gameId={gameId} view={view} pending={view.pending_pickup} onChanged={onChanged} />;
  }

  // `poolVisibility` is only ever passed by the two explicit Pool
  // Private/Pool Public buttons below -- there's no separate visibility
  // toggle to read from anymore (see item 7: replaced radios with buttons
  // that carry their own choice directly).
  async function handleConfirm(poolVisibility?: "private" | "public") {
    if (selected.length !== 2) return;
    setProposeError(null);
    setProposing(true);
    const result = poolingProposalId
      ? await submitCommand(
          gameId,
          "CREATE_POOL",
          { proposal_id: poolingProposalId, entity_c: selected[0], entity_d: selected[1], visibility: poolVisibility ?? "private" },
          { expectedVersion: view.version, onSettled: onChanged },
        )
      : burningReserveId
        ? await submitCommand(
            gameId,
            "BURN_RESERVE_FOR_SWAP",
            { reserve_holding_id: burningReserveId, entity_a: selected[0], entity_b: selected[1] },
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
      setProposeError(
        commandErrorMessage(result.data, poolingProposalId ? "Couldn't create that pool." : burningReserveId ? "Couldn't burn that reserve." : "Couldn't propose that swap."),
      );
      return;
    }
    setSelected([]);
    setPoolingProposalId(null);
    setBurningReserveId(null);
  }

  return (
    <div className="flex flex-1 flex-col gap-6 bg-zinc-50 px-4 py-6">
      <div className="flex items-center justify-between">
        <Image src="/gotiate-logo.png" alt="Gotiate" width={120} height={87} priority />
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
          <InfluenceGauge influence={self.influence} />
        </div>
      )}

      <MarketCorrectionBanner gameId={gameId} view={view} onChanged={onChanged} />

      <div>
        <div className="flex items-baseline justify-between text-xs font-medium text-zinc-500">
          <span>Position — points if it survives</span>
          <span>Payout chance</span>
        </div>
        {/* Every row here -- position/points, payout chance, the card grid
            -- is a plain, label-free flex row with identical per-item width
            and gap, all iterating view.market in the same position-sorted
            order. That's what guarantees column alignment; a leading label
            sharing a row with the numbered cells (the previous shape of
            this markup) throws every cell after it out of alignment with
            the rows above and below, which is exactly the bug this fixed.
            All three rows share one scroll container so they scroll
            together without any scroll-sync code. Payout chance is keyed
            by *position*, not by whichever entity happens to occupy it --
            see projections.py's haircut_risk_band_depth. Points-per-position
            (market_size - position + 1) is the same public linear_rank_v1
            formula projected_value already sums -- derived here purely for
            display, same as haircutRisk.ts's certaintyAt, never a second
            source of truth for an actual score. */}
        <div ref={marketScrollRef} className="-mx-4 overflow-x-auto px-4 pb-2">
          <div className="mb-2 flex gap-2 text-center">
            {view.market.map((entity) => {
              const points = view.market.length - entity.position + 1;
              return (
                <div key={entity.entity_id} className="w-28 flex-shrink-0 text-xs text-zinc-400">
                  #{entity.position} · {points}pt{points === 1 ? "" : "s"}
                </div>
              );
            })}
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
                <div key={entity.entity_id} className="flex-shrink-0" style={getFlipStyle(entity.entity_id)}>
                  <button
                    type="button"
                    data-entity-id={entity.entity_id}
                    onClick={() => toggleSelect(entity.entity_id)}
                    disabled={isDisabled}
                    className={`relative flex h-36 w-28 flex-shrink-0 flex-col items-center gap-1 overflow-hidden rounded p-2 text-center transition-transform duration-300 ${
                      owned > 0 ? "border-2 border-zinc-900" : "border border-zinc-200"
                    } ${isSelected ? "bg-blue-100 ring-2 ring-blue-500" : "bg-white"} ${isDisabled ? "opacity-30" : ""} ${
                      highlight ? "z-10 scale-110 shadow-lg" : ""
                    } ${highlightRingClass(highlight)}`}
                  >
                    {entity.logo_url ? (
                      <Image src={entity.logo_url} alt="" width={24} height={24} unoptimized className="h-6 w-6 flex-shrink-0 rounded-sm object-cover" />
                    ) : (
                      // No logo_url configured for this entity (true for every entity
                      // today -- theme_data doesn't populate it yet) -- a same-size
                      // blank square keeps every card the same height either way.
                      <span aria-hidden className="block h-6 w-6 flex-shrink-0 rounded-sm bg-zinc-100" />
                    )}
                    <span className="flex-shrink-0 font-mono text-sm font-bold text-zinc-900">{entity.ticker_symbol}</span>
                    {/* Fixed card height (h-36 above) is what actually keeps
                        every card the same size regardless of player count/
                        badges -- line-clamp-2 here just stops a long
                        display_name from pushing past its own reserved two
                        lines and getting clipped by the card's own overflow
                        -hidden mid-word. */}
                    <span className="line-clamp-2 text-xs leading-tight text-zinc-600">{entity.display_name}</span>
                    {owned > 1 && <span className="text-xs font-bold text-zinc-900">×{owned}</span>}
                    {markers && markers.length > 0 && (
                      <div className="flex flex-wrap items-center justify-center gap-0.5 text-[10px] font-bold leading-none text-emerald-700">
                        <span>↑</span>
                        {markers.map((m) => (
                          <span
                            key={m.playerId}
                            title={`${playerLabel(m.playerId, view)}${m.unilateralCount > 0 ? " — unilateral move" : ""}`}
                          >
                            {playerInitial(m.playerId, view)}
                            {m.count > 1 && <sup>{formatSupportCount(m.count)}</sup>}
                            {/* Distinct from negotiated support: this player burned a
                                reserve to force this rise themselves, not just agreed
                                to it with someone. See the unilateral-marker design
                                writeup -- deliberately not folded into the emerald
                                count above. */}
                            {m.unilateralCount > 0 && (
                              <span className="text-purple-600" title="Unilateral reserve burn">
                                ⚡
                              </span>
                            )}
                          </span>
                        ))}
                      </div>
                    )}
                  </button>
                </div>
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

      {burningReserveId && selected.length < 2 && (
        <div className="flex items-center justify-between gap-2 rounded border border-purple-300 bg-purple-50 p-2 text-xs text-purple-900">
          <span>Burning a reserve for a unilateral swap — tap two cards. Never revealed to you either way.</span>
          <button type="button" onClick={cancelSelection} className="flex-shrink-0 underline">
            Cancel
          </button>
        </div>
      )}

      {selected.length === 2 && (
        <div className="flex flex-col gap-2 rounded border border-blue-300 bg-blue-50 p-3 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className="text-blue-900">
              {poolingProposalId ? "Pool" : burningReserveId ? "Burn for swap" : "Propose"} {entityLabel(selected[0], view)} ↔ {entityLabel(selected[1], view)}?
            </span>
            <div className="flex flex-shrink-0 gap-2">
              <button type="button" onClick={cancelSelection} className="rounded border border-zinc-300 px-3 py-1 text-zinc-700">
                Cancel
              </button>
              {poolingProposalId ? (
                // Two direct-action buttons instead of a generic "Pool"
                // button plus a separate visibility radio pair -- the
                // radios were hard to see/use at the table; this makes the
                // choice itself the tap.
                <>
                  <button
                    type="button"
                    onClick={() => handleConfirm("private")}
                    disabled={proposing || proposeUnaffordable || displayedProposeCost === null}
                    className="rounded bg-purple-700 px-3 py-1 text-white disabled:opacity-50"
                  >
                    {proposing ? "…" : `Pool Private${displayedProposeCost === null ? "" : ` (${displayedProposeCost})`}`}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleConfirm("public")}
                    disabled={proposing || proposeUnaffordable || displayedProposeCost === null}
                    className="rounded bg-purple-900 px-3 py-1 text-white disabled:opacity-50"
                  >
                    {proposing ? "…" : `Pool Public${displayedProposeCost === null ? "" : ` (${displayedProposeCost})`}`}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => handleConfirm()}
                  disabled={proposing || (!burningReserveId && (proposeUnaffordable || displayedProposeCost === null))}
                  className="rounded bg-zinc-900 px-3 py-1 text-white disabled:opacity-50"
                >
                  {proposing ? "…" : burningReserveId ? "Burn" : `Propose${displayedProposeCost === null ? "" : ` (${displayedProposeCost})`}`}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
      {proposeError && <p className="text-sm text-red-600">{proposeError}</p>}

      <OpenProposals
        gameId={gameId}
        view={view}
        onChanged={onChanged}
        onVisualize={visualizeProposal}
        onStartPool={startPooling}
        lingeringDeals={lingeringDeals}
        onSelfWithdrawProposal={markSelfWithdrawnProposal}
      />

      <ReserveControls gameId={gameId} view={view} onChanged={onChanged} burningReserveId={burningReserveId} onStartBurn={startBurning} onCancelBurn={cancelSelection} />

      <ReadyToCloseToggle gameId={gameId} view={view} onChanged={onChanged} />

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

/** Deliberately self-only, no aggregate anywhere -- no count, no
 * percentage, no hint of anyone else's state or how close the table is
 * to closing. Readiness is a secret trigger, not a public countdown:
 * the only thing anyone else ever learns is the sudden fact of closure
 * itself once the threshold is actually reached (see describeEvent's
 * CLOSE_THRESHOLD_REACHED/MARKET_CLOSED copy below). Matches the
 * backend's own "owner-only live, never the aggregate" projection rule
 * exactly -- there is no field to show a count from even if we wanted
 * to. */
function ReadyToCloseToggle({ gameId, view, onChanged }: { gameId: string; view: GameView; onChanged: () => void }) {
  const self = view.players.find((p) => p.game_player_id === view.you);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!self) return null;
  const ready = self.ready_to_close === true;

  async function handleToggle() {
    setError(null);
    setBusy(true);
    const result = await submitCommand(gameId, "SET_READY_TO_CLOSE", { ready: !ready }, { expectedVersion: view.version, onSettled: onChanged });
    setBusy(false);
    if (!result.ok) setError(commandErrorMessage(result.data, "Couldn't update readiness."));
  }

  return (
    <div className="flex items-center justify-between gap-2 rounded border border-zinc-200 bg-white p-3 text-sm">
      <span className="text-zinc-700">{ready ? "You're marked ready to close." : "Mark yourself ready to close whenever you're done."}</span>
      <button
        type="button"
        onClick={handleToggle}
        disabled={busy}
        className={`flex-shrink-0 rounded border px-3 py-1 text-xs font-medium disabled:opacity-50 ${
          ready ? "border-emerald-500 bg-emerald-50 text-emerald-700" : "border-zinc-300 text-zinc-700"
        }`}
      >
        {busy ? "…" : ready ? "Ready ✓" : "Ready to close"}
      </button>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}

/** 2-player-only anti-stagnation mechanic -- public to both players the
 * instant one is offered, deliberately minimal (never entities or
 * displacement, only the id + countdown -- see useGameView.ts). Either
 * player may trigger; there's no veto and no confirmation dialog, same
 * one-tap convention as Accept/Ready. A real expectedVersion is sent
 * (unlike DISCARD_HOLDING/DECLINE_PICKUP) -- this is a normal, live
 * -polled, game-level offer, not a frozen per-player snapshot. See the
 * Market Correction design writeup. */
function MarketCorrectionBanner({ gameId, view, onChanged }: { gameId: string; view: GameView; onChanged: () => void }) {
  const correction = view.pending_market_correction;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!correction) return null;
  const correctionId = correction.correction_id;

  async function handleTrigger() {
    setError(null);
    setBusy(true);
    const result = await submitCommand(
      gameId,
      "TRIGGER_MARKET_CORRECTION",
      { correction_id: correctionId },
      { expectedVersion: view.version, onSettled: onChanged },
    );
    setBusy(false);
    if (!result.ok) setError(commandErrorMessage(result.data, "Couldn't trigger the Market Correction."));
  }

  return (
    <div className="flex flex-col gap-2 rounded border border-indigo-300 bg-indigo-50 p-3 text-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="text-indigo-900">Market Correction available. Ready to shake things up?</span>
        <span className="flex-shrink-0 font-mono text-xs font-bold tabular-nums text-indigo-700">
          {formatCountdownTo(new Date(correction.expires_at).getTime())}
        </span>
      </div>
      <button
        type="button"
        onClick={handleTrigger}
        disabled={busy}
        className="self-start rounded bg-indigo-700 px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
      >
        {busy ? "…" : "Trigger Market Correction"}
      </button>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}

/** Card treatment for the pending-pickup decision surface -- composes two
 * independent facts, same principle as the results screen's overlay:
 * border *weight* signals ownership (bold = one of the original five,
 * matches MarketView's own convention, and is the tappable discard
 * target), a ring signals the just-revealed reserve (NEW), and anything
 * that's neither fades quieter as positional-only context. The two can
 * coexist -- the drawn reserve can coincide with an already-owned
 * entity. */
function pendingPickupCardClasses(ownedCount: number, isNew: boolean): string {
  const weight = ownedCount > 0 ? "border-2 border-zinc-900 bg-white" : "border border-zinc-100 bg-zinc-50 opacity-60";
  const ring = isNew ? "ring-4 ring-amber-400" : "";
  return `${weight} ${ring}`.trim();
}

/** The dedicated decision-mode screen while a reserve pickup is pending --
 * present only via view.pending_pickup, injected server-side directly
 * into the frozen cached_view (see engine._handle_pick_up_reserve).
 * Deliberately self-contained: reads only `view`/`pending`, never
 * `useGameEvents` or anything else independently polled, so there's
 * nothing live to leak through the activity ticker or support markers by
 * construction -- see MarketView's early-return call site. Everything
 * else in `view` (market positions, payout chances, holdings) is frozen
 * too, the whole poll response being the cached snapshot, not just this
 * component's own slice -- "only this player freezes" is already true
 * server-side; this view just makes it the *entire* screen instead of
 * one panel buried among live negotiation UI. */
function PendingPickupDecisionView({
  gameId,
  view,
  pending,
  onChanged,
}: {
  gameId: string;
  view: GameView;
  pending: NonNullable<GameView["pending_pickup"]>;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const marketScrollRef = useRef<HTMLDivElement>(null);

  // Center the NEW card on mount so it's not off-screen on a phone --
  // same scroll-centering technique visualizeProposal already uses.
  useEffect(() => {
    const container = marketScrollRef.current;
    const card = container?.querySelector<HTMLElement>(`[data-entity-id="${pending.revealed_entity_id}"]`);
    if (!container || !card) return;
    container.scrollTo({ left: card.offsetLeft + card.offsetWidth / 2 - container.clientWidth / 2, behavior: "smooth" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const myHoldings = (view.holdings ?? []).filter((h) => h.owner_player_id === view.you && h.zone === "portfolio");
  const ownedByEntity = new Map<string, HoldingView[]>();
  for (const h of myHoldings) {
    if (!h.entity_id) continue;
    const list = ownedByEntity.get(h.entity_id) ?? [];
    list.push(h);
    ownedByEntity.set(h.entity_id, list);
  }

  async function handleDiscard(entityId: string) {
    // Two copies of a doubled/anchor holding are fungible -- either one
    // is a legal discard target, it doesn't matter which.
    const holding = ownedByEntity.get(entityId)?.[0];
    if (!holding || busy) return;
    setError(null);
    setBusy(true);
    const result = await submitCommand(
      gameId,
      "DISCARD_HOLDING",
      { pending_pickup_id: pending.pending_pickup_id, holding_id_to_discard: holding.holding_id },
      { onSettled: onChanged },
    );
    setBusy(false);
    if (!result.ok) setError(commandErrorMessage(result.data, "Couldn't discard that holding."));
  }

  async function handleSkip() {
    setError(null);
    setBusy(true);
    const result = await submitCommand(gameId, "DECLINE_PICKUP", { pending_pickup_id: pending.pending_pickup_id }, { onSettled: onChanged });
    setBusy(false);
    if (!result.ok) setError(commandErrorMessage(result.data, "Couldn't skip."));
  }

  return (
    <div className="flex flex-1 flex-col gap-6 bg-zinc-50 px-4 py-6">
      <div className="flex items-center justify-between">
        <Image src="/gotiate-logo.png" alt="Gotiate" width={120} height={87} priority />
        <span className="font-mono text-2xl font-bold tabular-nums text-amber-700">{formatCountdownTo(new Date(pending.decision_deadline_at).getTime())}</span>
      </div>

      <div className="rounded border border-amber-300 bg-amber-50 p-4 text-center">
        <p className="font-medium text-amber-900">You drew {entityLabel(pending.revealed_entity_id, view)}</p>
        <p className="mt-1 text-xs text-amber-800">Tap one of your holdings below to replace it, or Skip.</p>
      </div>

      <div>
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
              const owned = ownedByEntity.get(entity.entity_id) ?? [];
              const isNew = entity.entity_id === pending.revealed_entity_id;
              const tappable = owned.length > 0 && !busy;
              return (
                <button
                  key={entity.entity_id}
                  type="button"
                  data-entity-id={entity.entity_id}
                  onClick={() => tappable && handleDiscard(entity.entity_id)}
                  disabled={!tappable}
                  className={`relative flex h-36 w-28 flex-shrink-0 flex-col items-center gap-1 overflow-hidden rounded p-2 text-center ${pendingPickupCardClasses(owned.length, isNew)}`}
                >
                  <span className={`text-xs ${owned.length > 0 || isNew ? "text-zinc-400" : "text-zinc-300"}`}>{entity.position}</span>
                  <span className={`font-mono text-sm font-bold ${owned.length > 0 || isNew ? "text-zinc-900" : "text-zinc-300"}`}>{entity.ticker_symbol}</span>
                  <span className={`line-clamp-2 text-xs leading-tight ${owned.length > 0 || isNew ? "text-zinc-600" : "text-zinc-300"}`}>{entity.display_name}</span>
                  {owned.length > 1 && <span className="text-xs font-bold text-zinc-900">×{owned.length}</span>}
                  {isNew && <span className="text-[10px] font-bold text-amber-600">NEW</span>}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      <button
        type="button"
        onClick={handleSkip}
        disabled={busy}
        className="rounded border border-zinc-300 bg-white px-4 py-2 text-sm font-medium text-zinc-700 disabled:opacity-50"
      >
        {busy ? "…" : "Skip — keep current portfolio"}
      </button>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}

/** Own still-unrevealed reserves -- each a Pick Up slot (always legal
 * during negotiation, no client-side pre-check) and, while inside the
 * unilateral window, a Burn-for-swap trigger that hands off to
 * MarketView's own card-selection flow. The Burn button being disabled
 * once the window closes IS the final-window lockout indicator -- the
 * server would 400 it anyway, this just surfaces that state up front
 * instead of letting the tap fail silently. */
function ReserveControls({
  gameId,
  view,
  onChanged,
  burningReserveId,
  onStartBurn,
  onCancelBurn,
}: {
  gameId: string;
  view: GameView;
  onChanged: () => void;
  burningReserveId: string | null;
  onStartBurn: (holdingId: string) => void;
  onCancelBurn: () => void;
}) {
  const [pickingUp, setPickingUp] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const myReserves = (view.holdings ?? []).filter((h) => h.owner_player_id === view.you && h.zone === "reserve_unrevealed");
  const canBurn = view.unilateral_cutoff_at !== null && !isPastDeadline(new Date(view.unilateral_cutoff_at).getTime());

  if (myReserves.length === 0) return null;

  async function handlePickUp(holdingId: string) {
    setError(null);
    setPickingUp(holdingId);
    const result = await submitCommand(gameId, "PICK_UP_RESERVE", { reserve_holding_id: holdingId }, { expectedVersion: view.version, onSettled: onChanged });
    setPickingUp(null);
    if (!result.ok) setError(commandErrorMessage(result.data, "Couldn't pick that up."));
  }

  return (
    <div className="rounded border border-zinc-200 bg-white p-3">
      <h2 className="mb-2 text-sm font-medium text-zinc-700">Reserves</h2>
      <ul className="flex flex-col gap-1.5">
        {myReserves.map((h, i) => (
          <li key={h.holding_id} className="flex items-center justify-between gap-2 text-sm text-zinc-900">
            <span>Reserve {i + 1}</span>
            <span className="flex flex-shrink-0 gap-1">
              <button
                type="button"
                onClick={() => handlePickUp(h.holding_id)}
                disabled={pickingUp !== null}
                className="rounded border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 disabled:opacity-50"
              >
                {pickingUp === h.holding_id ? "…" : "Pick up"}
              </button>
              <button
                type="button"
                onClick={() => (burningReserveId === h.holding_id ? onCancelBurn() : onStartBurn(h.holding_id))}
                disabled={!canBurn}
                title={canBurn ? undefined : "The unilateral swap window has closed"}
                className={`rounded border px-2 py-1 text-xs font-medium disabled:opacity-30 ${
                  burningReserveId === h.holding_id ? "border-purple-500 bg-purple-100 text-purple-900" : "border-purple-300 text-purple-700"
                }`}
              >
                {burningReserveId === h.holding_id ? "Cancel burn" : "Burn for swap"}
              </button>
            </span>
          </li>
        ))}
      </ul>
      {!canBurn && <p className="mt-2 text-xs text-zinc-400">The unilateral swap window has closed — Pick Up is still open.</p>}
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
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
  lingeringDeals,
  onSelfWithdrawProposal,
}: {
  gameId: string;
  view: GameView;
  onChanged: () => void;
  onVisualize: (pairs: [string, string, string][]) => void;
  onStartPool: (proposalId: string) => void;
  lingeringDeals: LingeringDeal[];
  onSelfWithdrawProposal: (proposalId: string) => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // My own passes only -- see PassLingeringEntry. Keyed by proposal_id,
  // same as lingeringById below.
  const [passLingering, setPassLingering] = useState<Map<string, PassLingeringEntry>>(new Map());
  const openCount = view.proposals.filter((p) => p.status === "open").length;
  const self = view.players.find((p) => p.game_player_id === view.you);
  const available = self?.influence?.available ?? 0;
  // Keyed by proposal_id/pool_id -- a resolved-but-still-lingering row is
  // found and overlaid in place below, never rendered as a second, freshly
  // -positioned entry (that used to make an accepted deal visually jump to
  // wherever the ghost list happened to render it).
  const lingeringById = new Map(lingeringDeals.map((d) => [d.key, d]));

  function addPassLingering(proposalId: string, entityA: string, entityB: string, proposerId: string) {
    setPassLingering((prev) => new Map(prev).set(proposalId, { proposalId, entityA, entityB, proposerId, fading: false }));
    setTimeout(() => {
      setPassLingering((prev) => {
        const entry = prev.get(proposalId);
        if (!entry) return prev;
        return new Map(prev).set(proposalId, { ...entry, fading: true });
      });
    }, 2200);
    setTimeout(() => {
      setPassLingering((prev) => {
        if (!prev.has(proposalId)) return prev;
        const next = new Map(prev);
        next.delete(proposalId);
        return next;
      });
    }, 2900);
  }

  // Rows are a pure computation from current props/state every render --
  // deliberately *not* persisted/reconciled in a separate effect. An
  // earlier version kept a `rowOrder` state array reconciled via its own
  // effect, which introduced a real bug: this component's effects run
  // before its parent's (children-before-parent commit ordering), so on
  // the very render a proposal resolved, this component's reconciliation
  // effect always ran against the *previous* lingeringDeals (MarketView's
  // own detection effect hadn't updated it yet) -- concluding the
  // now-resolved proposal was neither open nor lingering, and dropping it
  // from `rowOrder` for good. Once dropped, nothing ever re-added it (the
  // reconciliation only ever appended from the *open* set), so the
  // overlay silently never appeared even though lingeringDeals correctly
  // grew a moment later. A pure per-render computation can't get stuck
  // that way: the row simply isn't included on a render where neither
  // condition holds yet, and *is* included the moment either becomes true,
  // however many renders that takes.
  //
  // view.proposals itself already provides stable, never-reordered
  // positions for every proposal (dict-insertion order, server-side) that
  // remains present for this player -- so filtering it directly, in place,
  // is what keeps an accepted/withdrawn/everyone-passed row from jumping
  // elsewhere. The one proposal that's never present here to filter is one
  // *this player themselves* just passed on -- Pass omits it from their own
  // view.proposals immediately and permanently (see the Pass design
  // writeup), unlike every other resolution -- so passLingering entries
  // are rendered separately, prepended at the top rather than trying to
  // reconstruct a position view.proposals no longer has any record of.
  const visibleProposals = view.proposals.filter((p) => p.status === "open" || lingeringById.has(p.proposal_id));
  const passLingeringEntries = [...passLingering.values()];

  async function runCommand(id: string, type: string, payload: Record<string, unknown>, fallback: string) {
    setError(null);
    setBusyId(id);
    const result = await submitCommand(gameId, type, payload, { expectedVersion: view.version, onSettled: onChanged });
    setBusyId(null);
    if (!result.ok) setError(commandErrorMessage(result.data, fallback));
    return result;
  }

  if (visibleProposals.length === 0 && passLingeringEntries.length === 0) return null;

  return (
    <div>
      <h2 className="mb-2 text-sm font-medium text-zinc-700">Open proposals ({openCount})</h2>
      <ul className="flex flex-col gap-2 rounded border border-zinc-200 bg-white p-3 text-sm">
        {passLingeringEntries.map((passEntry) => (
          <li
            key={passEntry.proposalId}
            className="relative flex items-center justify-between gap-2 border-b border-zinc-100 pb-2 text-zinc-500 last:border-0 last:pb-0"
          >
            <span>
              {playerLabel(passEntry.proposerId, view)}: {entityLabel(passEntry.entityA, view)} ↔ {entityLabel(passEntry.entityB, view)}
            </span>
            <RowOverlay text="You Passed" tone="yellow" fading={passEntry.fading} />
          </li>
        ))}
        {visibleProposals.map((p) => {
          const isMine = p.proposer_id === view.you;
          const lingering = lingeringById.get(p.proposal_id);
          // Mirrors the server's own rule (PASS/CREATE_POOL both reject
          // while I already hold an open Pool of my own on this proposal)
          // -- Pool/Pass simply aren't offered in that state instead of
          // being offered and then bouncing off a server rejection.
          const myOpenPoolOnThis = view.pools.some(
            (pool) => pool.base_proposal_id === p.proposal_id && pool.initiator_id === view.you && pool.status === "open",
          );
          const pools = view.pools.filter(
            (pool) => pool.base_proposal_id === p.proposal_id && (pool.status === "open" || lingeringById.has(pool.pool_id)),
          );
          return (
            <li
              key={p.proposal_id}
              // Visualize used to be a separate button players had to
              // remember to press every time, then just hovering/tapping the
              // swap text -- now the whole row triggers it, since a player's
              // cursor lands anywhere on the row while reading it, not just
              // on the text itself. Not wired up at all once resolved --
              // nothing left to evaluate.
              onMouseEnter={lingering ? undefined : () => onVisualize([[p.entity_a, p.entity_b, p.rising_entity_id]])}
              onClick={lingering ? undefined : () => onVisualize([[p.entity_a, p.entity_b, p.rising_entity_id]])}
              title={lingering ? undefined : "Hover or tap to visualize"}
              className={`relative flex flex-col gap-1.5 border-b border-zinc-100 pb-2 text-zinc-900 last:border-0 last:pb-0 ${
                lingering ? "" : "cursor-pointer hover:bg-zinc-50"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span>
                  {playerLabel(p.proposer_id, view)}: {entityLabel(p.entity_a, view)} ↔ {entityLabel(p.entity_b, view)}
                  {/* Self-only, proposer-only, anonymous -- the proposer's one
                      and only channel to Pass feedback. See the Pass design
                      writeup: never identities, never shown to anyone else. */}
                  {isMine && p.passed_count !== undefined && (
                    <span className="ml-2 text-xs font-normal text-zinc-400">Passed: {p.passed_count}</span>
                  )}
                </span>
                {!lingering && (
                  <span className="flex flex-shrink-0 gap-1">
                    {!isMine && !myOpenPoolOnThis && (
                      <button
                        type="button"
                        onClick={() => onStartPool(p.proposal_id)}
                        className="rounded border border-purple-300 px-2 py-1 text-xs font-medium text-purple-700"
                      >
                        Pool
                      </button>
                    )}
                    {!isMine && !myOpenPoolOnThis && (
                      // No confirmation -- treated like Accept, one deliberate
                      // tap. On success (and only then), lingers this exact
                      // row under a "You Passed" overlay for a beat -- see
                      // addPassLingering. entity_a/b/proposer_id are captured
                      // *before* the request, since a successful Pass omits
                      // this proposal from view.proposals on the very next
                      // poll, taking the server's own copy of that data with
                      // it.
                      <button
                        type="button"
                        onClick={async () => {
                          const { entity_a: entityA, entity_b: entityB, proposer_id: proposerId } = p;
                          const result = await runCommand(p.proposal_id, "PASS_PROPOSAL", { proposal_id: p.proposal_id }, "Couldn't pass.");
                          if (result.ok) addPassLingering(p.proposal_id, entityA, entityB, proposerId);
                        }}
                        disabled={busyId === p.proposal_id}
                        className="rounded border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 disabled:opacity-50"
                      >
                        {busyId === p.proposal_id ? "…" : "Pass"}
                      </button>
                    )}
                    {isMine ? (
                      <button
                        type="button"
                        onClick={() => {
                          // Recorded *before* the request settles -- see
                          // MarketView's myExplicitWithdrawalsRef -- so the
                          // event-processing effect never has to race it.
                          onSelfWithdrawProposal(p.proposal_id);
                          runCommand(p.proposal_id, "WITHDRAW_PROPOSAL", { proposal_id: p.proposal_id }, "Couldn't withdraw.");
                        }}
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
                )}
              </div>

              {pools.length > 0 && (
                <ul className="ml-3 flex flex-col gap-1 border-l border-zinc-200 pl-3">
                  {pools.map((pool) => {
                    const isPoolMine = pool.initiator_id === view.you;
                    const visible = Boolean(pool.entity_c && pool.entity_d);
                    const poolLingering = lingeringById.get(pool.pool_id);
                    const visualizePoolLeg =
                      visible && !poolLingering
                        ? () =>
                            onVisualize([
                              [p.entity_a, p.entity_b, p.rising_entity_id],
                              [pool.entity_c!, pool.entity_d!, pool.rising_entity_id!],
                            ])
                        : undefined;
                    return (
                      <li
                        key={pool.pool_id}
                        onMouseEnter={visualizePoolLeg}
                        onClick={visualizePoolLeg}
                        title={visualizePoolLeg ? "Hover or tap to visualize" : undefined}
                        className={`relative flex items-center justify-between gap-2 text-xs text-zinc-700 ${visualizePoolLeg ? "cursor-pointer hover:bg-zinc-50" : ""}`}
                      >
                        <span>
                          {playerLabel(pool.initiator_id, view)} pooled {pool.visibility}
                          {visible ? `: ${entityLabel(pool.entity_c!, view)} ↔ ${entityLabel(pool.entity_d!, view)}` : " (hidden)"}
                        </span>
                        {!poolLingering && (
                          <span className="flex flex-shrink-0 gap-1">
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
                        )}
                        {/* "accepted" only -- ACCEPT_POOL always resolves the base
                            proposal to executed in the same instant, so the outer
                            <li>'s own overlay below already covers this row as part
                            of the same group; a second overlay here would just
                            double up the same "Accepted by X" text. WITHDRAW_POOL
                            is different: it only ever resolves the pool itself, never
                            touching the base proposal, so a withdrawn pool needs its
                            own overlay -- there's no outer one to rely on. */}
                        {poolLingering?.kind === "withdrawn" && <RowOverlay {...lingeringOverlayProps(poolLingering)} fading={poolLingering.fading} />}
                      </li>
                    );
                  })}
                </ul>
              )}
              {lingering && <RowOverlay {...lingeringOverlayProps(lingering)} fading={lingering.fading} />}
            </li>
          );
        })}
      </ul>
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  );
}

const _ROW_OVERLAY_TONE_CLASSES = {
  green: "bg-emerald-500/25 text-emerald-900",
  yellow: "bg-amber-400/30 text-amber-900",
  red: "bg-red-500/25 text-red-900",
  gray: "bg-zinc-500/25 text-zinc-700",
} as const;

/** "accepted" renders "Accepted" (no name) until the enriching event
 * arrives and fills in accepterLabel -- see MarketView's enrichment
 * effect. Usually only visible for a single poll cycle or two. */
function lingeringOverlayProps(lingering: LingeringDeal): { text: string; tone: keyof typeof _ROW_OVERLAY_TONE_CLASSES } {
  if (lingering.kind === "accepted") {
    return { text: lingering.accepterLabel ? `Accepted by ${lingering.accepterLabel}` : "Accepted", tone: "green" };
  }
  if (lingering.kind === "everyone_passed") {
    return { text: "Everyone Passed", tone: "red" };
  }
  return { text: "Withdrawn", tone: "gray" };
}

/** Translucent banner overlaid on a proposal/pool row's own, still-in-place
 * `<li>` (which must be `position: relative`) once it resolves in a way
 * worth calling out -- covers the whole row rather than replacing it, so
 * nothing shifts position while it lingers and fades. */
function RowOverlay({ text, tone, fading }: { text: string; tone: keyof typeof _ROW_OVERLAY_TONE_CLASSES; fading: boolean }) {
  return (
    <div
      className={`pointer-events-none absolute inset-0 flex items-center justify-center rounded text-sm font-bold transition-opacity duration-700 ${
        _ROW_OVERLAY_TONE_CLASSES[tone]
      } ${fading ? "opacity-0" : "opacity-100"}`}
    >
      {text}
    </div>
  );
}

const _ACTIVITY_EVENT_TYPES = new Set([
  "PROPOSAL_CREATED",
  "PROPOSAL_RESOLVED",
  "PRIVATE_POOL_CREATED",
  "PUBLIC_POOL_CREATED",
  "POOL_RESOLVED",
  // Game-lifecycle events -- narrative/informational, so only surfaced
  // under the "All" filter, never the default "Executed" one (they're
  // not a proposal/pool resolving, so matchesActivityFilter's "executed"
  // branch already excludes them regardless of this set).
  "CLOSE_THRESHOLD_REACHED",
  "MARKET_CLOSED",
  "PORTFOLIOS_REVEALED",
  "GAME_SCORED",
  "GAME_ENDED",
  // 2-player-only anti-stagnation mechanic -- narrative/informational like
  // the lifecycle events above, so only surfaced under "All" (matchesActivityFilter's
  // "executed" branch only ever matches PROPOSAL_RESOLVED/POOL_RESOLVED).
  // See the Market Correction design writeup.
  "MARKET_CORRECTION_OFFERED",
  "MARKET_CORRECTION_RESOLVED",
]);
const _TONE_CLASSES: Record<string, string> = {
  open: "text-blue-700",
  success: "text-emerald-700",
  muted: "text-zinc-500",
  warning: "text-amber-700",
  // Distinct from every other tone above (not blue/emerald/zinc/amber, and
  // not the market-card badges' emerald/purple either) -- a correction is
  // unmistakably not a player-authored proposal, pool, or burn. See the
  // Market Correction design writeup.
  correction: "text-indigo-700",
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
    if (reason === "executed") {
      const proposer = playerLabel(proposal?.proposer_id ?? null, view);
      const accepter = playerLabel(event.actor_game_player_id, view);
      return { text: `${swap} — ${proposer} proposed, ${accepter} accepted`, tone: "success" };
    }
    if (reason === "withdrawn_by_initiator") return { text: `${swap} — withdrawn`, tone: "muted" };
    if (reason === "market_closed") return { text: `${swap} — expired, market closed`, tone: "warning" };
    // Deliberately loud, never masked (the opposite of Pass) -- the
    // market moved far enough that the proposed direction was no longer
    // valid. See the market-direction-reversal design writeup.
    if (reason === "voided_market_swung") return { text: `${swap} — voided, market swung`, tone: "warning" };
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
    if (reason === "executed") {
      const initiator = playerLabel(pool?.initiator_id ?? null, view);
      const accepter = playerLabel(event.actor_game_player_id, view);
      return { text: `${swap} — ${initiator} pooled, ${accepter} accepted`, tone: "success" };
    }
    if (reason === "withdrawn_by_initiator") return { text: `${swap} — pool withdrawn`, tone: "muted" };
    if (reason === "declined_by_target") return { text: `${swap} — pool declined`, tone: "muted" };
    if (reason === "base_proposal_withdrawn") return { text: `${swap} — base proposal was withdrawn`, tone: "muted" };
    if (reason === "invalidated_by_initiator_action") return { text: `${swap} — pool abandoned by its own initiator`, tone: "muted" };
    if (reason === "preempted_by_other_action" && pool) return { text: `${swap} — ${describePreemption(pool, view)}`, tone: "warning" };
    if (reason === "market_closed") return { text: `${swap} — expired, market closed`, tone: "warning" };
    if (reason === "voided_market_swung") return { text: `${swap} — voided, market swung`, tone: "warning" };
    if (reason === "base_proposal_voided") return { text: `${swap} — base proposal voided, market swung`, tone: "warning" };
    return { text: `${swap} — pool resolved`, tone: "muted" };
  }
  if (event.type === "CLOSE_THRESHOLD_REACHED") {
    // Deliberately doesn't echo the payload's numeric `count` -- readiness
    // stays a secret trigger, not a public countdown, right up until this
    // exact instant; even here the reveal is the closing itself, not a
    // number. See the Stage 6 design writeup.
    return { text: "Ready threshold reached", tone: "warning" };
  }
  if (event.type === "MARKET_CLOSED") {
    const reason = event.payload.reason as string;
    if (reason === "TIME_EXPIRED") return { text: "Market closed — time ran out", tone: "warning" };
    if (reason === "READY_THRESHOLD") return { text: "Market closed — ready threshold reached", tone: "warning" };
    return { text: "Market closed", tone: "warning" };
  }
  if (event.type === "PORTFOLIOS_REVEALED") return { text: "Portfolios revealed", tone: "open" };
  if (event.type === "GAME_SCORED" || event.type === "GAME_ENDED") return { text: "Game scored", tone: "success" };
  if (event.type === "MARKET_CORRECTION_OFFERED") {
    return { text: "Market Correction available", tone: "correction" };
  }
  if (event.type === "MARKET_CORRECTION_RESOLVED") {
    const reason = event.payload.reason as string;
    if (reason === "triggered") {
      // moves is only ever present live when reason === "triggered" --
      // projections.project_events redacts it for every other reason. Each
      // line reads the entity's own name plus its old -> new position;
      // positions have already swapped by the time this event is walked,
      // so entity_b's *current* position is where entity_a used to sit.
      const moves = (event.payload.moves as { target_player_id: string; entity_a: string; entity_b: string }[] | undefined) ?? [];
      const lines = moves.map((m) => {
        const oldPosition = positionOf(m.entity_b, view);
        const newPosition = positionOf(m.entity_a, view);
        return `${entityLabel(m.entity_a, view)} #${oldPosition ?? "?"} → #${newPosition ?? "?"} — market correction`;
      });
      return { text: ["Market Correction triggered", ...lines].join("\n"), tone: "correction" };
    }
    if (reason === "expired") return { text: "Market Correction expired unused", tone: "correction" };
    if (reason === "invalidated") return { text: "Market Correction cancelled — the market moved", tone: "correction" };
    if (reason === "market_resumed") return { text: "Market Correction cancelled — trading resumed", tone: "correction" };
    return { text: "Market Correction resolved", tone: "correction" };
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

/** Zone-specific "what you never ended up using" copy for the reveal
 * section below -- postgame is deliberately maximally transparent (see
 * the Stage 6/7 design writeup): a discarded holding was seen and let
 * go, a surrendered-unused reserve was never picked up at all, a
 * pickup-surrendered one was seen but lost to the clock, and a
 * burned-unseen one -- the "oh, I burned Motorboat" beat -- was never
 * seen even by its own owner until this exact screen. */
function neverUsedLabel(zone: string): string {
  if (zone === "discarded") return "discarded after Pick Up";
  if (zone === "surrendered_unused") return "never picked up";
  if (zone === "pickup_surrendered") return "revealed but lost to the clock";
  if (zone === "burned_unseen") return "burned for a swap, never seen";
  return zone;
}

/** Card treatment for the results-screen market overlay -- composes three
 * independent facts rather than picking one winner-take-all style: border
 * *weight* signals ownership (bold = in the selected player's final
 * portfolio, matches MarketView's own ownership convention), border
 * *color* signals wiped (red, always, regardless of who's selected --
 * "wiped positions remain visibly wiped independent of which player is
 * selected"), and a dashed style plus muted fill marks a never-used
 * reveal (burned/discarded/surrendered) when the position isn't also a
 * live portfolio holding. Anything irrelevant to the selected player
 * fades back rather than disappearing, per the "remain visible as
 * context, but visually quieter" requirement. */
function resultsCardClasses(isWiped: boolean, ownedCount: number, hasNeverUsed: boolean): string {
  const weight = ownedCount > 0 ? "border-2" : hasNeverUsed ? "border border-dashed" : "border";
  if (isWiped) return `${weight} border-red-300 bg-red-50`;
  if (ownedCount > 0) return `${weight} border-zinc-900 bg-white`;
  if (hasNeverUsed) return `${weight} border-zinc-400 bg-zinc-100`;
  return `${weight} border-zinc-100 bg-zinc-50 opacity-50`;
}

/** Stage 7 — the results screen. No new backend data needed:
 * compute_final_scores' result already merges into project() at SCORED
 * (realized_haircut_depth/wiped_entity_ids/results/winners), and
 * holdings are unconditionally revealed across every zone once scored
 * (confirmed directly against _holding_view's default reveal=True) --
 * this is purely a rendering pass over data the backend already hands
 * over in full. The leaderboard is the *only* selector -- clicking a
 * name projects that player's final holdings directly onto the one
 * shared Final Market strip below it, rather than each player getting
 * their own separate text block (real playtest feedback: a stacked
 * per-player list forced the reader to mentally re-map it onto the
 * market strip that was already right there). */
function ResultsView({ gameId, view }: { gameId: string; view: GameView }) {
  const { events } = useGameEvents(gameId);
  const results = view.results ?? [];
  const winners = new Set(view.winners ?? []);
  const depth = view.realized_haircut_depth ?? 0;
  const wiped = new Set(view.wiped_entity_ids ?? []);
  const holdings = view.holdings ?? [];
  const sortedResults = [...results].sort((a, b) => b.final_value - a.final_value);
  const topValue = sortedResults[0]?.final_value;
  const closeReasonText =
    view.close_reason === "TIME_EXPIRED" ? "Time ran out." : view.close_reason === "READY_THRESHOLD" ? "Ready threshold reached." : null;

  // Default = your own row, not the winner's -- the screen should open on
  // "how did I do", not congratulate whoever won regardless of who's
  // looking (real playtest feedback: every player was seeing the winner
  // highlighted by default). Falls back to the winner (ties keep whichever
  // one sorted first, per compute_final_scores' own player-iteration order
  // -- stable sort preserves that), then the top-ranked player, for a
  // spectator with no seat (view.you is null) or if winners is ever empty
  // (defensive; shouldn't happen once scored).
  const [selectedOverride, setSelectedOverride] = useState<string | null>(null);
  const defaultSelectedId =
    sortedResults.find((r) => r.game_player_id === view.you)?.game_player_id ??
    sortedResults.find((r) => winners.has(r.game_player_id))?.game_player_id ??
    sortedResults[0]?.game_player_id ??
    null;
  const selectedPlayerId = selectedOverride ?? defaultSelectedId;
  const selectedResult = results.find((r) => r.game_player_id === selectedPlayerId);

  // Grouped once per selection, not per card -- portfolio holdings counted
  // for the ×N badge (a doubled/anchor entity), every other zone kept as
  // its own list (rare, but two never-used holdings can reference the
  // same entity) so each gets its own small caption below.
  const portfolioCounts = new Map<string, number>();
  const neverUsedByEntity = new Map<string, HoldingView[]>();
  for (const h of holdings) {
    if (h.owner_player_id !== selectedPlayerId || !h.entity_id) continue;
    if (h.zone === "portfolio") {
      portfolioCounts.set(h.entity_id, (portfolioCounts.get(h.entity_id) ?? 0) + 1);
    } else {
      const list = neverUsedByEntity.get(h.entity_id) ?? [];
      list.push(h);
      neverUsedByEntity.set(h.entity_id, list);
    }
  }

  return (
    <div className="flex flex-1 flex-col gap-6 bg-zinc-50 px-4 py-6">
      <div className="flex items-center justify-between">
        <Image src="/gotiate-logo.png" alt="Gotiate" width={120} height={87} priority />
        <Link href="/" className="text-sm font-medium text-zinc-700 underline">
          Play again
        </Link>
      </div>

      <div className="rounded border border-zinc-200 bg-white p-4 text-center">
        <h1 className="text-lg font-bold text-zinc-900">Game over</h1>
        {closeReasonText && <p className="text-sm text-zinc-500">{closeReasonText}</p>}
        <p className="mt-2 text-base font-medium text-emerald-700">
          {winners.size > 1
            ? `${[...winners].map((id) => playerLabel(id, view)).join(" & ")} tie at ${topValue}!`
            : `${playerLabel([...winners][0] ?? null, view)} wins with ${topValue}!`}
        </p>
        <p className="mt-1 text-xs text-zinc-500">{depth > 0 ? `Positions 1–${depth} were wiped by the realized risk.` : "Nothing was wiped this game."}</p>
      </div>

      <div>
        <h2 className="mb-2 text-sm font-medium text-zinc-700">Leaderboard</h2>
        <ul className="flex flex-col gap-1 rounded border border-zinc-200 bg-white p-3">
          {sortedResults.map((r) => {
            const isWinner = winners.has(r.game_player_id);
            const isSelected = r.game_player_id === selectedPlayerId;
            return (
              <li key={r.game_player_id}>
                <button
                  type="button"
                  onClick={() => setSelectedOverride(r.game_player_id)}
                  className={`flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-sm text-zinc-900 ${
                    isSelected ? "bg-blue-50 ring-1 ring-blue-400" : ""
                  }`}
                >
                  <span className={isWinner ? "font-semibold text-emerald-700" : undefined}>
                    {isWinner ? "🏆 " : ""}
                    {playerLabel(r.game_player_id, view)}
                    {r.game_player_id === view.you ? " - You" : ""}
                  </span>
                  <span className="tabular-nums text-zinc-700">{r.final_value}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      <div>
        <h2 className="mb-2 text-sm font-medium text-zinc-700">
          {playerLabel(selectedPlayerId, view)}&apos;s final position — <span className="tabular-nums">{selectedResult?.final_value ?? "—"}</span>
        </h2>
        <div className="-mx-4 overflow-x-auto px-4 pb-2">
          <div className="flex gap-2">
            {view.market.map((entity) => {
              const isWiped = wiped.has(entity.entity_id);
              const ownedCount = portfolioCounts.get(entity.entity_id) ?? 0;
              const neverUsed = neverUsedByEntity.get(entity.entity_id) ?? [];
              const muted = ownedCount === 0 && neverUsed.length === 0 && !isWiped;
              // The realized score for this position -- 0 if it fell within
              // the wiped depth, otherwise the same public linear_rank_v1
              // formula projected_value/the live market view already use.
              // Never a second source of truth: purely a display of a
              // deterministic formula over already-public position/wipe
              // data, same principle as haircutRisk.ts.
              const points = isWiped ? 0 : view.market.length - entity.position + 1;
              return (
                <div
                  key={entity.entity_id}
                  className={`flex h-40 w-28 flex-shrink-0 flex-col items-center gap-1 overflow-hidden rounded p-2 text-center ${resultsCardClasses(isWiped, ownedCount, neverUsed.length > 0)}`}
                >
                  <span className={`text-xs ${muted ? "text-zinc-300" : "text-zinc-400"}`}>
                    #{entity.position} · {points}pt{points === 1 ? "" : "s"}
                  </span>
                  <span className={`font-mono text-sm font-bold ${muted ? "text-zinc-300" : "text-zinc-900"}`}>{entity.ticker_symbol}</span>
                  <span className={`line-clamp-2 text-xs leading-tight ${muted ? "text-zinc-300" : "text-zinc-600"}`}>{entity.display_name}</span>
                  {ownedCount > 1 && <span className="text-xs font-bold text-zinc-900">×{ownedCount}</span>}
                  {isWiped && <span className="text-[10px] font-bold text-red-600">WIPED</span>}
                  {/* mt-auto -- bottom-justified within the card's fixed
                      height (not wherever the flow of shorter content above
                      happens to end), so these notes (discarded/never
                      -picked-up/burned-unseen -- the "what could have been"
                      reveals) don't get lost floating mid-card. */}
                  {neverUsed.length > 0 && (
                    <div className="mt-auto flex flex-col items-center gap-0.5">
                      {neverUsed.map((h, i) => (
                        <span key={i} className="text-[9px] font-medium leading-tight text-zinc-500">
                          {neverUsedLabel(h.zone)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <ActivityStream events={events} view={view} />
    </div>
  );
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
              <li key={event.seq_no} className={`whitespace-pre-line ${_TONE_CLASSES[tone]}`}>
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
        <Image src="/gotiate-logo.png" alt="Gotiate" width={120} height={87} priority />
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
