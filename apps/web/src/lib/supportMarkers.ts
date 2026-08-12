import type { EventView } from "./useGameEvents";

export interface SupportEntry {
  playerId: string;
  count: number;
}

export type SupportMarkers = Map<string, SupportEntry[]>;

function pairKey(a: string, b: string): string {
  return [a, b].sort().join("::");
}

/** Derives, from the public event log alone, which players have publicly
 * and successfully pushed each market entity up. When a bare proposal
 * executes, its proposer and accepter both get +1 on the entity that
 * rose; either of them also has any existing marker on the entity that
 * fell cleared entirely (not decremented) -- an "up" only goes away once
 * that same player helps the same entity back down. Withdrawn/expired
 * proposals never touch a marker. Movement magnitude is irrelevant, and
 * the count is never capped here -- only the display layer caps at 3+.
 * Direction is derived by comparing the two entities' *final* positions
 * against each other post-swap (lower position = stronger rank = rose) --
 * no need to know their positions before the swap. Deliberately not a
 * generic reducer: Pool- and unilateral-swap-triggered markers are
 * Stage 4+ scope, added as more event shapes into this same shape later. */
export function computeSupportMarkers(events: EventView[]): SupportMarkers {
  const support = new Map<string, Map<string, number>>();
  const proposalsById = new Map<string, { proposerId: string; entityA: string; entityB: string }>();
  const lastSwapByPair = new Map<string, { entityA: string; entityB: string; positionA: number; positionB: number }>();

  function bump(entityId: string, playerId: string) {
    const perEntity = support.get(entityId) ?? new Map<string, number>();
    perEntity.set(playerId, (perEntity.get(playerId) ?? 0) + 1);
    support.set(entityId, perEntity);
  }
  function clear(entityId: string, playerId: string) {
    support.get(entityId)?.delete(playerId);
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
      const proposal = proposalsById.get(event.payload.proposal_id as string);
      const accepterId = event.actor_game_player_id;
      if (!proposal || !accepterId) continue;
      const swap = lastSwapByPair.get(pairKey(proposal.entityA, proposal.entityB));
      if (!swap) continue;
      const risingEntity = swap.positionA < swap.positionB ? swap.entityA : swap.entityB;
      const fallingEntity = risingEntity === swap.entityA ? swap.entityB : swap.entityA;
      for (const playerId of [proposal.proposerId, accepterId]) {
        bump(risingEntity, playerId);
        clear(fallingEntity, playerId);
      }
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
