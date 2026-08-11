-- Player display-name seeds -- domain model: no free-text display names,
-- ever. A player creating/joining a game gets served a random placeholder
-- from this pool and can re-roll (unlimited, until the game starts) but
-- never type their own -- the whole point is never needing a content-
-- moderation filter on a display name. FastAPI validates every
-- create_game/join_game display_name against this table before accepting
-- it (see apps/api/src/gotiate/persistence/player_names.py) and increments
-- usage_count on success; within a single game, a name already taken by a
-- seated player is also rejected (checked in-process against that game's
-- players, not here -- this table only tracks the global pool).
--
-- Direct-read table, same "genuinely public, no membership question to
-- scope against" reasoning as theme_sets/theme_entities -- not gameplay
-- state, the same rows apply regardless of game or account.

create table player_name_seeds (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  usage_count int not null default 0,
  created_at timestamptz not null default now()
);

comment on table player_name_seeds is
  'Curated placeholder display names. No open text entry anywhere in the product -- see DATABASE.md.';

alter table player_name_seeds enable row level security;

create policy "player_name_seeds are publicly readable"
  on player_name_seeds for select
  to authenticated, anon
  using (true);

revoke all on player_name_seeds from authenticated, anon;
grant select on player_name_seeds to authenticated, anon;
grant all on player_name_seeds to gotiate_backend;
