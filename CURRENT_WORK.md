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

## Two pre-existing tests are intermittently flaky in a full suite run

Not caused by any specific feature — found while adding the real-names
tests below simply made the suite slightly larger/slower. Both are 100%
reliable run individually or in their own file, but each failed once in
5 full-suite runs (never both in the same run, never a consistent
repro): `tests/api/test_lobby_flow.py::test_extend_lobby_timer_pushes_the_deadline_out`
and `tests/domain/test_market_correction.py::test_discard_holding_that_changes_source_ownership_resolves_invalidated`.
Comparing against the baseline before this session's additions (5/5
clean full-suite runs) suggests real wall-clock timing and/or shared
unseeded `random` module state leaking across tests, not a logic bug —
worth a closer look (seed/inject time and rng explicitly in both) before
the suite gets meaningfully larger, but out of scope to chase down here.
If you see either fail, rerun before treating it as a regression.

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
- **No integration test suite against real Postgres.** The 238-test
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

## This session's own unfinished follow-ups (Market Correction feature)

- **Stagnation-point construction-success rate is not yet instrumented.**
  The Market Correction feature's construction-failure rate was measured
  extensively at `START_GAME` time (led to widening the 2-player market
  from 9 to 11 slots — see `GAMEPLAY.md` §9 and `git log` around
  `20260815000000_market_correction.sql`), but the rate that actually
  matters — construction success right when the 90-second stagnation
  threshold fires, in a genuinely stagnant late-game state — has not been
  separately measured. This was explicitly called out as a follow-up, not
  something the `START_GAME`-time numbers substitute for.
- **No live browser verification of the Market Correction banner/ticker
  UI.** The backend + Postgres persistence path was live-verified end to
  end (a real save/reload round-trip against the live Supabase project,
  plus `TRIGGER_MARKET_CORRECTION` executed against a game reloaded fresh
  from the database) — see the commit message on
  `464b104`/`git log --grep "Market Correction"`. The frontend banner
  (`MarketCorrectionBanner` in `apps/web/src/app/game/[id]/page.tsx`),
  its countdown, and the new ticker copy (`describeEvent`'s
  `MARKET_CORRECTION_*` branches) have only been verified via `tsc`/
  `eslint`/`next build` passing cleanly plus a standalone script against
  the real `computeSupportMarkers` — not an actual browser/Playwright
  pass.

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
