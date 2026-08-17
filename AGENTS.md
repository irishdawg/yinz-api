# Gotiate — Agent Onboarding

Monorepo for Gotiate ("Everything is Negotiable"), a real-time negotiation
card game. This file is a concise map, not a spec — it tells you where to
look, not what the rules are.

## Authoritative sources, in order

1. **Code + tests** (`apps/api/src/gotiate/`, `apps/api/tests/`) — the
   actual behavior. If any doc disagrees with the code/tests, the code and
   tests win. Always.
2. **`GAMEPLAY.md`** (repo root) — current gameplay rules and invariants,
   kept in sync with the code. Read this before touching domain logic.
3. **`DATABASE.md`** (repo root) — current DB conventions, migration rules,
   and schema state.
4. **`CURRENT_WORK.md`** (repo root) — unresolved decisions, deferred
   features, known gaps, and one flagged inconsistency awaiting a decision.
   Check this before assuming something unusual you find is a bug versus
   already-known.
5. **`HISTORY.md`** (repo root) — chronological build narrative.
   **Non-authoritative** — historical color only, not kept in sync, never
   overrides 1–4. `git log` is the actually-authoritative history if you
   need it.

Comments throughout the domain code reference an external "domain model
doc" by section number (`§01`–`§11`) and various feature "design
writeups." **None of these exist in this repository.** Treat them as
historical color pointing at something outside git, never as a resolvable
pointer — `GAMEPLAY.md` plus the code/tests are what this repo actually
has.

## Architecture

```
yinz-api/
├── apps/
│   ├── api/   — FastAPI backend (Python 3.12, uv). Sole authoritative
│   │            decision-maker for all game state. Deployed on Render.
│   └── web/   — Next.js (App Router, TypeScript, Tailwind, pnpm).
│                Deployed on Vercel. Never calls Render directly.
├── supabase/  — Postgres migrations, RLS policies, theme content seed
│                data for the one shared Supabase project. See DATABASE.md.
├── render.yaml
├── GAMEPLAY.md, DATABASE.md, CURRENT_WORK.md, HISTORY.md
└── README.md
```

**One repo because most feature work touches both an endpoint and the UI
that calls it.** Render/Vercel each deploy only their own subtree (see
root `README.md` for the exact mechanism). `supabase/` isn't deployed by
either platform — `supabase db push` is a deliberate, separate, manual
step (see `DATABASE.md`).

**Request path**: browser → Next.js Route Handlers (`apps/web/src/app/api/`)
→ FastAPI (`apps/api`) → Postgres (via `gotiate_backend`, a direct
Postgres role, not the Supabase Data API). The browser never calls Render
directly — every FastAPI route except `/health` requires a shared-secret
gateway header only Next.js's server-side code holds (see "Security
boundaries" below).

**Backend layout** (`apps/api/src/gotiate/`):
- `domain/` — the rules engine. Pure Python, zero I/O, fully unit-testable.
  `engine.py` (command handlers + all mutation), `entities.py` (data
  shapes + `GameConfig`), `events.py` (the event vocabulary), `projections.py`
  (`project()`/`project_events()` — the *one* place visibility is decided),
  `setup.py` (starting-state generation), `themes.py` (content, not rules).
- `api/` — thin FastAPI layer: `routes.py`, `auth.py` (Supabase JWT
  verification), `deps.py`, `rate_limit.py`, `schemas.py`. Decides nothing;
  calls the engine, persists the result.
- `persistence/` — `repository.py` (the `GameRepository` Protocol +
  in-memory implementation, used by every test), `postgres_repository.py`
  (the real implementation main.py actually serves), `player_names.py`.

**Frontend layout** (`apps/web/src/`):
- `app/page.tsx` (home/create/join), `app/game/[id]/page.tsx` (the main
  gameplay screen — large, single-file by convention), `app/join/[code]/page.tsx`.
- `app/api/**/route.ts` — thin gateway wrappers around `lib/gotiate-api.ts`'s
  `callGotiateApi()`, the single chokepoint that attaches the Supabase
  session token, the gateway secret, and a request id.
- `lib/` — `submitCommand.ts`, `useGameView.ts`/`useGameEvents.ts` (polling
  hooks), `supportMarkers.ts` (pure, non-authoritative UI derivation — see
  `GAMEPLAY.md` §13), `haircutRisk.ts`, `auth.ts`.

## Build / test commands

```bash
# Backend
cd apps/api
uv sync
uv run uvicorn gotiate.main:app --reload    # local dev — ALWAYS with --reload, see gotchas below
uv run pytest                                # offline suite, in-memory repository only

# Frontend
cd apps/web
pnpm install
pnpm dev
npx tsc --noEmit
npx eslint <changed files>
npx next build
```

