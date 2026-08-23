# Gotiate Database

Current, authoritative DB conventions and schema state. For game rules see
`GAMEPLAY.md`; for unresolved/deferred items see `CURRENT_WORK.md`; for the
chronological build narrative (non-authoritative) see `HISTORY.md`.

## Source of truth

`supabase/migrations/` in **this repo** — a monorepo, the same one Render
deploys `apps/api` from and Vercel will deploy `apps/web` from (see root
`README.md`). There is no separate database repo. Schema changes and the
domain code that depends on them land in the same commit; that's the whole
point of keeping them together.

```
yinz-api/
├── apps/
│   ├── api/
│   │   └── src/gotiate/domain/theme_data/   # theme content fixture --
│   │                                         # Postgres is the real app's
│   │                                         # source of truth now, this
│   │                                         # JSON is the offline test
│   │                                         # suite's copy, hand-synced
│   └── web/
├── supabase/
│   ├── migrations/                  # ordered, timestamped .sql files
│   └── config.toml                  # safe to commit — no secrets in it
├── DATABASE.md                      # this file
└── render.yaml                      # scoped to apps/api (rootDir + buildFilter)
```

`supabase/` sits at the repo root, outside both `apps/`, because it isn't
deployed by either platform — migrations are pushed manually (below), not
triggered by Render or Vercel's build steps.

## Remote project

Created and linked.

- Name: `Gotiate`
- Ref: `vomnvmnsdhvwtqkjtoal`
- Region: `us-east-1` (East US, North Virginia) — confirmed at creation
- Postgres: 17 (engine release `17.6.1.155`, GA channel)
- Status: `ACTIVE_HEALTHY`

Region was chosen over Oregon because it lands exactly on Render's
`virginia` region (set explicitly in `render.yaml`) *and* Vercel's default
function region (`iad1`, Washington D.C.) — the only pairing where all
three services share a physical region with zero extra Vercel-side
configuration. Also closer to the larger US population center. This was a
real decision, not a placeholder — Supabase's region is effectively
permanent once chosen, so don't re-litigate it without a concrete reason.

The project ref is not sensitive (it's the subdomain in the project's
public URL) and is safe to have in this file. The database password and
service-role key are not — those stay in `apps/api/.env` (gitignored) and
Render's dashboard env vars, never here.

**Two distinct trusted credentials, not one.** `gotiate_backend` is a
direct PostgreSQL role FastAPI holds a connection pool against — it runs
`SELECT ... FOR UPDATE` and multi-statement transactions around a game's
row lock, which the Supabase Data API (PostgREST) can't express. Supabase's
`service_role` API key is separate: only relevant if FastAPI ever calls
Supabase's HTTP APIs directly (Auth admin, Storage) instead of talking to
Postgres. Both are in `.env` now: `SUPABASE_SERVICE_ROLE_KEY` and
`GOTIATE_BACKEND_DATABASE_URL`. Still needs adding to Render's dashboard
env vars before deploy.

**Connection path: Supavisor session pooler, not the direct connection.**
Render has no IPv6 outbound, and Supabase's direct Postgres connection is
IPv6-only by default. Two ways around that: the $4/month IPv4 add-on, or
Supabase's Supavisor pooler, which is IPv4-compatible and free. Went with
Supavisor in **session mode** (port 5432, not the 6543 transaction-mode
port) — session mode holds one dedicated connection per client for the
connection's lifetime, same behavior as a direct connection, which is what
`SELECT ... FOR UPDATE` across a multi-statement transaction needs.
Transaction-mode pooling can't be relied on for that. Username format is
`<role>.<project-ref>` (Supavisor's tenant-routing convention, not a
Postgres username), host is `aws-0-<region>.pooler.supabase.com`. Verified
live: connects as `gotiate_backend`, reads all 120 seeded theme entities.

## Rules

- All schema/RLS/Realtime changes are ordered SQL migration files under
  `supabase/migrations/`. No manual changes in the Supabase Dashboard except
  emergency read-only inspection — if a dashboard change is ever made
  live, it gets reproduced as a migration before anything else happens.
- Migrations cover tables, indexes, constraints, enums/types,
  triggers/functions, RLS enablement + policies, and Realtime publication
  config — the whole schema surface, not just tables.
- The migration history must build a fresh empty Supabase project from
  zero. No undocumented manual setup steps.
- Never edit a migration once it's been applied to a shared project. Fix
  mistakes with a new forward migration.
