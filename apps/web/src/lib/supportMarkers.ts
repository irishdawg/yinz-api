import type { EventView } from "./useGameEvents";
import type { PoolView } from "./useGameView";

export interface SupportEntry {
  playerId: string;
  count: number;
  // A distinct public signal from negotiated support -- see the
  // unilateral-marker design writeup: "I burned one of my two mystery
  // bullets and made this happen myself" is a stronger, different
  // statement than "I agreed to this movement with someone." Can coexist
  // with `count` on the same player/entity; one doesn't overwrite the
  // other.
  unilateralCount: number;
}

export type SupportMarkers = Map<string, SupportEntry[]>;

/** One unclaimed SWAP_EXECUTED occurrence -- a unilateral reserve burn (or,
 * in principle, an unclaimed Market Correction leg, but those are always
 * claimed by MARKET_CORRECTION_RESOLVED(triggered) before this point, see
 * the "Market Correction never credits support markers" note below). Same
 * data computeSupportMarkers already derives for its own unilateralCount
 * bump -- see findUnilateralSwaps, which shares that exact pass rather
 * than re-deriving it. */
export interface UnilateralSwapEvent {
  entityA: string;
  entityB: string;
  positionA: number;
  positionB: number;
  actorId: string;
  // The originating SWAP_EXECUTED event's own seq_no -- a stable identity
  // for a specific occurrence, since the same entity pair can legitimately
  // swap unilaterally more than once in a game (entityA/entityB alone
  // aren't unique).
  seqNo: number;
}

function pairKey(a: string, b: string): string {
  return [a, b].sort().join("::");
}

/** Derives, from the public event log alone, which players have publicly
 * and successfully pushed each market entity up. The governing principle:
 * a player receives an up-support marker on every upward movement
 * contained in the package they affirmatively acted upon, where "acted
 * upon" scopes to what that specific action encompassed --
 *   - proposing a bare swap, or accepting one: that leg's rise only.
 *   - creating a Pool against a base proposal: an affirmative "I support
 *     this entire package, conditional on my own leg too" -- both the
 *     base leg's rise and the pool leg's rise.
 *   - accepting a Pool (private or public): executes the whole bundle --
 *     both legs' rises.
 *   - a unilateral BURN_RESERVE_FOR_SWAP: see below -- its own distinct
 *     `unilateralCount`, not folded into ordinary `count`.
 * A player who has any existing marker (either kind) on the entity that
 * *fell* has it cleared entirely (not decremented) -- an "up" only goes
 * away once that same player helps the same entity back down, regardless
 * of which mechanism originally created it. Withdrawn/expired/declined/
 * preempted/voided resolutions never touch a marker. Movement magnitude
 * is irrelevant, and counts are never capped here -- only the display
 * layer caps at 3+. Direction is derived by comparing the two entities'
 * *final* positions against each other post-swap (lower position =
 * stronger rank = rose) -- no need for pre-swap positions.
 *
 * A Pool's ACCEPT_POOL execution fires two independent resolution events
 * -- PROPOSAL_RESOLVED for the base leg, POOL_RESOLVED for the pool's own
 * leg. Per the principle above, the *pool's initiator* must additionally
 * be credited on the base leg's PROPOSAL_RESOLVED (they didn't just
 * author their own leg, they affirmatively endorsed the base leg by
 * pooling against it) -- winningPoolInitiatorByBaseProposalId supplies
 * that lookup. The base *proposer* is never credited on the pool leg
 * unless they're also the accepter (the private-pool self-accept case),
 * which falls out naturally rather than needing a special case. All
 * credit lists get deduped through creditLeg's Set regardless of which
 * roles happen to coincide in a given player.
 *
 * **Unilateral detection.** BURN_RESERVE_FOR_SWAP never fires
 * PROPOSAL_RESOLVED/POOL_RESOLVED -- it's the only one of
 * engine._execute_swap's 4 call sites that doesn't (accept-proposal and
 * both accept-pool legs unconditionally emit a matching resolution for
 * the exact same entity pair, every time). So a SWAP_EXECUTED with no
 * matching resolution is structurally guaranteed to be a unilateral
 * burn, not inferred from a heuristic. Tracked via a per-pair *queue*
 * (not a flat claimed/unclaimed set) since the same pair can legitimately
 * swap more than once over a game -- each SWAP_EXECUTED is queued with
 * its own position snapshot, and a resolution for that exact pair pops
 * (claims) the most recent one, LIFO. Whatever's left in any queue after
 * the full event walk gets credited unilaterally, using its own recorded
 * positions rather than a shared "most recent swap" lookup, so repeated
 * unclaimed swaps of the same pair never cross-contaminate each other's
 * direction. RESERVE_BURNED_FOR_SWAP itself stays SERVER_ONLY throughout
 * -- this never needs it, or any event-visibility change: SWAP_EXECUTED's
 * actor is already, safely, the burner (for a burn, the ephemeral
 * SwapIntent's initiator_player_id is the actor themselves, unlike an
 * *accepted* proposal/pool swap where that same field is the original
 * author, not whoever's command triggered it) -- entities, positions, and
 * actor are all already public with zero mention of which reserve funded
 * it.
 *
 * **Market Correction never credits support markers, ever.** A
 * corrective move is real (it goes through the same SWAP_EXECUTED /
 * engine._execute_swap choke point as everything else here), but nobody
 * authored its direction -- so a triggered correction's two swaps must
 * be *claimed*, exactly like an executed proposal/pool leg claims its
 * own swap, WITHOUT crediting any player. MARKET_CORRECTION_RESOLVED's
 * payload only ever carries its `moves` live when reason === "triggered"
 * (see projections.project_events' bespoke redaction) -- expired/
 * invalidated/market_resumed corrections never reveal what they would
 * have done, so there's nothing to claim for those, and their swaps
 * never happened anyway. If a triggering player needs attribution
 * in the ticker, it comes from MARKET_CORRECTION_RESOLVED's own
 * actor_game_player_id, entirely separate from this function.
 *
 * `pools` (the live view.pools, not the event log) is the source of a
 * pool's own entities/initiator/base_proposal_id -- not the
 * PRIVATE_POOL_CREATED event's payload, which a non-insider's client may
 * have already cached in redacted form (no entity_c/entity_d) before the
 * pool executed and became publicly visible. view.pools is refetched
 * every poll, so it always reflects current visibility, regardless of
 * when this specific client first saw the creation event. */
