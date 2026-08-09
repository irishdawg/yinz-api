-- Gotiate's direct-Postgres backend role.
--
-- Distinct from Supabase's `service_role` API key (see DATABASE.md) --
-- this is a real Postgres login role FastAPI connects to directly for
-- authoritative transactions: SELECT ... FOR UPDATE row locks and
-- multi-statement transactions the Data API (PostgREST) can't express.
--
-- Deliberately created WITHOUT a password here. Passwords never belong in
-- a committed migration file, even a private one -- git history is
-- forever. The password is set out-of-band immediately after this
-- migration is applied, directly against the live project, and goes
-- straight into .env / Render's dashboard, never into a file that gets
-- committed.

create role gotiate_backend with
  login
  bypassrls
  noinherit
  connection limit 20;

comment on role gotiate_backend is
  'Gotiate FastAPI backend. Direct Postgres connection for authoritative game-state transactions. Password set out-of-band -- see DATABASE.md.';