- Theme content (`fictional_companies_v1`, `dragons_v1`, `cats_v1`, ...):
  `theme_sets`/`theme_entities` in Supabase are the real deployed app's
  source of truth (`themes.PostgresThemeRepository`, loaded once into an
  in-memory cache at FastAPI startup — see `CURRENT_WORK.md` for why it
  can't be a live per-call query). `apps/api/src/gotiate/domain/theme_data/*.json`
  still exists as the offline test suite's fixture (tests must never touch
  real Postgres) and the real app's fallback with no database configured —
  kept in sync with Postgres **by hand**, no tooling enforces it. Edit
  content via a forward migration (see
  `20260817220000_theme_entity_content_update.sql` for the pattern) or the
  dashboard-plus-a-reproducing-migration convention above, then mirror the
  same change into the JSON in the same commit.
- **FastAPI remains the only authoritative gameplay writer, full stop.**
  Every table's grants revoke `INSERT`/`UPDATE`/`DELETE` from the
  `authenticated` and `anon` roles explicitly — not relying on RLS alone to
  block writes it would already block, matching Supabase's own
  grants-plus-RLS guidance (two independent layers, not one).
- **Reads split in two, deliberately, not duplicated.** Data that's
  intrinsically public to every player *seated in that specific game* —
  market order, phase, public proposal/pool fields, existence/status of
  private pools (not their contents), each player's own Moves/Boosts
  counts, the single active-negotiation pointer — may be read and
  subscribed to (Postgres Changes, V1) directly from Supabase. RLS on
  those tables is
  `using (private.is_game_member(game_id))` — one audited `SECURITY
  DEFINER` helper, not a repeated inline subquery. The obvious inline
  version is fine on other tables but a real recursion risk when applied to
  `game_players`' own policy (evaluating it re-triggers the same RLS-gated
  query); the helper breaks that cycle by design. Never a bare `true` —
  "public" means public to that game's players, not to every authenticated
  Gotiate account.
  Everything else — holdings, private-pool contents, `ready_to_close`,
  secret Arbitration jury votes, the frozen pending-Draw/Refresh view,
  portfolio values, the Haircut profile pre-reveal, postgame replay —
  stays exclusively behind FastAPI's `project()`. That logic (§03/§06 of
  the domain model) is already built and tested; re-expressing it as RLS
  policies would mean maintaining the same visibility rules in two
  languages that can drift apart. Never duplicate it.
  Non-seated spectator access (`PublicAudience`, already supported by
  `GET /games/{id}`) isn't covered by the direct-read fast path at all —
  it falls back to the FastAPI path, which already handles it.
- **Realtime consumption stays encapsulated on the frontend.** Postgres
  Changes has a real scaling ceiling (Supabase's own guidance: usable to
  roughly low thousands of concurrent subscribers on a table, likely
  counted across *all* games sharing that table, not per game) — fine for
  V1 at 2-6 players/game, not a permanent guarantee once enough concurrent
  games are running. Broadcast is Supabase's recommended path past that
  point. The frontend should never scatter direct `postgres_changes`
  subscriptions through game UI code — wrap it once, so switching to
  Broadcast later is a swap behind that wrapper, not a rewrite.
- **Migrations never run automatically from Render's startup command.**
  Database deployment is a deliberate, separate step from the API process
  booting. A container that boots uvicorn and then casually runs pending
  migrations is a container that can end up serving traffic against a
  half-migrated schema, or hang wondering what decade it's in if the
  migration fails mid-flight.
- Git holds schema, not data: migrations, RLS policies, functions,
  `config.toml`, seed definitions. Never a production dump, never real
  game/user data, never secrets.

## Deploying migrations (current stage — manual, deliberate)

Already linked (`supabase/.temp/`, gitignored, holds the local link state —
re-run `link` if that directory ever gets wiped):

```bash
npx supabase link --project-ref vomnvmnsdhvwtqkjtoal
npx supabase db push
```

`db push` applies whatever local migrations haven't been applied to the
linked remote yet. No local Docker stack required just to push — that's
only needed for things like `db dump` or running the full stack locally
(Docker Desktop's absence only breaks the migration-catalog cache, not the
push itself — safe to ignore that warning).

If `db push` (or the CLI generally) fails with `LegacyDbConfigIpv6Error`,
re-run `supabase link --project-ref vomnvmnsdhvwtqkjtoal` — the CLI's own
push path needs the IPv4-compatible link too, not just FastAPI's runtime
connection.

The role's password is never in a migration file (roles are global to the
Postgres instance, not scoped to a migration's transaction) — set/rotate it
out-of-band with:

```bash
npx supabase db query --linked "ALTER ROLE gotiate_backend WITH PASSWORD '<new-password>';"
```

`--linked` runs the query via the Management API against the linked
project, no direct Postgres connection (or its own IPv6 problem) needed
just to set a password.

