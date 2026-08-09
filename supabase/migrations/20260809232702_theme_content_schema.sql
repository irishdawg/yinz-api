-- Theme content -- domain model §02/§11 (ThemeSet/ThemeEntityDefinition).
-- Not per-game state: the same catalog rows apply to every game, every
-- account, forever. Version is part of the identity, not metadata --
-- fictional_companies_v1 renaming an entity in a later version must never
-- retroactively change what an already-played game's replay shows.

create table theme_sets (
  theme_set_id text not null,
  version int not null,
  name text not null,
  created_at timestamptz not null default now(),
  primary key (theme_set_id, version)
);

create table theme_entities (
  theme_set_id text not null,
  theme_set_version int not null,
  theme_key text not null,
  display_name text not null,
  ticker_symbol text not null,
  is_locked boolean not null default false,
  logo_url text,
  primary key (theme_set_id, theme_set_version, theme_key),
  foreign key (theme_set_id, theme_set_version)
    references theme_sets (theme_set_id, version)
    on delete cascade,
  check (char_length(ticker_symbol) between 1 and 10)
);

comment on table theme_sets is
  'Versioned content catalog. A game freezes theme_set_id + version at deal time; content is never mutated in place once a game has referenced it.';
comment on table theme_entities is
  'Individual entities within a theme set version. Seeded from src/gotiate/domain/theme_data/*.json -- never hand-edited through the API or dashboard.';

-- Global, genuinely public content -- the one deliberate exception to
-- "never a bare true" RLS policy. There is no membership question to
-- scope against: the same rows apply regardless of which game, or
-- whether the caller is even authenticated yet.
alter table theme_sets enable row level security;
alter table theme_entities enable row level security;

create policy "theme_sets are publicly readable"
  on theme_sets for select
  to authenticated, anon
  using (true);

create policy "theme_entities are publicly readable"
  on theme_entities for select
  to authenticated, anon
  using (true);

-- Writes are seed/migration-only, regardless of RLS -- grants and RLS are
-- independent layers (DATABASE.md), so this is enforced even if a policy
-- were ever misconfigured.
revoke all on theme_sets from authenticated, anon;
revoke all on theme_entities from authenticated, anon;
grant select on theme_sets to authenticated, anon;
grant select on theme_entities to authenticated, anon;

grant all on theme_sets to gotiate_backend;
grant all on theme_entities to gotiate_backend;
