-- Core game tables -- domain model §02/§11. Tables only here: RLS and
-- grants are separate migrations, deliberately, so each stays reviewable
-- on its own.
--
-- gen_random_uuid() is native to Postgres 13+ (this project runs 17) --
-- no pgcrypto extension needed.

-- ---------------------------------------------------------------------
-- games -- the aggregate root's public face. Direct-read table.
-- ---------------------------------------------------------------------
create table games (
  id uuid primary key default gen_random_uuid(),
  version int not null default 0,
  next_seq_no int not null default 1,
  phase text not null default 'LOBBY'
    check (phase in ('LOBBY', 'NEGOTIATION', 'CLOSING', 'SCORED')),
  created_at timestamptz not null default now(),
  join_code text not null unique,
  host_player_id uuid, -- fk added below, after game_players exists (circular reference)
  config jsonb not null,
  expected_player_count int check (expected_player_count between 2 and 6),
  started_at timestamptz,
  max_duration_s int,
  unilateral_cutoff_at timestamptz,
  unilateral_window_closed_at timestamptz,
  close_threshold int,
  closed_at timestamptz,
  close_reason text check (close_reason in ('TIME_EXPIRED', 'READY_THRESHOLD')),
  scored_at timestamptz,
  waterline_revealed boolean not null default false
);

-- ---------------------------------------------------------------------
-- game_players -- public half of GamePlayer. Direct-read table.
-- Genuinely public: auth_user_id lives on game_player_private instead.
-- ---------------------------------------------------------------------
create table game_players (
  id uuid primary key default gen_random_uuid(),
  game_id uuid not null references games (id) on delete cascade,
  seat int not null check (seat between 0 and 5),
  display_name text not null,
  influence_available int not null default 0,
  influence_committed int not null default 0,
  influence_spent int not null default 0,
  reserve_count_remaining int not null default 0, -- denormalized from holdings; FastAPI's obligation to keep in sync
  unique (game_id, seat)
);

alter table games
  add constraint games_host_player_id_fkey
  foreign key (host_player_id) references game_players (id);

-- ---------------------------------------------------------------------
-- game_player_private -- FastAPI-only. Never a direct read, not even
-- for the owner -- see domain model §11 for why (frozen pending-pickup
-- view depends on reads going through project(), not the live table).
-- ---------------------------------------------------------------------
create table game_player_private (
  game_player_id uuid primary key references game_players (id) on delete cascade,
  game_id uuid not null references games (id) on delete cascade, -- denormalized: is_game_member() reads this directly
  auth_user_id uuid references auth.users (id) on delete set null,
  ready_to_close boolean not null default false,
  pending_pickup jsonb,
  unique (game_id, auth_user_id) -- one seat per account per game; NULLs (deleted accounts) don't conflict with each other
);

-- ---------------------------------------------------------------------
-- market_entities -- the live market. Direct-read table.
-- Position is the only mutable field, ever -- enforced by FastAPI, not
-- the schema; nothing here stops a direct UPDATE, that's what grants do.
-- ---------------------------------------------------------------------
create table market_entities (
  id uuid primary key default gen_random_uuid(),
  game_id uuid not null references games (id) on delete cascade,
  entity_id text not null,
  theme_set_id text not null,
  theme_set_version int not null,
  position int not null check (position >= 1),
  unique (game_id, entity_id),
  unique (game_id, position),
  foreign key (theme_set_id, theme_set_version, entity_id)
    references theme_entities (theme_set_id, theme_set_version, theme_key)
);

-- ---------------------------------------------------------------------
-- proposals -- never private, contents always public. Direct-read table.
-- ---------------------------------------------------------------------
create table proposals (
  id uuid primary key default gen_random_uuid(),
  game_id uuid not null references games (id) on delete cascade,
  entity_a text not null,
  entity_b text not null,
  initiator_player_id uuid not null references game_players (id),
  status text not null default 'open' check (status in ('open', 'resolved')),
  resolved_at_seq_no int,
  resolved_by_player_id uuid references game_players (id),
  resolution_reason text
    check (resolution_reason in ('executed', 'withdrawn_by_initiator', 'market_closed')),
  check (entity_a <> entity_b),
  foreign key (game_id, entity_a) references market_entities (game_id, entity_id),
  foreign key (game_id, entity_b) references market_entities (game_id, entity_id)
);

