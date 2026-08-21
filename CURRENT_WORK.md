# Current Work — unresolved decisions, deferred features, known gaps

This file exists so unresolved questions and known gaps don't get mixed
into `GAMEPLAY.md` (which describes only locked, current behavior) or
`DATABASE.md` (current DB rules/schema). Nothing here is a "TODO list" the
next session is obligated to pick up in order — it's an honest inventory
so a fresh agent doesn't have to rediscover any of it, or accidentally
treat a shaky corner as more solid than it is.

Each item cites exactly where to look. When you resolve one, delete it
from this file (or move it into `GAMEPLAY.md`/a commit, as appropriate) —
don't let this file accumulate stale "already done" entries.

---

## Theme content is now Postgres-sourced for the real app, JSON is a fixture kept in sync by hand

`theme_sets`/`theme_entities` in Supabase are the real deployed app's source
of truth as of this change — `themes.PostgresThemeRepository` loads them
into an in-memory cache once at FastAPI startup (`main.py`'s `lifespan`);
`themes.py`'s own module docstring explains why this can't be a live
per-call query (the domain layer — `engine.py`/`projections.py` — is
deliberately synchronous/zero-I/O, so `get()` can't await anything).
Consequence: **a content edit in Supabase takes effect on the next
deploy/restart, not live.** Not a bug — a deliberate simplicity trade-off
(no cache-invalidation machinery for a table that changes rarely), but
worth knowing if an edit doesn't seem to show up.

`theme_data/*.json` (`apps/api/src/gotiate/domain/theme_data/`) still
exists and is still what `JsonFileThemeRepository` reads — it's the
offline test suite's fixture (tests must never touch real Postgres, see
`AGENTS.md`) and the real app's fallback when `GOTIATE_BACKEND_DATABASE_URL`
isn't set. **There is no automated sync between the two anymore** — a
content edit made only in Supabase (or only in the JSON) will silently
drift from the other. Kept in sync by hand as of this change (see
`supabase/migrations/20260817220000_theme_entity_content_update.sql` and
the matching JSON edit in the same commit); no tooling enforces this going
forward.

No per-game version pinning: both repositories always resolve the *latest*
version for a `theme_set_id`. `GameConfig.theme_set_version` exists but
nothing reads it — a content rename made after a game started (or after
it's SCORED, for replay) can retroactively change what that game displays,
which is exactly what the original `theme_sets`/`theme_entities` schema
comment (`20260809232702_theme_content_schema.sql`) says must never happen.
This isn't a regression from the Postgres switch — the JSON-backed path
never implemented this either — but it's now more likely to actually bite,
since editing Supabase directly (rather than a code change + PR + deploy)
is a much lower-friction way to rename an entity mid-tournament. Fixing it
means threading `theme_set_version` through `engine._handle_start_game`
and `projections.py`'s own `themes.get_theme_set()` call, pinning it at
`START_GAME` time onto the persisted game — real scope, deliberately not
done as part of this change.

---

## Two pre-existing tests are intermittently flaky in a full suite run

Not caused by any specific feature. Both are 100% reliable run
individually or in their own file, but each has been observed to fail
roughly once per several full-suite runs (never both in the same run, no
consistent repro), across multiple separate sessions now — most recently
`test_discard_holding_that_changes_source_ownership_resolves_invalidated`,
confirmed flaky again this way (full-suite fail, isolated-run pass,
full-suite re-run clean) while adding unrelated features:
`tests/api/test_lobby_flow.py::test_extend_lobby_timer_pushes_the_deadline_out`
and `tests/domain/test_market_correction.py::test_discard_holding_that_changes_source_ownership_resolves_invalidated`.
Suggests real wall-clock timing and/or shared unseeded `random` module
state leaking across tests, not a logic bug — worth a closer look
(seed/inject time and rng explicitly in both) before the suite gets
meaningfully larger (currently 284 tests), but still out of scope to
chase down opportunistically. If you see either fail, rerun before
treating it as a regression.

---

## Typed display names have no content moderation (deliberately deferred)

`create_game`/`join_game` now accept a typed `display_name` (not just a
`player_name_seeds` catalog pick) for in-person play — see `AGENTS.md`,
`GAMEPLAY.md` §"Lobby", `api/routes.py`'s `_resolve_submitted_name`.
Validation on a typed name is purely structural (non-empty after
trimming, capped at 24 characters) — no profanity/abuse filter, by
explicit product decision at the time this shipped, since the initial
use case is players typing their own real names around a physical table.
Worth revisiting before any anonymous-link/public-facing use of typed
names becomes common — this is exactly the content-moderation surface
the curated-catalog approach was originally built to avoid entirely.

---

## Explicitly deferred / undecided in code comments (not this session's finding — pre-existing)

- **`CloseReason.OPTIONALITY_EXHAUSTED`** — a third close-reason value is
  named in a comment but was never added: `# No OPTIONALITY_EXHAUSTED for
  V1 — deliberately deferred, see decision log §10.` (`entities.py`,
  near `CloseReason`). Note: no "decision log" file exists anywhere in
  this repository — every `§NN` reference in the codebase (comments say
  "domain model §02/§03/...", "decision log §10") points at an external
  document this repo doesn't contain. Treat every such reference as
  historical color, not a resolvable pointer — see "External references
  that don't resolve" below.
- **`GameConfig.influence_revealed_in_replay`** (default `False`) —
  comment: "Undecided on purpose ... defaults to still-private in replay,
  trivially flippable later without a schema change." Same shape for
  `ready_to_close_revealed_in_replay`, though that one reads as more
  settled (defaults false, no "undecided" language attached).
- **`GameConfig.join_code_lifetime_minutes = 30`** — comment: "30 is a
  placeholder default, not a locked number — flagged for confirmation."
- **`GamePlayer.auth_user_id`** links straight to a Supabase auth identity;
  there's no persistent `PlayerAccount` concept spanning multiple games
  (stats, history, cross-game identity). `GamePlayer`'s own docstring says
  this explicitly: "not the same thing as an account ... PlayerAccount is
  deferred."
- **`valuation_policy_id`/`valuation_policy_version`** (`"linear_rank_v1"`)
  and **`final_scoring_policy_id`/`_version`** (`"haircut_risk_v1"`) exist
  on `GameConfig` and look like scaffolding for a pluggable policy
  registry, but there is no registry — `projections._portfolio_value`'s
  own comment says so directly: "Pluggable in name only for now; a real
  policy registry is future work." Exactly one scoring/valuation
  implementation exists today; the id/version fields are recorded but
  never dispatched on.
- **`game_player_private.auth_user_id` is nullable** (`ON DELETE SET
  NULL`) for an account-deletion path — no UI or command reaches that
  path today. `postgres_repository.py`'s `_to_game` comment notes this
  explicitly.
- **Full-table-scan lookups**: `find_active_game_hosted_by`/
  `find_active_game_seated_in` (both `InMemoryGameRepository` and,
  per the Postgres implementation's own equivalent queries,
  `PostgresGameRepository`) scan/filter without a dedicated index on the
  host/seat → auth_user_id path. Comment: "acceptable at V1 scale."
- **`get()`/`get_by_join_code()` do 9 sequential per-table SELECTs**
  rather than one aggregate query, inside a `REPEATABLE READ READ ONLY`
  snapshot transaction. Fine while most UI-reachable games are early-phase
  (several of the 9 tables are still empty), a real optimization target
  once negotiation-phase games with lots of proposals/pools/holdings are
  the common case.
- **`SetupQualityConfig`'s `topology_score` vs. `geometry_score` scale**
  — measured (not assumed) during the initial-distribution-quality pass:
  `topology_score` is nearly constant at low player counts while
  `geometry_score` has meaningfully larger variance, meaning
  `combined_score` may be geometry-dominated in practice. Flagged at the
  time as "worth knowing before playtesting... if the imbalance is real,
  flag it rather than silently reweighting" — **not yet rebalanced**, and
  not yet re-measured against the current (11-slot 2p) market size either.
- **n=2's "no isolation hard-reject" and n=3's "triangle-only topology
  convergence"** (`setup.py` / `SetupQualityConfig` defaults) were both
  explicitly flagged as "whether this holds up once played" — reasoned
  from first principles and one Monte Carlo pass, not yet validated
  against real multi-session play.

## Deferred infrastructure (repo-wide, not gameplay)

- **No staging/production split.** Both Render (`gotiate-api-staging`)
  and Vercel deploy from `main` only. Render's service is already grouped
  under a `Gotiate` Project with a `Staging` environment specifically so a
  `Production` environment can join later without restructuring, but that
  hasn't happened.
- **No integration test suite against real Postgres.** The 284-test
  offline suite (`uv run pytest`) exercises the domain engine directly and
  `InMemoryGameRepository` — real-Postgres behavior (constraints, RLS,
  concurrency) has been verified manually/live per feature, not via a
  committed, repeatable integration suite. A `-m integration`-style
  opt-in suite (skipped by default) was floated early on and never built.
- **Windows-local-dev-only gotcha, not a product bug**: running
  `uvicorn gotiate.main:app` **without** `--reload` on Windows gets a
  broken `ProactorEventLoop` that can't run psycopg in async mode
  (`main.py` has a long comment on this). Always run local dev with
  `--reload` per the documented command. A related recurring trap this
  session hit multiple times: an old `--reload` worker left running from
  a previous session can silently keep answering requests on the same
  port alongside a freshly started one, serving stale code. Before
  trusting any live/local verification, confirm there's exactly one
  `python.exe` process tied to the dev server (`Get-CimInstance
  Win32_Process | Where-Object Name -match 'python'` — kill all, start
  exactly one, confirm before proceeding).

## Market Correction — known gaps

Two real bugs have been found and fixed via live 2-player play since the
feature shipped (`git log --grep "Market Correction"` /
`tests/domain/test_market_correction.py`): the cooldown was pushing
forward unconditionally on *every* resolution reason, including
`EXPIRED`/`INVALIDATED` where nothing actually changed — this tacked
extra silence onto an already-stagnant market, so the real trigger
cadence drifted from "90s since the last deal" into something that felt
random to the player who reported it. Fixed: cooldown now only extends
on `TRIGGERED`/`MARKET_RESUMED`. Separately, a shared (mutually-owned)
holding could be the *other* player's move target, landing a second,
uninvited hit on top of your own already-independently-targeted move —
the mechanic is supposed to be exactly one downward move per player.
Fixed: each player's move source now excludes anything both players own.
Both fixes are covered by dedicated regression tests. What's still
genuinely unverified:

- **Stagnation-point construction-success rate is not yet instrumented.**
  The feature's construction-failure rate was measured extensively at
  `START_GAME` time (led to widening the 2-player market from 9 to 11
  slots — see `GAMEPLAY.md` §9 and `git log` around
  `20260815000000_market_correction.sql`), but the rate that actually
  matters — construction success right when the 90-second stagnation
  threshold fires, in a genuinely stagnant late-game state — has not been
  separately measured.
- **No automated browser verification of the Market Correction banner/
  ticker UI.** The backend + Postgres persistence path has been
  live-verified repeatedly (including catching both bugs above); the
  frontend banner (`MarketCorrectionBanner`), its countdown, and the
  ticker copy (`describeEvent`'s `MARKET_CORRECTION_*` branches) have
  only ever been verified via `tsc`/`eslint`/`next build` plus manual
  playtesting, never an automated Playwright-style pass.

---

## Playtest punch list — one item still outstanding

An extended live-playtesting cycle (2-6 player games, real feedback —
`git log` from `845bf1b` "Fix four playtest-found bugs" through the most
recent commits) worked through a 23-item punch list. Every item shipped
except:

- **"Faster pool alternative"** — a lighter-weight way to counter a
  proposal than the full Pool flow, floated as a possible pacing
  improvement. Never designed or implemented. If picked back up, start by
  asking what's actually slow about the current Pool flow in live play
  (composer steps? Influence-cost preview latency? something else)
  rather than assuming the fix.

Everything else from that list shipped: the accept-lock grace period
(now 7s, was 4s — real-play sync lag ate into the original window),
zero-Influence agency, the all-zero-Influence top-up, private per-card
notes (with per-player colors and a "Nobody" tag), the multi-accept
threshold at 5-6 players, the randomized Haircut generator, the flat
9-minute clock, and auto-withdraw on re-proposing.

Two of the more structurally novel additions have thorough test coverage
but haven't been exercised in an actual multi-player live game yet:

- **The accept threshold at 5-6 players** (`GameConfig.accepters_required`,
  `Proposal`/`Pool.pending_accepters`) — a bare proposal or public pool
  needs 2 distinct accepters before it executes, not just 1, at that
  headcount. Covered thoroughly by `tests/domain/test_accept_threshold.py`
  (pledge/refund/settlement paths), but never actually played out live at
  5 or 6 seats — watch for it feeling right pacing-wise once it does.
- **The randomized Haircut generator**'s two tunable constants —
  `_HAIRCUT_MIN_ADJACENT_GAP` (4 percentage points) and
  `_HAIRCUT_WITHIN_BAND_CEILING` (92%) — are first-cut numbers reasoned
  from a handful of real games, not a large sample. Both were added
  reactively to real problems found in play (adjacent positions reading
  as barely differentiated; the deepest in-band position always showing
  100% safe, defeating "the top K positions carry some risk"), so
  they're grounded, not arbitrary — but still worth revisiting if either
  starts to feel off with more play.

## External references that don't resolve — worth knowing, not fixing

Comments throughout `apps/api/src/gotiate/domain/*.py` cite an external
"Gotiate domain model doc" by section (`§01`–`§11`) and a "decision log
§10" — e.g. `entities.py`'s module docstring: "the conceptual shapes from
the Gotiate domain model doc, §02/§03." **No such document exists
anywhere in this repository.** It's presumably an external
design/planning document the project's author keeps outside of git. Don't
go looking for it in-repo, and don't treat a `§NN` reference as a
resolvable pointer — `GAMEPLAY.md`, the code, and the tests are what this
repo actually has, and are authoritative regardless of what an
out-of-repo document might additionally say. If a `§NN` comment and the
current code/tests ever seem to disagree, trust the code/tests.

Similarly, many comments across the domain layer reference a "design
writeup" for a specific feature (e.g. "see the Market Correction design
writeup," "see the Pass design writeup") — these are the same kind of
external reference, not files in this repo. `GAMEPLAY.md` is the
in-repo replacement for all of them; if a comment's claim and
`GAMEPLAY.md`/the code disagree, the code wins and `GAMEPLAY.md` should be
corrected.