function _walkEvents(events: EventView[], pools: PoolView[]): { markers: SupportMarkers; unilateralSwaps: UnilateralSwapEvent[] } {
  const support = new Map<string, Map<string, { count: number; unilateralCount: number }>>();
  const proposalsById = new Map<string, { proposerId: string; entityA: string; entityB: string }>();
  const poolsById = new Map<string, { initiatorId: string; entityC: string; entityD: string }>();
  for (const pool of pools) {
    if (pool.entity_c && pool.entity_d) {
      poolsById.set(pool.pool_id, { initiatorId: pool.initiator_id, entityC: pool.entity_c, entityD: pool.entity_d });
    }
  }
  // At most one pool per base proposal ever resolves "executed" -- every
  // sibling gets preempted/invalidated instead, so this is unambiguous.
  const winningPoolInitiatorByBaseProposalId = new Map<string, string>();
  for (const pool of pools) {
    if (pool.resolution_reason === "executed") {
      winningPoolInitiatorByBaseProposalId.set(pool.base_proposal_id, pool.initiator_id);
    }
  }
  const lastSwapByPair = new Map<string, { entityA: string; entityB: string; positionA: number; positionB: number }>();
  // Per-pair queue of not-yet-claimed SWAP_EXECUTED occurrences -- see the
  // "Unilateral detection" note above. Each entry carries its own
  // position snapshot so a later unilateral credit never depends on
  // lastSwapByPair (which only ever holds the *most recent* swap for a
  // pair, wrong if the same pair swaps unilaterally more than once).
  const pendingSwapsByPair = new Map<string, { entityA: string; entityB: string; positionA: number; positionB: number; actorId: string | null; seqNo: number }[]>();

  function bump(entityId: string, playerId: string, kind: "support" | "unilateral") {
    const perEntity = support.get(entityId) ?? new Map<string, { count: number; unilateralCount: number }>();
    const entry = perEntity.get(playerId) ?? { count: 0, unilateralCount: 0 };
    if (kind === "support") entry.count += 1;
    else entry.unilateralCount += 1;
    perEntity.set(playerId, entry);
    support.set(entityId, perEntity);
  }
  function clear(entityId: string, playerId: string) {
    support.get(entityId)?.delete(playerId);
  }
  function creditLeg(risingEntity: string, fallingEntity: string, participants: (string | null | undefined)[]) {
    const uniquePlayerIds = new Set(participants.filter((p): p is string => Boolean(p)));
    for (const playerId of uniquePlayerIds) {
      bump(risingEntity, playerId, "support");
      clear(fallingEntity, playerId);
    }
  }
  function directionOf(entityA: string, entityB: string): { rising: string; falling: string } | null {
    const swap = lastSwapByPair.get(pairKey(entityA, entityB));
    if (!swap) return null;
    const rising = swap.positionA < swap.positionB ? swap.entityA : swap.entityB;
    return { rising, falling: rising === swap.entityA ? swap.entityB : swap.entityA };
  }
  function queueSwap(entityA: string, entityB: string, positionA: number, positionB: number, actorId: string | null, seqNo: number) {
    const key = pairKey(entityA, entityB);
    const queue = pendingSwapsByPair.get(key) ?? [];
    queue.push({ entityA, entityB, positionA, positionB, actorId, seqNo });
    pendingSwapsByPair.set(key, queue);
  }
  function claimSwap(entityA: string, entityB: string) {
    pendingSwapsByPair.get(pairKey(entityA, entityB))?.pop();
  }

  for (const event of events) {
    if (event.type === "PROPOSAL_CREATED" && event.actor_game_player_id) {
      proposalsById.set(event.payload.proposal_id as string, {
        proposerId: event.actor_game_player_id,
        entityA: event.payload.entity_a as string,
        entityB: event.payload.entity_b as string,
      });
    } else if (event.type === "SWAP_EXECUTED") {
      const entityA = event.payload.entity_a as string;
      const entityB = event.payload.entity_b as string;
      const positionA = event.payload.position_a as number;
      const positionB = event.payload.position_b as number;
      // Overwritten on every occurrence of this exact pair -- since events
      // are walked in seq order and a swap's own resolution always follows
      // it immediately (same command), the map always holds the swap that
      // belongs to whichever resolution reads it next.
      lastSwapByPair.set(pairKey(entityA, entityB), { entityA, entityB, positionA, positionB });
      queueSwap(entityA, entityB, positionA, positionB, event.actor_game_player_id, event.seq_no);
    } else if (event.type === "PROPOSAL_RESOLVED" && event.payload.reason === "executed") {
      const proposalId = event.payload.proposal_id as string;
      const proposal = proposalsById.get(proposalId);
      const accepterId = event.actor_game_player_id;
      if (!proposal || !accepterId) continue;
      const direction = directionOf(proposal.entityA, proposal.entityB);
      if (!direction) continue;
      // If this base leg resolved as part of an executed Pool, the pool's
      // initiator affirmatively endorsed it too -- see the principle above.
      const winningPoolInitiator = winningPoolInitiatorByBaseProposalId.get(proposalId);
      creditLeg(direction.rising, direction.falling, [proposal.proposerId, accepterId, winningPoolInitiator]);
      claimSwap(proposal.entityA, proposal.entityB);
    } else if (event.type === "POOL_RESOLVED" && event.payload.reason === "executed") {
      const pool = poolsById.get(event.payload.pool_id as string);
      const accepterId = event.actor_game_player_id;
      if (!pool || !accepterId) continue;
      const direction = directionOf(pool.entityC, pool.entityD);
      if (!direction) continue;
      creditLeg(direction.rising, direction.falling, [pool.initiatorId, accepterId]);
      claimSwap(pool.entityC, pool.entityD);
    } else if (event.type === "MARKET_CORRECTION_RESOLVED" && event.payload.reason === "triggered") {
      // Claims both swaps so they never fall into the "unclaimed =
      // unilateral" bucket below -- deliberately no bump()/clear() calls
      // at all. See the design note above.
      const moves = (event.payload.moves as { entity_a: string; entity_b: string }[] | undefined) ?? [];
      for (const move of moves) {
        claimSwap(move.entity_a, move.entity_b);
      }
    }
  }

  // Whatever's left unclaimed in any pair's queue is, by construction, a
  // unilateral burn -- see the design note above.
  const unilateralSwaps: UnilateralSwapEvent[] = [];
  for (const queue of pendingSwapsByPair.values()) {
    for (const swap of queue) {
      if (!swap.actorId) continue;
      const rising = swap.positionA < swap.positionB ? swap.entityA : swap.entityB;
      const falling = rising === swap.entityA ? swap.entityB : swap.entityA;
      bump(rising, swap.actorId, "unilateral");
      clear(falling, swap.actorId);
      unilateralSwaps.push({
        entityA: swap.entityA,
        entityB: swap.entityB,
        positionA: swap.positionA,
        positionB: swap.positionB,
        actorId: swap.actorId,
        seqNo: swap.seqNo,
      });
    }
  }

  const markers: SupportMarkers = new Map();
  for (const [entityId, perPlayer] of support) {
    const entries = [...perPlayer.entries()]
      .map(([playerId, { count, unilateralCount }]) => ({ playerId, count, unilateralCount }))
      .sort((x, y) => y.count + y.unilateralCount - (x.count + x.unilateralCount) || x.playerId.localeCompare(y.playerId));
    if (entries.length > 0) markers.set(entityId, entries);
  }
  return { markers, unilateralSwaps };
}

export function computeSupportMarkers(events: EventView[], pools: PoolView[]): SupportMarkers {
  return _walkEvents(events, pools).markers;
}

/** Every unclaimed SWAP_EXECUTED across the full event log, in event order
 * -- the raw data behind computeSupportMarkers' own unilateralCount bump,
 * exposed separately for consumers that need the swap occurrences
 * themselves (e.g. a "Hanky just swapped X for Y" lingering overlay),
 * not just the aggregated per-entity marker counts. Shares _walkEvents'
 * single claim-tracking pass rather than re-deriving it -- same
 * unilateral-vs-claimed distinction, same Market Correction exclusion. */
export function findUnilateralSwaps(events: EventView[], pools: PoolView[]): UnilateralSwapEvent[] {
  return _walkEvents(events, pools).unilateralSwaps;
}

/** Real count stays uncapped internally (computeSupportMarkers never
 * throws information away) -- only this compact tile-sized label caps
 * at 3+, per an explicit "don't lose information just because the
 * display is small" instruction. */
export function formatSupportCount(count: number): string {
  return count >= 3 ? "3+" : String(count);
}
