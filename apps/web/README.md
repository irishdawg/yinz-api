# Gotiate Web

Frontend, deployed on Vercel. Talks to `apps/api` (FastAPI, on Render) through
this app's own routes — the browser never calls Render directly; see the
root README and the domain model doc for why (SENT→CONFIRMED UI state
machine over the request round-trip).

Not yet scaffolded — framework choice is still open. When it is:

1. Scaffold the app directly in this directory.
2. In Vercel's project settings, set **Root Directory** to `apps/web`.
3. Add an Ignored Build Step so a commit that only touches `apps/api` or
   `supabase/` doesn't trigger a Vercel rebuild — e.g. (for git-based
   frameworks) `git diff --quiet HEAD^ HEAD -- apps/web` as the check
   command, or `npx turbo-ignore` if this becomes a Turborepo.
