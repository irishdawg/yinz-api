# Gotiate Web

Frontend, deployed on Vercel. Talks to `apps/api` (FastAPI, on Render)
through this app's own routes — the browser never calls Render directly;
see the root README and the domain model doc for why (SENT→CONFIRMED UI
state machine over the request round-trip).

Next.js (App Router, TypeScript, Tailwind), package manager: pnpm.

## Local development

```bash
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000). Edit `src/app/page.tsx` — the page auto-updates.

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
