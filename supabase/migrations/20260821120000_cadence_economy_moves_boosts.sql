-- Cadence/economy redesign (prototype branch), checkpoint 1: additive schema
-- for the Move/Boost economy and the single-active-negotiation constraint.
-- Influence, Market Correction, and gameplay-clock columns are left in
-- place, unused, until a later checkpoint's forward migration drops them --
-- never edit an applied migration, see DATABASE.md.

alter table game_players add column moves_remaining int not null default 5;
alter table game_players add column boosts_remaining int not null default 2;

-- Nullable, references proposals(id) -- at most one bare negotiation open
-- table-wide at any time. No index beyond the implicit FK one is needed at
-- this scale (see DATABASE.md's "acceptable at V1 scale" precedent for
-- find_active_game_hosted_by).
alter table games add column active_proposal_id uuid references proposals(id);
alter table games add column boosts_expired boolean not null default false;

-- Both game_players and games are already direct-read tables
-- (is_game_member() RLS policy, existing grants) -- a new column on an
-- existing direct-read table needs no new policy or grant, only a
-- dedicated table would.
