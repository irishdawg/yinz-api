# Gotiate — Development History (non-authoritative)

> **This file is historical narrative, not documentation of current
> behavior.** It's a chronological account of decisions, fixes, and
> reversals made while building the database/persistence layer, preserved
> because the reasoning is genuinely useful context — but it was written
> at various points in the past and has **not** been kept in sync with
> every schema/behavior change since.
>
> **It must never override `GAMEPLAY.md`, `DATABASE.md`, `CURRENT_WORK.md`,
> or the actual code/tests.** If anything here conflicts with those, this
> file is wrong and stale — trust the current docs and the code instead.
> A concrete example already present in this file: it describes "14
> tables" and "six migrations" at a point in the project's history: there
> are 23 migrations and (still, coincidentally) 14 tables as of this
> writing — check `supabase/migrations/` and `DATABASE.md`'s own current
> table list, don't count from this file.
>
> For the complete, always-accurate history, `git log` is authoritative —
> this file is a hand-written narrative overlay on top of it, useful for
> the *why*, not the *what's true now*.

---

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
a cancelled game has no realized Haircut depth, no winner, nothing meaningful to
replay, and reusing `SCORED` would mean either running the real scoring
algorithm against a possibly-still-in-lobby game or special-casing
`project()`'s scored branch to detect "fake" scores. A new enum value
was cheaper and more honest than either. **(Note: `CANCEL_GAME`'s legal
phase range was later narrowed to `LOBBY`-only — see `GAMEPLAY.md`, not
described in this paragraph since this paragraph predates that change.)**

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
tests) still green. **(Test count is long stale — see the real current
count via `uv run pytest`.)**

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

**"Next up" list as it stood at that point in history (largely
resolved since — do not treat as current status):**
- A `reserve_count_remaining`-vs-`holdings` invariant test, and more
  generally a small opt-in integration test suite against real Postgres
  (marked/skipped by default so the offline suite stays untouched) —
  deliberately deferred past this pass. **Still true as of this
  writing** — see `CURRENT_WORK.md`.
- `get()`/`get_by_join_code()` do 9 sequential per-table SELECTs rather
  than one aggregate query. **Still true as of this writing** — see
  `CURRENT_WORK.md`.
- Add `SUPABASE_SERVICE_ROLE_KEY` and the matching Vercel env vars —
  **done** since this was written (present in `apps/api/.env`/Render's
  dashboard).
- Staging/production split on both Render and Vercel — deliberately
  deferred. **Still true as of this writing** — see `CURRENT_WORK.md`.

---

## Later chapters, not originally written up here

Everything from Stage 3 (bare proposals) onward — Pools, Reserves,
the private Influence economy, Pass, the Haircut-risk scoring model,
market-direction locking, unilateral support markers, and Market
Correction — was built after this narrative was last extended, and was
never folded back into it. Their design reasoning lives in this
project's own conversation/planning history outside this repository, not
here; their *current, authoritative* behavior lives in `GAMEPLAY.md`
(cross-checked directly against the code as of the date this handoff
documentation was written). Don't assume this file's silence on any of
those features means they don't exist or aren't finished — check
`GAMEPLAY.md` and `git log` instead.
