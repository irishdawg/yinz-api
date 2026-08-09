# Gotiate Database

## Source of truth

`supabase/migrations/` in **this repo** — the same one Render deploys the
FastAPI backend from. There is no separate database repo. Schema changes and
the domain code that depends on them land in the same commit; that's the
whole point of keeping them together.

```
yinz-api/
├── src/gotiate/domain/theme_data/   # theme content — JSON stays here,
│                                     # seeded into Postgres, not hand-entered
│                                     # in the Supabase dashboard
├── supabase/
│   ├── migrations/                  # ordered, timestamped .sql files
│   ├── config.toml                  # safe to commit — no secrets in it
│   └── seed.sql                     # once it exists — generated from
│                                     # theme_data/*.json, not hand-maintained
├── DATABASE.md                      # this file
└── render.yaml
```

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
service-role key are not — those stay in `.env` (gitignored) and Render's
dashboard env vars, never here.

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
  lives as the JSON files already in `src/gotiate/domain/theme_data/` and
  gets seeded from there — not typed into the Supabase UI, and not
  hand-duplicated into a second SQL source that can drift from the JSON.
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
  Changes, V1) directly from Supabase. RLS on those tables is scoped to
  game membership, e.g. `exists (select 1 from game_players gp where
  gp.game_id = t.game_id and gp.auth_user_id = auth.uid())` — never a bare
  `true`, since "public" means public to that game's players, not to every
  authenticated Gotiate account. Index the membership lookup; Supabase
  explicitly recommends indexing columns used in RLS checks.
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
only needed for things like `db dump` or running the full stack locally.
No migrations exist yet, so there's nothing to push until the schema design
pass below is done.

Flow while the schema's still actively changing:

```
write migration → review the SQL → git commit → npx supabase db push → verify against the hosted project
```

Once the schema stabilizes, this can move to Supabase's GitHub integration
(deploy-on-merge-to-main) — not worth building that automation while the
schema is still moving weekly.

## What's not decided yet

- **The literal table list.** The read/write split above is settled;
  translating `Game`/`GamePlayer`/`Holding`/`Proposal`/`Pool`/
  `PendingPickup`/`CommandReceipt`/`EventLedgerEntry` into actual tables
  along that boundary is the next reviewed pass — same treatment as the
  domain model doc got before code did. Not written yet.
