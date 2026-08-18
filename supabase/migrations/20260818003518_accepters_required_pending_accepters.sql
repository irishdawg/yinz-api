-- Multi-accept threshold at 5-6 players (GameConfig.accepters_required):
-- a bare proposal/public pool now needs more than one distinct accepter
-- before it executes, so an in-progress accept that hasn't yet reached
-- threshold has to survive a reload same as everything else on
-- Proposal/Pool -- see engine._handle_accept_proposal/_handle_accept_pool
-- and Proposal.pending_accepters/Pool.pending_accepters.
--
-- {player_id: locked_liability} -- fully public (accepting is already
-- public everywhere else), so no RLS/grants change needed: both tables'
-- existing is_game_member()-scoped row policy already covers this column.

alter table proposals add column pending_accepters jsonb not null default '{}'::jsonb;
alter table pools add column pending_accepters jsonb not null default '{}'::jsonb;
