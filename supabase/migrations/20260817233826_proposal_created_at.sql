-- Accept-lock grace period: ACCEPT_PROPOSAL (and ACCEPT_POOL on a public
-- pool, gated to the base proposal's own lock) is blocked for
-- accept_lock_seconds after PROPOSAL_CREATED -- see engine.py's
-- _require_accept_unlocked. project() only ever sees the current Game
-- snapshot, never the event ledger, so this needs its own column rather
-- than being derived from event_ledger at read time.
--
-- Backfilled to now() for any already-open proposal at migration time --
-- harmless: at worst it re-enters a few-second accept lock right after
-- deploy, same as a proposal created moments earlier would.

alter table proposals add column created_at timestamptz not null default now();