-- ---------------------------------------------------------------------
-- pools -- metadata only: existence, initiator, visibility, status,
-- resolution. Direct-read table. Contents are a separate table, below.
-- ---------------------------------------------------------------------
create table pools (
  id uuid primary key default gen_random_uuid(),
  game_id uuid not null references games (id) on delete cascade,
  base_proposal_id uuid not null references proposals (id),
  initiator_player_id uuid not null references game_players (id),
  visibility text not null check (visibility in ('private', 'public')),
  status text not null default 'open' check (status in ('open', 'resolved')),
  resolved_at_seq_no int,
  resolved_by_player_id uuid references game_players (id),
  resolution_reason text check (resolution_reason in (
    'executed', 'withdrawn_by_initiator', 'invalidated_by_initiator_action',
    'declined_by_target', 'preempted_by_other_action', 'base_proposal_withdrawn',
    'market_closed'
  ))
);

-- ---------------------------------------------------------------------
-- pool_contents -- FastAPI-only, for every pool, public or private
-- alike. See domain model §11 for why public-pool contents aren't
-- split out to a faster direct-read path even though they safely could be.
-- ---------------------------------------------------------------------
create table pool_contents (
  pool_id uuid primary key references pools (id) on delete cascade,
  game_id uuid not null references games (id) on delete cascade, -- denormalized from pools, needed for the entity fk
  entity_c text not null,
  entity_d text not null,
  check (entity_c <> entity_d),
  foreign key (game_id, entity_c) references market_entities (game_id, entity_id),
  foreign key (game_id, entity_d) references market_entities (game_id, entity_id)
);

-- ---------------------------------------------------------------------
-- holdings -- every physical "card." FastAPI-only, always.
-- ---------------------------------------------------------------------
create table holdings (
  id uuid primary key default gen_random_uuid(),
  game_id uuid not null references games (id) on delete cascade,
  entity_id text not null,
  owner_player_id uuid references game_players (id),
  zone text not null check (zone in (
    'reserve_unrevealed', 'pickup_pending', 'portfolio', 'discarded',
    'pickup_surrendered', 'surrendered_unused', 'burned_unseen'
  )),
  revealed_to_owner boolean not null default false,
  revealed_at_seq_no int,
  foreign key (game_id, entity_id) references market_entities (game_id, entity_id)
);

-- ---------------------------------------------------------------------
-- waterline -- the one hidden reference. FastAPI-only, always.
-- ---------------------------------------------------------------------
create table waterline (
  game_id uuid primary key references games (id) on delete cascade,
  entity_id text not null,
  foreign key (game_id, entity_id) references market_entities (game_id, entity_id)
);

-- ---------------------------------------------------------------------
-- event_ledger -- append-only, unredacted, replay's only source of
-- truth. FastAPI-only, always. Composite PK is the event's actual
-- identity -- nothing in this model ever addresses an event globally.
-- ---------------------------------------------------------------------
create table event_ledger (
  game_id uuid not null references games (id) on delete cascade,
  seq_no int not null,
  type text not null,
  actor_game_player_id uuid references game_players (id),
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  primary key (game_id, seq_no)
);

-- ---------------------------------------------------------------------
-- command_receipts -- idempotency. FastAPI-only, always.
-- ---------------------------------------------------------------------
create table command_receipts (
  game_id uuid not null references games (id) on delete cascade,
  command_id text not null,
  actor_game_player_id uuid references game_players (id),
  command_type text not null,
  expected_game_version int,
  received_at timestamptz not null default now(),
  resulting_game_version int,
  status text not null check (status in ('applied', 'rejected_stale_version', 'rejected_illegal')),
  error_code text,
  error_message text,
  result_event_seq_start int,
  result_event_seq_end int,
  primary key (game_id, command_id)
);
