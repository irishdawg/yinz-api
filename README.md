# Gotiate

Monorepo for Gotiate ("Everything is Negotiable") — a real-time social
negotiation/bluffing card game.

```
yinz-api/
├── apps/
│   ├── api/    — FastAPI backend (Python 3.12), deployed on Render.
│   │            Sole source of truth for game rules and state. See
│   │            apps/api/README.md.
│   └── web/    — Next.js frontend, deployed on Vercel. See
│                apps/web/README.md.
├── supabase/   — Postgres migrations, RLS policies, and theme content
│                seed data for the shared Supabase project. See
│                DATABASE.md.
├── render.yaml — Render service definition, scoped to apps/api via
│                rootDir + buildFilter.
└── DATABASE.md — Database rules, deploy flow, and current schema status.
```

## Why one repo

`apps/api` and `apps/web` ship from the same commit history on purpose —
most feature work touches both a new endpoint and the UI that calls it, and
splitting that across two repos meant reviewing (and reasoning about) each
change in two places. Render and Vercel each deploy only their own
subtree: Render via `rootDir: apps/api` + a `buildFilter` in `render.yaml`;
Vercel via its project's Root Directory setting (`apps/web`) plus an
Ignored Build Step (see `apps/web/README.md` for the one-time Vercel
project setup — the project itself doesn't exist yet).
Supabase migrations aren't deployed by either platform — `supabase db
push` is still a deliberate, manual step (see `DATABASE.md`).

`FastAPI remains the sole authoritative source of truth.` Supabase stores
and distributes state; it never decides it. The frontend talks to
`apps/api` through Vercel, never straight to Render.
