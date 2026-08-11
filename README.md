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
Vercel via its project's Root Directory setting (`apps/web`) plus the
`ignoreCommand` in `apps/web/vercel.json`.
Supabase migrations aren't deployed by either platform — `supabase db
push` is still a deliberate, manual step (see `DATABASE.md`).

**Live infra:**
- Vercel: project `gotiate` under the Atlas Rising team (`atlasrising`;
  they house multiple projects there, hence the specific name rather than
  the `web` default), connected to this repo (auto-deploy on push to
  `main`, confirmed live). Deployment Protection (Vercel Pro's default SSO
  wall on every `*.vercel.app` URL without a custom domain) is off, so
  https://gotiate-atlasrising.vercel.app is publicly reachable.
- Render: service `gotiate-api-staging` (Virginia, free plan), created via
  Render's API and connected to this repo (`rootDir: apps/api`,
  auto-deploy on push to `main`). Live at
  https://gotiate-api-staging.onrender.com — `/health` is the one route
  exempt from the gateway-secret check; everything else 404s without it.
- Both currently deploy from `main` — no separate staging branch yet.
  Deliberately deferred (see git history) until there's an actual need to
  keep a live game running while staging changes. There's a clear
  migration path when that's needed: a second Render service + a second
  Vercel environment, each pointed at a `staging` branch instead of
  `main` — nothing about the current setup blocks that later.
- Verified end to end against the real deployed stack (not just
  localhost): two separate browser sessions against the live Vercel URL,
  one creates a game and gets a scannable QR (encoding the real
  `https://gotiate-atlasrising.vercel.app/join/<code>` URL, not a bare
  path), the other joins via the code, both land in the same game.
  Real server-side processing time on Render for that test: 1.9ms
  (create), 2.9ms (join) — Vercel's `iad1` and Render's `virginia` are
  effectively network neighbors.

`FastAPI remains the sole authoritative source of truth.` Supabase stores
and distributes state; it never decides it. The frontend talks to
`apps/api` through Vercel, never straight to Render.
