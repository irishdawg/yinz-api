-- Market Correction -- a 2-player-only, non-automatic anti-stagnation
-- mechanic. See the design writeup. Same treatment as haircut_profile:
-- stored directly on games as a JSONB blob plus a couple of timestamps,
-- no dedicated table, since a pending correction has no foreign-key
-- relationships of its own to enforce.

alter table games add column pending_market_correction jsonb;
alter table games add column last_negotiated_execution_at timestamptz;
alter table games add column market_correction_cooldown_until timestamptz;
