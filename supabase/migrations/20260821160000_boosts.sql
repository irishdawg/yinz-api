-- Cadence/economy redesign (prototype branch), checkpoint 4: Boosts.
--
-- Concentrate and Force Swap resolve synchronously -- nothing new to
-- persist beyond the holdings/market_entities rows save() already
-- upserts. Draw/Refresh's own pending decision, though, is exactly the
-- same class of "single-player-private, must never be direct-readable"
-- state the old (now-unused) pending_pickup column on game_player_private
-- held before checkpoint 1 -- so it gets a fresh column on that same
-- already-FastAPI-only table (RLS enabled, zero policies -- see
-- 20260809232706_rls_policies.sql) rather than reusing pending_pickup
-- itself, per "never edit an applied migration." pending_pickup is left
-- in place, unused, for a later cleanup checkpoint's forward migration to
-- drop alongside it.
--
-- Unlike proposal_arbitration (checkpoint 3), this does NOT need its own
-- dedicated table: game_player_private already carries zero grants for
-- authenticated/anon, so a new column here inherits that same posture for
-- free, no new revoke/grant statements required.

alter table game_player_private add column pending_boost_draw jsonb;
