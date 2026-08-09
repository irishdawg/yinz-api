-- RLS -- domain model §11. The membership helper first, then every game
-- table gets RLS enabled; the 5 direct-read tables get a real policy,
-- the 6 FastAPI-only tables get zero policies (default-deny to every
-- role except gotiate_backend, which has BYPASSRLS).

create schema if not exists private;

-- The obvious inline policy -- exists(select 1 from game_players where
-- game_id = ... and auth_user_id = auth.uid()) -- is fine on other
-- tables, but applying it to game_players' own policy is a
-- self-referencing RLS query: evaluating it re-triggers the same
-- RLS-gated query, a real recursion risk Supabase documents, not a
-- hypothetical one. security definer + a locked search_path breaks the
-- cycle by running this check with the function owner's privileges,
-- bypassing RLS only for this one narrow internal lookup.
create or replace function private.is_game_member(p_game_id uuid)
returns boolean
language sql
security definer
set search_path = public
stable
as $$
  select exists (
    select 1 from game_player_private
    where game_player_private.game_id = p_game_id
      and game_player_private.auth_user_id = auth.uid()
  );
$$;

revoke all on function private.is_game_member(uuid) from public;
grant execute on function private.is_game_member(uuid) to authenticated;

-- ---------------------------------------------------------------------
-- Direct-read tables: real policy. "Public" means public to that game's
-- seated players, never a bare `true`.
-- ---------------------------------------------------------------------
alter table games enable row level security;
alter table game_players enable row level security;
alter table market_entities enable row level security;
alter table proposals enable row level security;
alter table pools enable row level security;

create policy "seated players can read their game"
  on games for select
  to authenticated
  using (private.is_game_member(id));

create policy "seated players can read game_players"
  on game_players for select
  to authenticated
  using (private.is_game_member(game_id));

create policy "seated players can read market_entities"
  on market_entities for select
  to authenticated
  using (private.is_game_member(game_id));

create policy "seated players can read proposals"
  on proposals for select
  to authenticated
  using (private.is_game_member(game_id));

create policy "seated players can read pools"
  on pools for select
  to authenticated
  using (private.is_game_member(game_id));

-- ---------------------------------------------------------------------
-- FastAPI-only tables: RLS enabled, zero policies. Not even an
-- "owner can read their own row" shortcut -- see domain model §11 for
-- why (the pending-pickup frozen view specifically requires reads to
-- go through project(), not a live table).
-- ---------------------------------------------------------------------
alter table game_player_private enable row level security;
alter table pool_contents enable row level security;
alter table holdings enable row level security;
alter table waterline enable row level security;
alter table event_ledger enable row level security;
alter table command_receipts enable row level security;
