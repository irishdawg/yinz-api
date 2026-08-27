-- Force Swap durability: the pair most recently Force-Swapped stays
-- locked against a direct (Move-only, negotiated) reverse via
-- PROPOSE_SWAP/CREATE_POOL, until either another Force Swap happens
-- (anywhere, any pair -- a Boost undoing a Boost is fair, same-cost play,
-- so it's never blocked) or either protected entity actually moves again
-- through an executed proposal/pool. See entities.ProtectedPair,
-- engine._is_protected_reversal.
--
-- A single global slot, not per-player -- plain jsonb, not a dedicated
-- table: unlike Arbitration's secret jury votes, this is public,
-- unconditional state (every seated player already sees the live market;
-- which pair is locked is exactly that same tier of public fact).

alter table games add column protected_pair jsonb;
