-- PASS_PROPOSAL -- a non-binding, permanent per-player exit from an open
-- proposal. See the Pass design writeup. Add-only membership (a player
-- who passes a proposal never un-passes it), so a plain composite-key
-- join table is enough -- no per-pass metadata is read by anything.

create table proposal_passes (
  proposal_id uuid not null references proposals (id) on delete cascade,
  game_id uuid not null references games (id) on delete cascade,
  game_player_id uuid not null references game_players (id),
  primary key (proposal_id, game_player_id)
);

-- EXPIRED_ALL_PASSED is the internal-only reason for a proposal that
-- auto-resolved once every other seated player passed it. It is never
-- shown live (projections.py masks it back to withdrawn_by_initiator for
-- every live audience) -- only replay ever sees the real value.
alter table proposals drop constraint proposals_resolution_reason_check;
alter table proposals add constraint proposals_resolution_reason_check
  check (resolution_reason in ('executed', 'withdrawn_by_initiator', 'market_closed', 'expired_all_passed'));
