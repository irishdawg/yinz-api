-- Cadence/economy redesign (prototype branch), checkpoint 3: Arbitration.
--
-- proposal_arbitration holds the currently-pending Arbitration state for a
-- negotiation, including secret jury votes -- it is deliberately its OWN
-- FastAPI-only table, not a jsonb column bolted onto `proposals`.
-- `proposals` is a direct-read table (is_game_member() RLS, see
-- 20260809232706_rls_policies.sql): every field it has ever carried was
-- already intrinsically public (entities, status, who proposed it,
-- passed_player_ids). Jury votes are exactly the category DATABASE.md
-- warns to keep out of that bucket -- "everything else ... stays
-- exclusively behind FastAPI's project()" -- so this follows the same
-- pattern as pool_contents/holdings/game_player_private: RLS enabled,
-- zero policies, gotiate_backend only. At most one row exists at a time
-- (deleted the instant Arbitration resolves, any reason); the permanent
-- record for Replay lives in event_ledger's own ARBITRATION_RESOLVED
-- payload, not here.

create table proposal_arbitration (
  proposal_id uuid primary key references proposals(id) on delete cascade,
  game_id uuid not null references games(id) on delete cascade,
  state jsonb not null
);

alter table proposal_arbitration enable row level security;
revoke all on proposal_arbitration from authenticated, anon;
grant all on proposal_arbitration to gotiate_backend;

-- Not added to supabase_realtime -- FastAPI-only tables never are, see
-- 20260809232708_grants_and_realtime.sql's own precedent.

alter table proposals drop constraint proposals_resolution_reason_check;
alter table proposals add constraint proposals_resolution_reason_check
  check (resolution_reason in (
    'executed', 'withdrawn_by_initiator', 'market_closed', 'expired_all_passed',
    'voided_market_swung', 'arbitration_neither'
  ));
