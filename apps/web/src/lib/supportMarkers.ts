import type { EventView } from "./useGameEvents";
import type { PoolView } from "./useGameView";

export interface SupportEntry {
  playerId: string;
  count: number;
}

export type SupportMarkers = Map<string, SupportEntry[]>;

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
 * A player who has any existing marker on the entity that *fell* has it
 * cleared entirely (not decremented) -- an "up" only goes away once that
 * same player helps the same entity back down. Withdrawn/expired/
 * declined/preempted resolutions never touch a marker. Movement
 * magnitude is irrelevant, and the count is never capped here -- only the
 * display layer caps at 3+. Direction is derived by comparing the two
 * entities' *final* positions against each other post-swap (lower
 * position = stronger rank = rose) -- no need for pre-swap positions.
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
 * `pools` (the live view.pools, not the event log) is the source of a
 * pool's own entities/initiator/base_proposal_id -- not the
 * PRIVATE_POOL_CREATED event's payload, which a non-insider's client may
 * have already cached in redacted form (no entity_c/entity_d) before the
 * pool executed and became publicly visible. view.pools is refetched
 * every poll, so it always reflects current visibility, regardless of
 * when this specific client first saw the creation event. */
export function computeSupportMarkers(events: EventView[], pools: PoolView[]): SupportMarkers {
  const support = new Map<string, Map<string, number>>();
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

  function bump(entityId: string, playerId: string) {
    const perEntity = support.get(entityId) ?? new Map<string, number>();
    perEntity.set(playerId, (perEntity.get(playerId) ?? 0) + 1);
    support.set(entityId, perEntity);
  }
  function clear(entityId: string, playerId: string) {
    support.get(entityId)?.delete(playerId);
  }
  function creditLeg(risingEntity: string, fallingEntity: string, participants: (string | null | undefined)[]) {
    const uniquePlayerIds = new Set(participants.filter((p): p is string => Boolean(p)));
    for (const playerId of uniquePlayerIds) {
      bump(risingEntity, playerId);
      clear(fallingEntity, playerId);
    }
  }
  function directionOf(entityA: string, entityB: string): { rising: string; falling: string } | null {
    const swap = lastSwapByPair.get(pairKey(entityA, entityB));
    if (!swap) return null;
    const rising = swap.positionA < swap.positionB ? swap.entityA : swap.entityB;
    return { rising, falling: rising === swap.entityA ? swap.entityB : swap.entityA };
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
      // Overwritten on every occurrence of this exact pair -- since events
      // are walked in seq order and a swap's own resolution always follows
      // it immediately (same command), the map always holds the swap that
      // belongs to whichever resolution reads it next.
      lastSwapByPair.set(pairKey(entityA, entityB), {
        entityA,
        entityB,
        positionA: event.payload.position_a as number,
        positionB: event.payload.position_b as number,
      });
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
    } else if (event.type === "POOL_RESOLVED" && event.payload.reason === "executed") {
      const pool = poolsById.get(event.payload.pool_id as string);
      const accepterId = event.actor_game_player_id;
      if (!pool || !accepterId) continue;
      const direction = directionOf(pool.entityC, pool.entityD);
      if (!direction) continue;
      creditLeg(direction.rising, direction.falling, [pool.initiatorId, accepterId]);
    }
  }

  const result: SupportMarkers = new Map();
  for (const [entityId, perPlayer] of support) {
    const entries = [...perPlayer.entries()]
      .map(([playerId, count]) => ({ playerId, count }))
      .sort((x, y) => y.count - x.count || x.playerId.localeCompare(y.playerId));
    if (entries.length > 0) result.set(entityId, entries);
  }
  return result;
}

/** Real count stays uncapped internally (computeSupportMarkers never
 * throws information away) -- only this compact tile-sized label caps
 * at 3+, per an explicit "don't lose information just because the
 * display is small" instruction. */
export function formatSupportCount(count: number): string {
  return count >= 3 ? "3+" : String(count);
}
