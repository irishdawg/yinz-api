-- Follow-up fix: 20260814162000_proposal_pass.sql created proposal_passes
-- but never granted gotiate_backend access to it, nor enabled RLS -- a
-- real gap caught live (permission denied for table proposal_passes on
-- every GET /games/{id}). FastAPI-only table, same treatment as
-- pool_contents/event_ledger: RLS enabled with zero policies (deny-all
-- for RLS-subject roles, not even an owner-read shortcut), no grants at
-- all to authenticated/anon, full access to gotiate_backend (which
-- bypasses RLS via BYPASSRLS on the role -- grants are the second,
-- independent layer, see DATABASE.md).

alter table proposal_passes enable row level security;

revoke all on proposal_passes from authenticated, anon;
grant all on proposal_passes to gotiate_backend;
