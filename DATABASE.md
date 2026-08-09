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
- **FastAPI remains the only authoritative gameplay writer.** RLS exists to
  stop accidental/unauthorized direct browser access to Postgres — it is
  not a substitute for domain validation, and nothing about having RLS
  changes the "Supabase stores and distributes state, FastAPI decides
  state" principle from the domain model.
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

- **Table shape for the RLS split.** The domain model already establishes
  that private fields (portfolio holdings, `ready_to_close`, the waterline
  entity, reserve identities) can't share a table with public fields
  (market position, Influence counters) or Postgres Changes broadcasts
  them to everyone regardless of RLS. Translating `Game`/`GamePlayer`/
  `Holding`/`Proposal`/`Pool`/`PendingPickup` into actual tables along that
  boundary is real design work, not a mechanical dump of the Pydantic
  models — worth its own reviewed pass before the first migration gets
  written, same as the domain model doc got reviewed before code did.
