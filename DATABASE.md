# Gotiate Database

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
│   │   └── src/gotiate/domain/theme_data/   # theme content — JSON stays
│   │                                         # here, seeded into Postgres,
│   │                                         # not hand-entered in the
│   │                                         # Supabase dashboard
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
- Theme content (`fictional_companies_v1`, `dragons_v1`, `cats_v1`, ...)
  lives as the JSON files already in
  `apps/api/src/gotiate/domain/theme_data/` and gets seeded from there —
  not typed into the Supabase UI, and not hand-duplicated into a second
  SQL source that can drift from the JSON.
- **FastAPI remains the only authoritative gameplay writer, full stop.**
  Every table's grants revoke `INSERT`/`UPDATE`/`DELETE` from the
  `authenticated` and `anon` roles explicitly — not relying on RLS alone to
  block writes it would already block, matching Supabase's own
  grants-plus-RLS guidance (two independent layers, not one).
- **Reads split in two, deliberately, not duplicated.** Data that's
  intrinsically public to every player *seated in that specific game* —
  market order, phase, public proposal/pool fields, existence/status of
  private pools (not their contents), public Influence balances, reserve
  counts, clock/cutoff state — may be read and subscribed to (Postgres
  Changes, V1) directly from Supabase. RLS on those tables is
  `using (private.is_game_member(game_id))` — one audited `SECURITY
  DEFINER` helper, not a repeated inline subquery. The obvious inline
  version is fine on other tables but a real recursion risk when applied to
  `game_players`' own policy (evaluating it re-triggers the same RLS-gated
  query); the helper breaks that cycle by design. Never a bare `true` —
  "public" means public to that game's players, not to every authenticated
  Gotiate account.
  Everything else — holdings, reserve identities, private-pool contents,
  `ready_to_close`, the frozen pending-pickup view, portfolio values, the
  Waterline pre-close, postgame replay — stays exclusively behind
  FastAPI's `project()`. That logic (§03/§06 of the domain model) is
  already built and tested; re-expressing it as RLS policies would mean
  maintaining the same visibility rules in two languages that can drift
  apart. Never duplicate it.
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

## What's done, what's next

Schema is designed, reviewed, written, and **live on the Gotiate project**
— 13 tables (5 direct-read, 6 FastAPI-only, 2 global content), the
`is_game_member()` helper, RLS, grants, and the `supabase_realtime`
publication, as six migrations under `supabase/migrations/`
(`create_backend_role`, `theme_content_schema`, `game_schema`,
`rls_policies`, `grants_and_realtime`, `seed_theme_content`). See §11 of
the domain model doc for the design rationale. Theme content is seeded
via `apps/api/scripts/generate_theme_seed.py`, which reads
`apps/api/src/gotiate/domain/theme_data/*.json` — re-run it and paste the
output into a fresh migration whenever theme content changes; never
hand-edit an applied migration.

`gotiate_backend`'s password is set and `GOTIATE_BACKEND_DATABASE_URL` is
in `apps/api/.env`, verified against the live project.

`engine.new_id()` now returns `str(uuid.uuid4())` (canonical dashed form)
for clean Postgres `uuid` column compatibility. Full test suite (77
tests) still green.

Real Supabase JWT verification landed in `api/deps.py`/`api/auth.py`
(ES256/JWKS, confirmed live against the project), plus a shared-secret
gateway header (`GOTIATE_GATEWAY_SECRET`) so Render's public URL is
useless to anyone who isn't the Next.js gateway. Anonymous sign-in →
create game → join-code + QR → join is live end to end (`apps/web`),
verified against the live Supabase project in a real browser, not just
unit-tested. That was the last blocker before any public deployment.

Next up:
- A `reserve_count_remaining`-vs-`holdings` invariant test — deferred
  until the Postgres-backed `GameRepository` exists, since the in-memory
  repository has no second source of truth to drift against yet.
- Build the actual Supabase-backed `GameRepository` (satisfies the
  existing `GameRepository` Protocol in `persistence/repository.py`),
  swapping it in for `InMemoryGameRepository` — currently every created
  game only survives for the life of one running `uvicorn` process.
- Add `GOTIATE_BACKEND_DATABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and
  `GOTIATE_GATEWAY_SECRET` to Render's dashboard env vars, and the
  matching Vercel env vars (`NEXT_PUBLIC_SUPABASE_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_KEY`, `GOTIATE_API_URL`,
  `GOTIATE_GATEWAY_SECRET`), once the Render/Vercel projects exist.
- Create the actual Render service and Vercel project — neither exists
  yet. Render: point at this repo, `render.yaml` already scopes it to
  `apps/api`. Vercel: set Root Directory to `apps/web` (see
  `apps/web/README.md`).