There is no committed integration-test suite against real Postgres —
verification against the live database has been manual/scripted per
feature (see `CURRENT_WORK.md`).

## Security & privacy boundaries

- **Gateway secret**: every FastAPI route except `/health` (and, outside
  production, `/docs`/`/redoc`/`/openapi.json`) requires header
  `x-gotiate-gateway-key` matching `GOTIATE_GATEWAY_SECRET`
  (`GatewaySecretMiddleware`, fail-closed, `main.py`). A missing/wrong
  secret gets a plain 404, not 401 — indistinguishable from a route that
  doesn't exist. Only `apps/web`'s server-side Route Handlers hold this
  secret; the browser never does.
- **Auth**: real Supabase session JWTs, ES256 verified against the
  project's JWKS endpoint (`api/auth.py`) — not a stub, not HS256 (that
  path exists but is unused). Every failure collapses to a boring 401.
- **Visibility is decided in exactly one place**: `projections.project()`
  / `project_events()`, driven by an `Audience` (`PlayerAudience`,
  `PublicAudience`, `ReplayAudience`) the server derives from the verified
  JWT — never client-supplied. `EVENT_VISIBILITY` is an explicit,
  exhaustive, default-deny map (`test_event_visibility_registry_is_exhaustive`
  enforces this at the test level — a new `EventType` with no visibility
  decision fails the suite). See `GAMEPLAY.md` §11 for the full model.
  **Do not add a second place that decides what a player can see** — not
  in a route, not in the frontend, not in RLS beyond what `DATABASE.md`'s
  direct-read split already covers.
  - Reads split deliberately: data intrinsically public to a game's own
    seated players (market order, phase, public proposal/pool fields,
    reserve counts, clock state) *may* be read directly from Supabase
    under `is_game_member()`-scoped RLS. Everything else — holdings,
    reserve identities, private-pool contents, Influence, the pending
    -pickup frozen view, the Haircut profile pre-reveal — stays
    exclusively behind FastAPI's `project()`. Never re-express that logic
    as RLS; that's exactly the "same rule in two languages that can
    drift apart" trap this schema deliberately avoids.
- **Rate limits**: `POST /games` (create) 5/hour, `POST /games/join`
  10/5min, everything else 200/min default (`api/rate_limit.py`), keyed
  on the real client IP forwarded by the Next.js gateway, not per-session.
- **No free-text anywhere.** Every `display_name` is validated against
  the curated `player_name_seeds` catalog before the domain engine ever
  sees it — this is the entire content-moderation story, deliberately.

## Migration conventions (see `DATABASE.md` for the full current state)

- All schema/RLS/grants changes are ordered, timestamped `.sql` files
  under `supabase/migrations/`. No manual Supabase Dashboard changes,
  ever, except emergency read-only inspection.
- **Never edit an already-applied migration.** Fix mistakes with a new
  forward migration — this schema has already needed one grants-only
  follow-up migration for exactly this reason (see `HISTORY.md`).
- A new FastAPI-only table needs its own grants (RLS enabled, zero
  policies, `gotiate_backend` granted, `authenticated`/`anon` denied) —
  `create table` alone is not enough; this has been the source of a real,
  live-caught bug before.
- Push with `npx supabase db push` (already linked to the live project;
  re-link with `npx supabase link --project-ref <ref>` if
  `supabase/.temp/` is ever wiped). This is a real, deliberate action
  against shared infrastructure — confirm before pushing if there's any
  ambiguity about what's in the migration.
- Every migration must let a fresh empty Supabase project build from
  zero — no undocumented manual setup steps.

## Operational gotchas

- **Windows local dev: always run `uvicorn` with `--reload`.** Without it,
  uvicorn hardcodes a `ProactorEventLoop` on Windows that psycopg's async
  mode can't use — `--reload`'s subprocess path gets a working selector
  loop for free. Render/Linux is unaffected either way. See `main.py`'s
  own comment on this.
- **Stale/duplicate local `uvicorn --reload` workers are a recurring
  trap.** A worker left running from an earlier session can silently
  keep answering requests on the same port alongside a freshly started
  one, serving old code with no visible error. Before trusting any local
  verification: `Get-CimInstance Win32_Process | Where-Object Name -match
  'python'` (PowerShell), kill every result, confirm zero remain, start
  exactly one fresh instance, confirm its PID before proceeding.
- **Tests never touch real Postgres.** `create_app()` requires an
  explicit `repository` argument specifically so the test suite can never
  fall through to a real connection just because `.env` has
  `GOTIATE_BACKEND_DATABASE_URL` set — every test passes
  `InMemoryGameRepository()` explicitly.
- `apps/web/AGENTS.md` and `apps/web/CLAUDE.md` are **not** hand-maintained
  — `next dev` regenerates a Next.js-version-warning block into them.
  Don't be alarmed by their contents; they're unrelated to this file.
