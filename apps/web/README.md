# Gotiate Web

Frontend, deployed on Vercel. Talks to `apps/api` (FastAPI, on Render)
through this app's own Route Handlers (`src/app/api/**/route.ts`) — the
browser never calls Render directly; see the root `README.md` and
`AGENTS.md` for why and for the full request path.

Next.js (App Router, TypeScript, Tailwind), package manager: pnpm.

## Layout

- `src/app/page.tsx`, `src/app/join/[code]/page.tsx`, `src/app/game/[id]/page.tsx`
  — the three real routes (home/create/join, join-by-code, the main
  gameplay screen).
- `src/app/api/**/route.ts` — thin server-side gateway wrappers around
  `src/lib/gotiate-api.ts`'s `callGotiateApi()`, the one place the Supabase
  session token, gateway secret, and request id get attached before
  calling FastAPI.
- `src/lib/` — polling hooks (`useGameView.ts`, `useGameEvents.ts`),
  command submission (`submitCommand.ts`), and pure UI-derivation logic
  (`supportMarkers.ts`, `haircutRisk.ts`) — see `../../GAMEPLAY.md` §13 for
  what these are and aren't allowed to decide.

## Local development

```bash
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000). Requires a
(gitignored) `.env.local` with `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_SUPABASE_ANON_KEY`, `GOTIATE_API_URL`, `GOTIATE_GATEWAY_SECRET`,
`NEXT_PUBLIC_SITE_URL` — see the root `AGENTS.md` for what each is for.
No example file is committed; ask for the real values or generate your own
against your own Supabase project. `apps/api` (with a real or in-memory
backing store) must be running and reachable at `GOTIATE_API_URL` for
anything beyond the static shell to work.

## Vercel setup (one-time, do this when the project is first imported)

1. Import this repo into a new Vercel project.
2. Project Settings → General → **Root Directory** → `apps/web`.
3. Project Settings → Git → **Ignored Build Step** → Custom Command:
   ```
   git diff --quiet HEAD^ HEAD -- .
   ```
   Also codified in `vercel.json` (`ignoreCommand`) so it's not a
   dashboard-only setting — this command runs with its working directory
   *at* Root Directory (`apps/web`), not the repo root, so `.` is correct
   here, not `apps/web`. It skips the build (exit 0) when nothing under
   `apps/web` changed; a commit that only touches `apps/api` or
   `supabase/` won't trigger a Vercel deploy.

No environment variables needed yet — this is a fresh scaffold with no
backend calls wired up.