Flow while the schema's still actively changing:

```
write migration → review the SQL → git commit → npx supabase db push → verify against the hosted project
```

Once the schema stabilizes, this can move to Supabase's GitHub integration
(deploy-on-merge-to-main) — not worth building that automation while the
schema is still moving weekly.

## Current schema state

15 tables live on the Gotiate project as of this writing (verified
directly against `information_schema.tables`, not inferred from migration
file names — some migrations alter existing tables rather than create new
ones): `command_receipts`, `event_ledger`, `game_player_private`,
`game_players`, `games`, `holdings`, `market_entities`, `player_name_seeds`,
`pool_contents`, `pools`, `proposal_arbitration`, `proposal_passes`,
`proposals`, `theme_entities`, `theme_sets`. (`waterline` was created with
the original schema and properly `drop table`'d in
`20260813010000_haircut_risk_economy.sql` when the Haircut-risk model
replaced it — gone, not orphaned, confirmed live.) `proposal_arbitration`
is the cadence/economy redesign's own FastAPI-only addition (checkpoint
3) — holds a negotiation's pending Arbitration state, including secret
jury votes, deliberately its own table rather than a jsonb column on the
direct-read `proposals` table (see its own migration's comment). 33
migrations under `supabase/migrations/` as of this writing — run `ls
supabase/migrations/` for the authoritative current list, don't count
from prose in this file or in `HISTORY.md`.

**A real bug class to watch for**: `pools.visibility` was live-broken for
a while — `MAKE_POOL_PUBLIC` flipped it correctly in the domain object and
appended the event, but the `pools` row's own `ON CONFLICT (id) DO UPDATE
SET` clause in `postgres_repository.py` never listed `visibility`, so the
write silently kept its insert-time value forever (every reload read the
stale value straight back off the row — nothing about it looked wrong at
the domain-test level, since the offline suite never touches Postgres).
Fixed by adding the missing column to the `SET` list; a full audit of
every other upsert in the file (`proposals`, `game_players`,
`game_player_private`, `pool_contents`, `holdings`, `market_entities`)
at the same time found no other instance. **Whenever a new mutable
column is added to an existing table's upsert** (most recently
`game_player_private.pending_boost_draw`, added correctly with this
lesson already in mind — see checkpoint 4), it must be added to that
table's `ON CONFLICT ... DO UPDATE SET` list too, or it silently never
persists past the first insert — confirmed correct in every offline
test (which never touches Postgres) while being live-broken the whole
time. Double-check the `SET` list explicitly whenever a migration adds
a column the domain layer ever mutates after creation, not just on the
migration itself.

Direct-read (public, RLS via `is_game_member()`), FastAPI-only (RLS
enabled, zero policies, grants only to `gotiate_backend`), and global
-content (public catalog data, no membership question) categorization
follows the "Rules" section above — check a table's own migration for
which bucket it's in rather than trusting a stale count anywhere.

**A second lesson from a real deploy attempt, this time about dropping
columns/CHECK constraints, not adding them**: the cadence/economy
redesign's own checkpoint 6 cleanup migration
(`20260823140000_cadence_economy_cleanup.sql`) originally tried to
*tighten* several CHECK constraints (`games.close_reason`,
`proposals.resolution_reason`, `pools.resolution_reason`,
`holdings.zone`) down to only the values the current domain model can
still produce. The push failed: real pre-redesign SCORED games already
on this project used the old values (`TIME_EXPIRED`, a base proposal's
`withdrawn_by_initiator`, `base_proposal_withdrawn`, the old reserve
zones) — a narrowed CHECK rejected its own historical rows. Fixed by
*widening* instead: every dropped column is genuinely gone (nothing
reads or writes them), but every CHECK constraint keeps its old values
legal alongside the new ones, since no code path can produce the old
values again but real historical rows still carry them. **Dropping a
column that's truly unused is safe; narrowing a CHECK/enum against a
live table is not, unless you've confirmed no existing row uses the
value being removed** — `select distinct <column> from <table>` first.

`apps/api/scripts/generate_theme_seed.py` reads
`apps/api/src/gotiate/domain/theme_data/*.json` and generates INSERT
statements — useful for seeding a *brand-new* theme set (or a theme set's
first version) into Postgres for the first time. Editing *existing*
content goes the other direction now (Postgres is the source of truth —
see the "Theme content" bullet above and `CURRENT_WORK.md`): write a
migration, then hand-mirror the same change into the JSON. Never hand-edit
an applied migration either way.

For the chronological story of how this schema got here — including two
real bugs caught and fixed live, and one reversal — see `HISTORY.md`
(explicitly non-authoritative; if it disagrees with this section, this
section wins).
