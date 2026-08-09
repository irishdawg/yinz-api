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

Not yet created. Region is chosen once, at project creation, and is
expensive to change afterward (a new project + data migration, not a config
edit) — so this happens **before** any migration work, not after.

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

```bash
npx supabase link --project-ref <GOTIATE_PROJECT_REF>
npx supabase db push
```

`db push` applies whatever local migrations haven't been applied to the
linked remote yet. No local Docker stack required just to push — that's
only needed for things like `db dump` or running the full stack locally.

Flow while the schema's still actively changing:

```
write migration → review the SQL → git commit → npx supabase db push → verify against the hosted project
```

Once the schema stabilizes, this can move to Supabase's GitHub integration
(deploy-on-merge-to-main) — not worth building that automation while the
schema is still moving weekly.

## What's not decided yet

- **Region.** Render defaults to Oregon (US West) unless `render.yaml` sets
  `region:` explicitly — ours doesn't yet. Supabase-to-Render proximity is
  the most latency-critical relationship in the stack (every command's
  transaction round-trips Postgres, likely more than once, inside the row
  lock), more so than the Vercel↔Render hop, and it's the one that's
  hardest to fix after the fact. This needs an answer before the project
  gets created, not after.
- **Table shape for the RLS split.** The domain model already establishes
  that private fields (portfolio holdings, `ready_to_close`, the waterline
  entity, reserve identities) can't share a table with public fields
  (market position, Influence counters) or Postgres Changes broadcasts
  them to everyone regardless of RLS. Translating `Game`/`GamePlayer`/
  `Holding`/`Proposal`/`Pool`/`PendingPickup` into actual tables along that
  boundary is real design work, not a mechanical dump of the Pydantic
  models — worth its own reviewed pass before the first migration gets
  written, same as the domain model doc got reviewed before code did.
