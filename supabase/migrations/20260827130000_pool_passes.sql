-- Pool Pass is add-only membership, independently scoped to each Pool.
-- Identities are projected publicly by FastAPI, but this normalized table
-- remains FastAPI-only so clients do not acquire a second read path for
-- negotiation visibility rules.

create table pool_passes (
  pool_id uuid not null references pools (id) on delete cascade,
  game_id uuid not null references games (id) on delete cascade,
  game_player_id uuid not null references game_players (id),
  primary key (pool_id, game_player_id)
);

alter table pool_passes enable row level security;
revoke all on pool_passes from authenticated, anon;
grant all on pool_passes to gotiate_backend;

alter table pools drop constraint pools_resolution_reason_check;
alter table pools add constraint pools_resolution_reason_check
  check (resolution_reason in (
    'executed', 'withdrawn_by_initiator', 'invalidated_by_initiator_action',
    'declined_by_target', 'expired_all_passed', 'preempted_by_other_action',
    'market_closed', 'voided_market_swung', 'base_proposal_voided',
    'base_proposal_withdrawn' -- historical only; superseded by base_proposal_voided
  ));
