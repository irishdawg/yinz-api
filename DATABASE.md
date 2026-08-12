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
— 14 tables (5 direct-read, 6 FastAPI-only, 3 global content), the
`is_game_member()` helper, RLS, grants, and the `supabase_realtime`
publication, as six migrations under `supabase/migrations/`
(`create_backend_role`, `theme_content_schema`, `game_schema`,
`rls_policies`, `grants_and_realtime`, `seed_theme_content`). See §11 of
the domain model doc for the design rationale. Theme content is seeded
via `apps/api/scripts/generate_theme_seed.py`, which reads
`apps/api/src/gotiate/domain/theme_data/*.json` — re-run it and paste the
output into a fresh migration whenever theme content changes; never
hand-edit an applied migration.

`player_name_seeds` (2 more migrations: `player_name_seeds_schema`,
`seed_player_name_seeds`) is a third global-content, direct-read table
alongside `theme_sets`/`theme_entities` — same `using (true)` RLS
reasoning (genuinely public, no membership question to scope against).
100 curated "Adjective Creature" placeholder display names
(`Sly Fox`, `Clever Badger`, ...) — no open text entry anywhere in the
product. `create_game`/`join_game` validate every `display_name` against
this table before the domain engine ever sees it, and reject a name
already taken by another seated player in that specific game (checked
against the game's own loaded players, not this table). That's the
entire content-moderation story for display names: nothing here was ever
typed by a user, so there's nothing to filter.

Since then: `player_name_seeds` gained an `is_golden` boolean (rare,
1/500, drawn only on the very first name offered — never via reroll,
which structurally can't reach the golden pool at all) and an `is_active`
boolean (retire a name from future offers without deleting the row or
breaking historical references — checked only in the offer methods, not
in `is_valid_name()`, so a name offered while active but retired moments
before submission doesn't become rejectable). Two new endpoints
(`GET /player-names/initial`, `GET /player-names/reroll`) replace
free-text entirely in the UI; both take an optional `join_code` so a
game's existing roster is excluded from the offer too, not just
whatever's currently on screen — the actual fix for two players ending
up with the same name in one game. Offer selection narrows to the 10
least-used eligible names before picking randomly among just those, so
ordinary names stay roughly evenly exposed over time.

**One reversal along the way, worth recording as a real correction, not
just noise**: `game_players.display_name` briefly became `name_seed_id`
(a FK, resolved via join) for one commit, then got reverted back to text.
`player_name_seeds` is reference/catalog data — resolving a player's name
dynamically via join would mean renaming or retiring a seed name later
could retroactively change what an already-played game shows, the exact
replay-integrity problem `theme_entities` versioning already exists to
prevent elsewhere in this schema. `display_name` is assigned once, at
create_game/join_game time, and frozen from then on. The original
100-name placeholder list was purged and replaced with a real curated
53-name list — exactly one golden (`Dave`).

`gotiate_backend`'s password is set and `GOTIATE_BACKEND_DATABASE_URL` is
in `apps/api/.env`, verified against the live project.

**Golden-name odds moved off the free-preview endpoints and onto actual
seat creation.** `game_players` gained an `is_golden_name` boolean
(`20260811223018_game_players_is_golden_name.sql`). Previously the 1/500
golden coin-flip happened on `/player-names/initial` — a free, unbound
call anyone could hit repeatedly with a fresh anonymous JWT, no seat ever
consumed. Now `offer_name`/`reroll` never roll golden at all;
`create_game`/`join_game` each roll independently, exactly once, at the
moment a real seat is created — so getting more attempts means actually
filling seats (capped at 6/game) or creating more games
(`create_game`'s existing 5/hour-per-IP limit already bounds that). Also
dropped the old 5/hour limit on `/player-names/initial` specifically,
since there's nothing left on that path worth rate-limiting. Persisted
per-player rather than kept as a one-time event, so every seated player
can see who's golden live via the ordinary roster projection — the whole
point of it being rare is that it's visible to everyone, not a private
flag only the golden player's own client knows about.

**`games.phase` gained a `CANCELLED` terminal state**
(`20260811232821_games_phase_cancelled.sql`, extending the existing
`games_phase_check` constraint). `find_active_game_hosted_by`'s
one-active-game-per-host rule had no escape hatch short of playing a
game all the way to `SCORED` — a host who abandons a lobby or a
negotiation-in-progress was permanently blocked from creating another.
`CANCEL_GAME` (host-only, legal in any phase before `SCORED`) is the
fix. Deliberately a distinct phase from `SCORED`, not a reuse of it —
a cancelled game has no waterline, no winner, nothing meaningful to
replay, and reusing `SCORED` would mean either running the real scoring
algorithm against a possibly-still-in-lobby game or special-casing
`project()`'s scored branch to detect "fake" scores. A new enum value
was cheaper and more honest than either.

**`expected_player_count` removed; `lobby_reminder_deadline_at` and
`cancellation_reason` added**
(`20260812153122_lobby_reminder_and_cancellation_reason.sql`). Live
testing surfaced that `expected_player_count` was purely decorative — it
never capped joins (only the hard 6-seat limit did that) and never
triggered a start (only the host's Start button did). Dropped the column
entirely rather than leave it as dead weight; the lobby always allows up
to 6 now, no headcount question asked at all.

In its place: the host is still the *only* way `LOBBY` becomes
`NEGOTIATION` (no auto-start), but an abandoned lobby no longer sits open
forever either. `lobby_reminder_deadline_at` is set at `create_game` to
`created_at + lobby_reminder_seconds` (3 minutes); once passed, the host
sees a "start now or ask for more time" prompt (`EXTEND_LOBBY_TIMER`
pushes the deadline out by the same amount again, uncapped) while
everyone else sees a read-only version of the same prompt. If
`lobby_reminder_grace_seconds` (60s) passes with neither action, the game
auto-cancels — `cancellation_reason` (`HOST_INITIATED` or
`LOBBY_TIMEOUT`) distinguishes that from a deliberate host cancel so the
terminal screen can say something accurate. `CANCEL_GAME` itself is now
`LOBBY`-only too — once real gameplay is underway, the host loses the
unilateral power to end it for everyone else; an abandoned
`NEGOTIATION`-phase game just runs out its own negotiation clock instead.

One correctness gap this surfaced, not obvious until actually testing it
live: `apply_due_time_transitions` (the negotiation clock's own
auto-close, and now this) only ever ran from inside `handle_command` —
meaning a time-based transition would never actually fire unless
*something* submitted a command after the deadline passed. For an
abandoned lobby, by definition nobody is submitting anything anymore, so
the auto-cancel would never have fired in practice. Fixed by having
`GET /games/{id}` also apply due transitions
(`routes._sync_due_time_transitions`) — cheaply: a lock-free
`is_time_transition_due` check runs on every read, and only when
something's genuinely due does it acquire the write lock and persist for
real, so ordinary polling doesn't pay lock/write overhead on every tick.
This same fix means the negotiation clock's `TIME_EXPIRED` auto-close is
now also reachable from polling alone, not just from another command
happening to land after the deadline — a latent gap in already-shipped
behavior, caught as a side effect of building this.

**`market_entities`'s `unique (game_id, position)` constraint is now
`deferrable initially deferred`**
(`20260812194827_defer_market_entities_position_unique.sql`). A swap
exchanges two entities' positions atomically in the domain model
(`a.position, b.position = b.position, a.position`, no invalid
intermediate state, ever, in memory) — but `save()` persists that as two
separate `UPDATE`s against a plain unique constraint, and whichever
entity gets updated first transiently collides with the position the
other hasn't vacated yet. Never caught until Stage 3's `ACCEPT_PROPOSAL`
path first executed a real swap against live Postgres — every prior
verification pass exercised the domain engine or the in-memory
repository, neither of which has a uniqueness constraint to violate.
Deferring the check to transaction commit (`save()` already runs its
whole batch inside one transaction via `lock_for()`) fixes it generically
for any reordering, not just a hand-rolled two-entity special case.

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

`PostgresGameRepository` (`apps/api/src/gotiate/persistence/postgres_repository.py`)
is live, replacing `InMemoryGameRepository` in production — games created
through the UI now actually persist. Verified against the live project
both locally and on the real Render deployment: real rows confirmed via
direct SQL query, and a game created by one server process confirmed
readable, correctly, after that process was fully killed and a brand new
one queried it. `create_app()` now requires an explicit `repository`
argument (no default) specifically so the test suite can never fall
through to a real Postgres connection just because `.env` has
`GOTIATE_BACKEND_DATABASE_URL` loaded — every test passes
`InMemoryGameRepository()` explicitly; only the module-level `app` in
`main.py` (what `uvicorn` actually serves) builds the real one.

Two correctness issues specific to a real concurrent store (not
translation busywork) were caught and fixed as part of this, not left as
latent gaps — see the commit message on `PostgresGameRepository` for
detail: a stale-pre-lock-read bug in `join_game`/`submit_command` that
the in-memory implementation's shared-object-reference semantics had been
silently masking, and a torn-read risk on the unlocked `get()` path
(9 sequential SELECTs reconstructing one `Game` aggregate) fixed with a
`REPEATABLE READ READ ONLY` snapshot transaction.

Next up:
- A `reserve_count_remaining`-vs-`holdings` invariant test, and more
  generally a small opt-in integration test suite against real Postgres
  (marked/skipped by default so the 84-test offline suite stays
  untouched) — deliberately deferred past this pass; verification here
  was manual, matching how Vercel/Render themselves were stood up.
- `get()`/`get_by_join_code()` do 9 sequential per-table SELECTs rather
  than one aggregate query — fine while every UI-reachable game is
  LOBBY-phase (5 of those 9 return empty), real optimization work once
  gameplay commands (proposals/pools/holdings) are UI-reachable.
- Add `SUPABASE_SERVICE_ROLE_KEY` and the matching Vercel env vars
  (`GOTIATE_API_URL` now points at the real Render URL,
  `NEXT_PUBLIC_SITE_URL` at the real Vercel URL — both already set) to
  round out parity between the two platforms' dashboards.
- Staging/production split on both Render and Vercel — deliberately
  deferred; both currently deploy from `main` only. Render's service is
  already grouped under a **Gotiate** Render Project with a `Staging`
  environment specifically so a `Production` environment can join it
  later without restructuring.
