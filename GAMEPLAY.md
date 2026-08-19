# Gotiate — Gameplay Specification (current behavior)

**This document describes the game as it behaves today, as implemented in
`apps/api/src/gotiate/domain/` and verified by `apps/api/tests/`.** It is
authoritative for *current* rules and invariants. It intentionally contains
no history, no rationale for why a rule changed over time, and no
description of retired mechanics (there is no "Waterline" scoring model in
this codebase anymore; if you find a reference to it anywhere, that
reference is stale).

**If this document and the code/tests ever disagree, the code and tests
win.** Domain logic lives in `apps/api/src/gotiate/domain/engine.py`
(command handlers), `entities.py` (data shapes + config), `events.py` (the
event vocabulary), `projections.py` (visibility — what each audience is
allowed to see), and `setup.py` (starting-state generation). FastAPI is the
**sole authoritative decision-maker** for all game state; Supabase/Postgres
stores and distributes it but never adjudicates anything, and the frontend
never computes gameplay-legal outcomes itself (see "Frontend-derived UI
logic" at the end of this document for the one deliberate exception:
purely-decorative UI derived from already-public data).

For unresolved decisions, deferred features, and known gaps, see
`CURRENT_WORK.md` — this file describes only what's implemented and locked
today.

---

## 1. What Gotiate is

A real-time, 2–6 player negotiation game. A shared "market" of themed
entities (fictional companies, dragons, cats — see `theme_data/`) is
arranged on a linear scale from position 1 (best) to position N (worst).
Each player secretly owns some entities (their **portfolio**) and holds a
small number of secret **reserves**. Players negotiate swaps to move their
own holdings toward better positions, using a scarce private currency
(**Influence**) that only costs something when a swap actually benefits the
mover. At the end of the game, the top few positions on the market carry a
random chance of being wiped to zero (**Haircut risk**) — a public
probability distribution, revealed partway through the clock, that turns
"climb as high as possible" into a real risk/reward decision rather than a
dominant strategy.

---

## 2. Game lifecycle

`Game.phase` is one of `LOBBY → NEGOTIATION → CLOSING → SCORED`, or
`CANCELLED` (terminal, reachable only from `LOBBY`). `CLOSING` is
transient: `close_market()` freezes, reveals, and scores the game in one
synchronous pass, so a client poll can observe `NEGOTIATION` on one tick and
`SCORED` on the next without ever seeing `CLOSING` — the frontend renders a
minimal "Closing the market…" message for it and nothing more.

### Lobby
- A game is created by one host (`create_game`) and joined by up to 5 more
  players (`join_game`), 2–6 total. A `display_name` matching the curated
  seed names in `player_name_seeds` goes through the anonymous-play path
  (golden-eligible, see below); anything else is accepted as a typed name
  (trimmed, capped at 24 characters, never golden-eligible) for in-person
  play — see `api/routes.py`'s `_resolve_submitted_name`. Typed names get
  no content-moderation filter, a deliberate gap (see `CURRENT_WORK.md`).
  Either way, a display name already taken by another seated player in
  *this* game is rejected (exact match, source-agnostic).
- **Golden names**: a rare (1/500) draw, rolled independently at `create_game`
  and each `join_game`, exactly once per real seat, and only for a catalog
  name — never at the free-preview/reroll endpoints, and never overriding a
  typed name. `GamePlayer.is_golden_name` is public roster information.
- `join_code` expires `join_code_lifetime_minutes` (default 30) after
  creation; joining with an expired code is rejected.
- The **only** way `LOBBY → NEGOTIATION` happens is the host submitting
  `START_GAME` (2–6 players required) — there is no auto-start and no
  headcount target.
- **Abandoned-lobby handling**: `lobby_reminder_deadline_at` is set at
  creation to `created_at + lobby_reminder_seconds` (180s). Once passed, the
  host may `EXTEND_LOBBY_TIMER` (pushes the deadline out by the same amount
  again, uncapped) or `START_GAME`. If `lobby_reminder_grace_seconds` (60s)
  passes with neither, the game auto-cancels
  (`CancellationReason.LOBBY_TIMEOUT`). `CANCEL_GAME` (host-only,
  `CancellationReason.HOST_INITIATED`) is legal **only** in `LOBBY` — once
  real gameplay starts, the host loses the unilateral power to end it; an
  abandoned `NEGOTIATION`-phase game only ever ends via its own negotiation
  clock.
- Time-based lobby transitions are re-checked cheaply on every
  `GET /games/{id}` poll (`is_time_transition_due` → `apply_due_time_transitions`),
  not only inside command handling — an abandoned lobby has nobody left to
  submit a command, so polling is the only thing that can ever fire its
  auto-cancel.

### START_GAME — what happens, in order
1. Validates: phase is `LOBBY`, actor is the host, player count is 2–6.
2. `setup.generate_starting_state(...)` produces a market order and a
   portfolio assignment for every player (see §3). Emits `MARKET_INITIALIZED`.
3. Deals: every player's portfolio holdings land in `HoldingZone.PORTFOLIO`;
   `reserve_count` (default 2) reserve holdings per player land in
   `HoldingZone.RESERVE_UNREVEALED`, each an independently random entity
   (not necessarily from the player's own portfolio). Emits `PORTFOLIO_DEALT`
   (payload carries the full setup-quality diagnostics, `SERVER_ONLY`) and
   `RESERVES_DEALT`.
4. Sets `max_duration_s` (`max_clock_seconds_by_players[n]` — flat 540s/9
   minutes at every player count, deliberately not scaled by n; every
   other clock-relative pacing point below stays a pure fraction of this,
   so a larger game gets proportionally less time per trade, by design),
   `started_at`, `unilateral_cutoff_at` (`started_at + max_duration_s * (1 -
   unilateral_cutoff_fraction)`), `close_threshold` (`close_threshold(n)`).
5. (2-player games only) `last_negotiated_execution_at` and
   `market_correction_cooldown_until` both set to `started_at` — see §9.
6. A `HaircutProfile` is generated fresh, at random (`_generate_random_haircut_profile`
   — not chosen from a fixed list, see §8) and locked onto the game;
   `haircut_reveal_at = started_at + max_duration_s * haircut_reveal_fraction`
   (default 50% of the clock). Emits `HAIRCUT_PROFILE_SELECTED`
   (`SERVER_ONLY` — nobody sees this live, not even the reveal timing's
   contents).
7. `phase = NEGOTIATION`. Emits `GAME_STARTED`.

### Negotiation
The bulk of gameplay — see §4–§9 below. Ends via `close_market()`, triggered
by either:
- **`TIME_EXPIRED`**: `now >= started_at + max_duration_s`, detected by
  `apply_due_time_transitions`.
- **`READY_THRESHOLD`**: enough players have `SET_READY_TO_CLOSE(ready=true)`
  to reach `close_threshold` (see §10).

### close_market() — exact sequence
1. `phase = CLOSING`; sets `closed_at`/`close_reason`; emits `MARKET_CLOSED`
   (payload carries `reason`).
2. Every still-`OPEN` proposal resolves `MARKET_CLOSED` (committed Influence
   refunds to available — a market close is a non-execution, same as a
   withdrawal). Every still-`OPEN` pool resolves `MARKET_CLOSED` the same way.
3. An offered-but-unresolved Market Correction (2-player only) resolves
   `EXPIRED` — the closest existing fit ("this opportunity is now gone").
4. Every player with a still-pending reserve pickup has it forcibly resolved
   `MARKET_CLOSED` via the same machinery a timeout uses.
5. Every still-`RESERVE_UNREVEALED` holding becomes `SURRENDERED_UNUSED`.
   Emits `PORTFOLIOS_REVEALED`.
6. The **one and only** random Haircut-depth draw for this game happens now
   (`draw_haircut_depth`, a single correlated pick from `haircut_profile`,
   never independent per-position rolls) and is persisted immediately onto
   `realized_haircut_depth`. `compute_final_scores` (pure, `rng`-free) then
   computes the result from that persisted depth. Emits `GAME_SCORED`
   (payload: `realized_haircut_depth`, `wiped_entity_ids`, `results`,
   `winners`).
7. `scored_at` set, `phase = SCORED`. Emits `GAME_ENDED`.

`compute_final_scores` is called again, idempotently, from `project()`
every time a `SCORED` game is read — it never redraws, only reads the
already-persisted `realized_haircut_depth`, so repeated reads always agree
with the original `GAME_SCORED` payload.

---

## 3. Market & starting-state generation

The market is `market_size_by_players[n]` positions wide: `{2: 11, 3: 11,
4: 13, 5: 15, 6: 17}`. Position 1 is best (worth the most at scoring),
position N is worst. Entities are drawn from the game's theme set
(`theme_set_id`, default `fictional_companies_v1`; also `dragons_v1`,
`cats_v1`) — a theme set's `is_locked` entities are always dealt into every
market regardless of size; the rest are sampled to fill out the remainder
(`setup.select_market_entities`), favoring entities that have a `logo_url`
over ones that don't (falls back to the plain pool once the icon'd one
runs out) — selection only, never a fixed subset (still an `rng.sample`
draw within whichever tier is being filled from) and never position,
which stays entirely up to the geometry phase below.

Each player's portfolio shape is `portfolio_shape` (default `[2, 1, 1, 1]`)
— 4 distinct entities, one of them doubled (an "anchor" holding worth double
at scoring). `setup.generate_starting_state` doesn't just deal randomly: it
generates many candidate portfolio assignments ("topology"), hard-rejects
pathological ones, scores survivors, and repeats for market position
("geometry") — see the module docstring in `setup.py` for the full two-phase
algorithm. Hard rejects (topology phase): a shared anchor between two
players, identical portfolios between two players, pairwise overlap above
`max_pair_overlap` for that player count, an isolated (zero-overlap) player
when `reject_isolated_player` is set. Geometry phase currently has **no**
hard rejects (elite-concentration used to be one under the retired scoring
model; it's a soft penalty now — see `_geometry_hard_reject`'s comment).
If no legal complete state can be generated within the attempt budget,
`START_GAME` fails explicitly with `unable_to_generate_valid_starting_state`
— never silently relaxed.

`SetupQualityConfig` is tuned per player count (`setup_quality_by_players`);
n=3 is deliberately the strictest (`max_pair_overlap=1`, isolation is a hard
reject) since a 2-of-4 overlapping pair forms a dominant bloc against a lone
third player in a way it doesn't at higher counts. n=2 has no isolation hard
reject (a disjoint pair still has a real negotiation lever — proposals
aren't restricted to entities you own) and a looser overlap cap.

**Portfolio value** (`linear_rank_v1`, `projections._portfolio_value` /
`engine._projected_value`): `sum(market_size - position + 1)` over every
owned `PORTFOLIO`-zone holding (a doubled anchor counts twice, since it's
two holdings of the same entity). This is what `projected_value` shows a
player about themselves.

---

## 4. The private Influence economy

Every player starts with `starting_influence` (default 10) Influence,
tracked as `available` / `committed` / `spent` — **self-only**, never shown
to any other live audience (a visible balance or balance-delta would be a
hand-reconstruction oracle: "proposal executed, Hanky's balance dropped →
Hanky must own the rising entity").

**The one rule**: a player's liability for a swap is **1 iff they own the
entity that would rise** out of it, else **0** — never more than 1 per
resolved package, never based on quantity owned or movement magnitude.
"Rising" (`engine._rising_entity`) is whichever of the two entities
currently holds the *worse* (higher) position — it would take the other's
better position if swapped.

- **Authoring** (`PROPOSE_SWAP`, `CREATE_POOL`): liability is computed once,
  from the actor's holdings *at that exact moment*, and locked onto the
  `Proposal`/`Pool` (`initiator_influence_liability`) — never recomputed
  later even if holdings or the market change before it resolves. A
  0-liability proposal/pool is always legal regardless of balance (Influence
  is a tax on *benefiting*, not a toll on speaking); a 1-liability one
  requires `available >= 1` and moves `available → committed`.
- **Settlement** (`_resolve_proposal`/`_resolve_pool`): a locked liability of
  1 resolves `committed → spent` if the reason is `executed`, else
  `committed → available` (withdrawal, market-closed, voided, preempted, etc.
  — any non-execution refunds).
- **Accepting** (`ACCEPT_PROPOSAL`): the accepter's liability is computed
  fresh, right now, against their *own* current holdings, and charged
  straight `available → spent` — no commit interval, since accepting
  executes synchronously. **Zero available Influence never blocks
  accepting** — only originating (`PROPOSE_SWAP`/`CREATE_POOL`) requires
  affordability. If the fresh liability is 1 and `available` is already 0,
  the accept still goes through, just for free (nothing is charged, nothing
  goes negative). A broke player can always say yes to someone else's deal;
  there's simply nothing left to charge them.
- **Accepting a Pool** (`ACCEPT_POOL`) — the one genuinely tricky case: the
  accepter's total liability is the OR of two bits, capped at 1:
  - **base-leg bit**: the base proposal's own *locked* value, but only if
    the accepter is that proposal's author (true for every private-pool
    accept, since only the base proposer may accept a private pool);
    otherwise computed fresh against the base proposal's own swap.
  - **pool-leg bit**: always computed fresh against the pool's own swap (the
    pool's initiator can never be its own accepter).
  - If the base-leg liability was *already* committed at propose time, no
    *new* Influence is required even if the pool-leg bit is also 1 — the
    package's max-1 charge is already reserved.
  - Same zero-Influence waiver as a bare proposal: if the combined charge
    would be 1 and `available` is 0, the accept still executes, free.
- **One open bare proposal per player, auto-withdrawing** — not a hard cap.
  A second `PROPOSE_SWAP` while one is still `OPEN` silently withdraws the
  old one first (`WITHDRAWN_BY_INITIATOR`, same cascade to attached open
  Pools as an explicit `WITHDRAW_PROPOSAL`) before creating the new one, as
  long as the new proposal itself is otherwise legal — an illegal new
  proposal (duplicate pair, unaffordable) leaves the old one untouched, no
  partial effect. Pools are **not** further capped beyond the existing "one
  open pool per player per base proposal" rule.
- **Accept-lock grace period** (`accept_lock_seconds`, default 7s — was
  4s, bumped after real playtesting showed the view poll's own lag ate a
  couple of seconds off the top, landing more like 2s in practice): blocks
  only `ACCEPT_PROPOSAL`, and `ACCEPT_POOL` on a **public** pool, for this
  long after the *base proposal's* own `PROPOSAL_CREATED` — gives the room
  a moment to actually read a new proposal before someone already at the
  keyboard snap-accepts it. `Proposal.created_at` is what's checked; a
  public pool created well after its base proposal simply inherits
  whatever's left of the base's own lock (often already elapsed). Nothing
  else is gated by it — `WITHDRAW_PROPOSAL`, `PASS_PROPOSAL`, `CREATE_POOL`,
  and accepting a **private** pool (always the base proposer, the one
  person structurally guaranteed to have already read it) all work
  immediately regardless. The unlock timestamp
  (`Proposal.accept_locked_until` in the projected view) is public and
  unconditional so every viewer can render the same countdown.
- **All-zero Influence top-up**: the instant every seated player's
  `available` hits 0 at the same time, the whole table gets a flat
  `+zero_influence_topup_amount` (default 2), publicly (`INFLUENCE_TOPPED_UP`,
  same amount for everyone, nothing per-player to redact). Checked once
  after every command (`engine._maybe_topup_zero_influence`), not just
  Influence-spending ones — cheap, and only spending can ever newly zero
  everyone out. No cooldown, no cap: firing again the next time the table
  goes fully broke is intended, not a bug.
- **Accept threshold at 5-6 players** (`GameConfig.accepters_required`):
  a bare proposal, and a **public** pool, needs 2 distinct accepters (not
  just 1) before it actually executes at 5 or 6 players — at 2-4, the
  first accept still executes immediately, exactly as always. A single
  1:1 deal has an outsized effect on a market meant to reflect 5-6
  people's collective read; requiring a second accepter makes deals need
  real consensus rather than any two players being able to move it alone.
  **Private pools are exempt at every player count** — only the base
  proposer is ever eligible to accept one, so there's structurally never
  a second distinct accepter to gather.
  - Below threshold, an accept registers as a *pledge*: the accepter's own
    liability locks fresh (`available → committed`, exactly like an
    author's own liability locks at authoring time — free if they can't
    afford it, same zero-Influence waiver as above), `PROPOSAL_ACCEPT_PLEDGED`
    / `POOL_ACCEPT_PLEDGED` fires (fully public, actor included — accepting
    is already public everywhere else, unlike Pass), and the proposal/pool
    stays `OPEN`. The same player can't pledge twice on the same object.
  - Once the required count is reached, every pending accepter's locked
    liability settles to `spent` and the swap executes exactly as the
    single-accept case always has (`_resolve_proposal`/`_resolve_pool` now
    settle every pending accepter alongside the author's own bit, on
    whichever resolution reason the object eventually reaches).
  - If the object resolves any other way first (withdrawn, market closed,
    voided, preempted by a sibling pool, ...) before reaching threshold,
    every pending accepter's locked liability refunds to `available` — a
    pledge that never executes never costs anything, no matter how far it
    got.
  - `pending_accepter_ids`/`accepters_required` in the projected view are
    public and unconditional (for a pool, even when the contents
    themselves aren't) — "who's pledged" doesn't reveal anything the
    Pass-anonymity model protects.
- **No two open bare proposals for the same pair, across players**:
  `PROPOSE_SWAP` also rejects if *any* player already has an `OPEN`
  proposal naming the same two entities (order-independent), not just a
  check against the actor's own proposals. Accepting either one would
  already void the other via the ordinary crossing-invalidation scan the
  instant the first swap lands (§6), so a duplicate never offered a
  meaningfully different outcome — just a confusing, easy-to-misread
  second entry in the open-proposals list. This check is pair-scoped only;
  Pools and `BURN_RESERVE_FOR_SWAP` are unaffected — targeting a pair that
  already has an open bare proposal is still legal for those.
- **Reserve actions are not blocked by an open proposal/pool of your own.**
  `PICK_UP_RESERVE` and `BURN_RESERVE_FOR_SWAP` are both legal even while
  you currently author an `OPEN` proposal or pool — a locked liability is
  immune to a later holdings change by construction (it's computed once,
  at authoring time, and never recomputed), and a later swap crossing your
  own open negotiation's locked direction already voids it loudly via the
  ordinary crossing-invalidation scan (§6) regardless of who caused the
  crossing. An earlier version of this game blocked reserve actions in
  this state defensively; removed once both of those existing mechanisms
  were confirmed to already cover the actual risk independently.
- A self-only, server-authoritative preview is available at
  `GET /games/{id}/propose-cost?entity_a=&entity_b=` — the frontend never
  reimplements this rule.

---

## 5. Negotiation: bare proposals, Pass, Pools

### PROPOSE_SWAP → Proposal
Payload `{entity_a, entity_b}` (distinct, both must exist in the market).
Locks `rising_entity_id` (see §6) and `initiator_influence_liability` at
creation. Public immediately: `PROPOSAL_CREATED` is `PUBLIC`, and a
`Proposal`'s `entity_a`/`entity_b`/`rising_entity_id`/`proposer_id` are
always visible to everyone.

### ACCEPT_PROPOSAL
Executes the swap (§6), settles both the proposer's locked liability and
the accepter's fresh one (free if the accepter is at 0 available — see §4),
resolves the proposal `EXECUTED`, cascades any still-`OPEN` pools attached
to it (`resolve_sibling_pools` — the pool's own author gets
`INVALIDATED_BY_INITIATOR_ACTION` if they're the one who just accepted
directly, everyone else's sibling pool gets `PREEMPTED_BY_OTHER_ACTION`).
Cannot accept your own proposal, or one you've `PASS`'d, or one still
inside its accept-lock grace period (§4).

### WITHDRAW_PROPOSAL
Proposer-only. Resolves `WITHDRAWN_BY_INITIATOR` (refunds committed
Influence); cascades attached open pools to `BASE_PROPOSAL_WITHDRAWN`.

### PASS_PROPOSAL — a non-binding, permanent per-player exit
Any non-proposer may `PASS_PROPOSAL` on an open proposal they haven't
already passed, **provided they don't currently hold an open Pool of their
own on it** ("you can't leave the hand while your own chips are in the
pot" — only the actor's *own* pool blocks this; other players' pools never
do). Once passed:
- The proposal (and every pool attached to it, present and future) is
  **omitted entirely** from that player's own live `project()` view — not
  hidden client-side, actually absent from the response — and they can
  never accept/pool/accept-a-pool on it again.
- **Auto-expiry**: once every seated player except the proposer has passed,
  the proposal auto-resolves internally as `EXPIRED_ALL_PASSED`. This is
  **masked to `WITHDRAWN_BY_INITIATOR` for every live audience** (public
  view, player view, and the live event log) — Pass is designed to give the
  proposer only a private, anonymous signal, never a public "nobody wanted
  this" stigma. `ReplayAudience` (post-`SCORED` only) sees the true reason.
- The proposer sees a live, self-only, anonymous `passed_count` on their own
  proposal — never identities, never visible to anyone else including the
  players who passed. `PROPOSAL_PASSED` itself is `ACTOR_ONLY` (a passer
  sees their own pass in their own history; nobody else ever sees the event
  at all — this is the proposer's *only* channel to Pass feedback).
- **Explicit non-goal**: Pass only ever filters the live *current-state*
  projection. It never touches the event log — if a passed proposal later
  executes via someone else's accept, the passer still sees the resulting
  `SWAP_EXECUTED`/`PROPOSAL_RESOLVED` events normally, same as any public
  fact.

### Pools — a private or public counter-offer against someone else's proposal
`CREATE_POOL` (payload `{proposal_id, entity_c, entity_d, visibility}`):
names a *second*, disjoint entity pair (no overlap with the base proposal's
own two entities) to bundle with the base proposal. Not legal for the base
proposer or anyone who's passed it. `visibility` is `private` or `public`
(each independently toggleable via `allow_private_pools`/`allow_public_pools`).
At most one open pool per player per base proposal.

- **`ACCEPT_POOL`**: executes *both* legs (base proposal's swap, then the
  pool's own swap — sequentially, each excluding the other from its own
  crossing-invalidation scan, see §6), resolves both `EXECUTED`, cascades
  sibling pools on the same base proposal. Legal for the base proposer
  (private or public pool) or, for a public pool only, any other
  non-passed, non-initiator player.
- **`DECLINE_POOL`**: private pools only, base-proposer-only. Resolves
  `DECLINED_BY_TARGET`.
- **`WITHDRAW_POOL`**: the pool's own initiator only. Resolves
  `WITHDRAWN_BY_INITIATOR`, refunding committed Influence to available —
  same rule as `WITHDRAW_PROPOSAL` (`_resolve_pool(...,
  WITHDRAWN_BY_INITIATOR, ..., spend=False)`). An earlier version of this
  charged the withdrawer instead (`spend=True`); confirmed unintentional
  and fixed — see `test_withdraw_pool_refunds_committed_liability` in
  `test_influence_economy.py`.
- **`MAKE_POOL_PUBLIC`**: the pool's own initiator only, private → public,
  one-way.
- **Private-pool-reveal-on-execution**: a private pool's contents
  (`entity_c`/`entity_d`/`rising_entity_id`) stay hidden from everyone but
  its initiator and the base proposal's own author (`_pool_insiders`) —
  forever, if it's declined/withdrawn/preempted/invalidated and never
  executes. The **instant** it executes, its contents become public as part
  of that transaction (`_pool_executed`), both in the live view and in the
  event log (`PRIVATE_POOL_CREATED`'s `POOL_INSIDERS` visibility flips to
  fully visible once `_pool_executed(pool)` is true).

---

## 6. Market-direction locking & crossing invalidation

**A swap's "rising" direction is locked once, at authoring time** —
`SwapIntent.rising_entity_id`, computed via `_rising_entity` at the moment a
bare proposal, pool leg, unilateral burn, or Market Correction move is
created — and **never recomputed** while it's pending. The frontend must
pin its own Visualize UI to this locked value, never to a live position
comparison.

**`_execute_swap`** is the *sole* choke point where two entities' positions
ever change (all 4 call sites: `ACCEPT_PROPOSAL`, both legs of
`ACCEPT_POOL`, `BURN_RESERVE_FOR_SWAP`, and both legs of
`TRIGGER_MARKET_CORRECTION`). After swapping, it emits `SWAP_EXECUTED` and
calls `_invalidate_crossed_negotiations`, which scans every *other*
still-`OPEN` proposal/pool whose own two entities intersect the just-moved
pair. If a scanned negotiation's live-recomputed `_rising_entity` no longer
matches its own locked `rising_entity_id`, it's voided **loudly, never
masked** (the deliberate opposite of Pass):
- An open proposal crossed this way resolves `VOIDED_MARKET_SWUNG` and
  cascades any attached open pools to `BASE_PROPOSAL_VOIDED`.
- An open pool crossed this way (its own leg, independent of its base
  proposal) resolves `VOIDED_MARKET_SWUNG` on its own; the base proposal is
  untouched if only the pool's own direction crossed.

The executing swap's *own* negotiation is excluded from this scan (its
locked entity trivially "crosses" the instant it succeeds — that's the
negotiation working as promised, not a violation); `ACCEPT_POOL`'s two
sequential legs never cross-invalidate each other because a pool's entities
are always disjoint from its base proposal's entities by construction.

This same infrastructure is what a pending Market Correction's own locked
moves are checked against too (§9) — one shared choke point covering every
trigger.

---

## 7. Reserves — Pick Up, Discard, Skip, unilateral burn

Each player starts with `reserve_count` (default 2) reserve holdings, dealt
`RESERVE_UNREVEALED` — the entity is unknown even to its owner. Redaction is
enforced server-side in `project()` (`_UNREVEALED_TO_OWNER_ZONES`), not left
to frontend discretion.

### PICK_UP_RESERVE
Payload `{reserve_holding_id}`. Rejected while a pickup is already pending,
or while the actor authors an open proposal/pool. Reveals the reserve
(`zone → PICKUP_PENDING`, `revealed_to_owner = true`), starts a
`PendingPickup` with a `decision_deadline_at = now + pickup_decision_seconds`
(default 12.0s; `pickup_transport_grace_ms` = 500ms extra grace applied only
at the timeout check, never shown to the player). Emits `PICKUP_STARTED`
(`ACTOR_ONLY`).

**Frozen-view guarantee**: the acting player's entire `project()` response
is pinned to a `cached_view` snapshot rendered once, at pickup time —
`market`, `holdings`, `haircut_profile`, everything — until the pickup
resolves. Other players' own views are completely unaffected. This is
enforced at the `project()` level (short-circuits before computing anything
live), not by the frontend choosing not to poll.

Resolves one of three ways:
- **`DISCARD_HOLDING`** (payload `{pending_pickup_id, holding_id_to_discard}`,
  `holding_id_to_discard` must be one of the original five): the discarded
  holding → `DISCARDED`, the reserve → `PORTFOLIO`. Emits `PICKUP_COMPLETED`.
- **`DECLINE_PICKUP`** ("Skip", payload `{pending_pickup_id}`): keeps the
  original five, reserve → `PICKUP_SURRENDERED`. Reuses the exact same
  `_fail_pending_pickup` machinery a timeout uses, tagged
  `PickupFailureReason.DECLINED_BY_PLAYER`.
- **Timeout**: `apply_due_time_transitions` detects
  `now > decision_deadline_at + pickup_transport_grace_ms` and forces the
  same surrender, tagged `DECISION_TIMEOUT`.

`DISCARD_HOLDING` and `DECLINE_PICKUP` are the **only** two commands exempt
from the optimistic-concurrency `expected_version` check
(`_VERSION_EXEMPT_COMMANDS`) — a frozen client genuinely cannot know the
live game version while the rest of the game keeps moving around it.

A `DISCARD_HOLDING` that changes who owns a pending Market Correction's
source/destination entity is the one invalidation path that doesn't route
through `_execute_swap` at all (it only flips holding zones, never moves a
market position) — see §9.

### BURN_RESERVE_FOR_SWAP — unilateral swap
Payload `{reserve_holding_id, entity_a, entity_b}`. Legal only before
`unilateral_cutoff_at` (`started_at + max_duration_s * (1 -
unilateral_cutoff_fraction)`, i.e. within the first 90% of the clock by
default), and — same lock as `PICK_UP_RESERVE` — not while the actor
authors an open proposal/pool. The named entities need not be owned by the
actor at all. The reserve → `BURNED_UNSEEN` **permanently** — its identity
is never revealed to its own owner, live or in replay's own event log
redaction rules, though it *is* fully revealed post-game via the ordinary
`SCORED`-phase holdings reveal (see §11). No Influence cost. Executes
through the same `_execute_swap` choke point as any other swap (so it can
void other open negotiations, and it's the unilateral counterpart to the
Market Correction's own moves for support-marker purposes — see the
frontend section).

---

## 8. Haircut risk & final scoring

At `START_GAME`, one `HaircutProfile` (`depth_probabilities: list[float]`,
summing to 1.0) is generated fresh, at random, and locked for the game.
`depth_probabilities[d]` is the probability the realized wipe depth equals
`d`; `certainty(p) = sum(depth_probabilities[0:p])` is the probability a
position `p` (1-indexed) survives.

- **Risk band**: `haircut_risk_band_depth` in `project()` — the number of
  top positions that carry *some* risk, public and structural from game
  start — is computed straight from config (`round(market_size *
  risk_depth_fraction)`, `risk_depth_fraction = 0.35`), deliberately **not**
  `HaircutProfile.max_depth` (the actual profile's own highest nonzero
  index). The two used to always agree back when profiles came from a
  curated list validated against this exact formula; a randomly generated
  profile's own effective depth can land earlier (see below), and reading
  it here would leak the profile's shape before reveal.
- **Reveal**: hidden until `haircut_reveal_at` (50% of the clock by
  default) — `HAIRCUT_RISK_REVEALED` fires live at that instant. A game
  that closes via `READY_THRESHOLD` before the halfway mark never sees the
  live event, but `project()` unconditionally reveals the profile once
  `phase == SCORED` regardless.
- **Generated fresh every game, not chosen from a list**
  (`engine._generate_random_haircut_profile`) — an earlier version picked
  one of 5 named curves per player count (Cliff / Deep burn / Brutal
  plateau / Moderate / Mild); retired once real playtesting showed players
  starting to recognize and play around specific shapes. The generator
  works in the survival CDF (`certainty(p)` above, monotonic by
  construction): position 1's survival lands in `[5%, 50%]`, position 2's
  in `[11%, 61%]` (automatically greater than position 1's, since it's a
  cumulative sum), and every deeper position keeps climbing by a fresh
  random amount toward certainty — deliberately lumpy rather than a fixed
  step, so some games jump to safety early and others creep up gradually.
  Every adjacent pair is floored at least `_HAIRCUT_MIN_ADJACENT_GAP` (4
  percentage points) apart, except a step that lands exactly on the
  within-band ceiling — an unbounded draw landed two adjacent positions
  only ~1 point apart in real play, reading as no differentiation at all
  even though it was technically a valid distribution.
  **No position inside the risk band ever reaches full safety**: every
  in-band position's own cumulative survival is capped at
  `_HAIRCUT_WITHIN_BAND_CEILING` (92%) — real playtest catch, the deepest
  in-band position (`round(market_size * risk_depth_fraction)` deep, same
  as the risk band above) was showing exactly 100% survival, because the
  profile's `depth_probabilities` list was built with exactly that many
  entries, so summing *all* of them (= that position's own survival
  chance) was forced to 1.0 by construction — "the top K positions carry
  some risk" was a lie for position K itself. The profile now carries one
  extra slot *past* the band (`depth_probabilities` has `risk_band_depth
  + 1` entries), which is what's actually structurally safe, absorbing
  whatever's left (always ≥ 8%) — the survival curve reaches exactly 100%
  only there, sometimes with a genuine zero-probability stretch leading
  up to it within the band itself ("a big jump from the last at-risk
  position straight to certain safety" is an intended shape, not an edge
  case). Still varies on the same two axes the old curated shapes did —
  how severe position #1 is, and how deep the danger zone cascades into
  #2/#3 — just drawn instead of picked, for real game-to-game variability
  instead of a small family of curves.
- **The one random draw**: `draw_haircut_depth` runs exactly once, at
  `close_market`, and persists its result immediately — everything
  downstream is pure/deterministic from that persisted value (§2).
- **Scoring** (`compute_final_scores`, `haircut_risk_v1`): positions
  `1..realized_haircut_depth` score zero; every other owned holding scores
  its linear-rank value (`market_size - position + 1`, a doubled holding
  counts twice); highest total wins; **exact ties share the win** (every
  player at the max value is a winner, not just one).
- **Live self-only fields**: `projected_value` (unconditional linear-rank
  sum — what you'd score if nothing were wiped) always available to self;
  `safe_value` (only positions structurally beyond `max_depth`) appears only
  once the profile is visible. No probability-weighted expected value is
  ever shown — the risk judgment is deliberately left to the player.

---

## 9. Market Correction — 2-player-only anti-stagnation mechanic

Exists **only** when `len(game.players) == 2`. Purpose: the 2-player game
can otherwise stagnate once neither side sees an obviously good trade. It is
**never automatic** — inactivity only ever makes a correction *available*;
a player must affirmatively trigger it.

### Trigger — time-only
- `last_negotiated_execution_at` updates **only** on an executed
  `ACCEPT_PROPOSAL`/`ACCEPT_POOL` (never a burn, never the correction
  itself). Once `market_correction_cooldown_until` has passed *and*
  `now - last_negotiated_execution_at >= market_correction_stagnation_seconds`
  (90.0s default), the server attempts to construct a correction.
- If construction succeeds, it's offered: `game.pending_market_correction`
  is set, `MARKET_CORRECTION_OFFERED` fires (payload deliberately minimal —
  `{correction_id, expires_at}`, **never** entities or displacement).
  `project()` exposes exactly that same minimal shape while pending.
- If construction fails (see below), **nothing is offered this cycle and
  the cooldown is deliberately not pushed** — the very next tick retries
  against whatever the board looks like by then, rather than sitting dark
  for a full cooldown period.
- The offer lasts `market_correction_offer_seconds` (15.0s default); if
  unclaimed, it resolves `EXPIRED`.
- **Cooldown only pushes forward for `TRIGGERED`/`MARKET_RESUMED`** — both
  represent genuinely fresh activity (a correction actually firing, or a
  real negotiated deal landing) that earns the market a real breather.
  `EXPIRED` and `INVALIDATED` deliberately leave `market_correction_cooldown_until`
  untouched: nothing changed, the market is exactly as stagnant as when
  the correction was first offered, so the very next tick re-offers
  immediately if it's still genuinely stagnant. An earlier version pushed
  the cooldown unconditionally on every resolution reason, which meant an
  `EXPIRED`/`INVALIDATED` correction tacked a full extra
  `market_correction_cooldown_seconds` of silence onto the stagnation that
  was already there — real playtest feedback: the trigger felt like it
  fired at "seemingly random times" rather than a clean 90s mark, because
  the actual re-offer timing depended on how many times one had already
  expired/invalidated, not on "90s since the last deal."

### Construction — hidden, fixed, built before anyone chooses
`_construct_market_correction` builds exactly one downward move per player,
in full, before either player has seen anything ("offer first, outcome
already fixed" — accepting is a wager on a fixed unknown, never a roll made
after the fact):

1. **Severity** (`_market_correction_target_displacements`, pure): each
   player's private, server-internal-only `_projected_value` gap
   (`gap = |leader − trailer|`, never shown to players in any form) drives a
   spread: `spread = max_spread * min(1, gap / gap_saturation)`, then
   `leader_displacement = base − spread/2`, `trailer_displacement = base +
   spread/2`, both clamped to `[1, market_size − 1]`
   (`market_correction_base_displacement_fraction=0.5`,
   `market_correction_max_spread_fraction=0.4`,
   `market_correction_gap_saturation_fraction=1.0`, all × `market_size`).
2. **Targeting** (`_construct_one_correction_move`, locked, never relaxed):
   start from the player's **top two distinct owned entities by position**,
   after first excluding any entity **both players own** in PORTFOLIO
   (`_construct_market_correction`'s `mutually_owned` set) — without this,
   a shared holding could be the *other* player's move target, landing a
   second, uninvited hit on top of your own already-independently
   -targeted move; real playtest catch, since the mechanic is supposed to
   be exactly one downward move per player, never two. A doubled/anchor
   holding is excluded from eligibility if the player's *other* (non
   -mutually-owned) top-two entity is singly-owned; only when **both**
   top-two are doubled does a double become eligible. Selection within
   the eligible set is uniform-random, never damage-optimized.
3. **Destination search** (`_find_correction_destination`): must be
   strictly *worse* (higher position) than the source and unowned by
   **either** player. Prefers the entity closest to the exact target
   displacement (nearest-distance wins; ties go to the lower/first-scanned
   position); if the randomly-drawn top-two candidate has no legal
   destination, the *other* eligible candidate is tried before giving up.
   Never chains multiple swaps to force an exact displacement.
4. If **either** player's move can't find a legal destination this cycle
   (both eligible candidates exhausted), the whole correction construction
   returns `None` — a failed construction is a valid, expected outcome,
   never a partial/one-sided correction.
5. Destinations are kept disjoint between the two players' own moves
   (sequential construction, second move's search excludes the first's
   chosen destination).

### Player choice — `TRIGGER_MARKET_CORRECTION`
Payload `{correction_id}`. Either player may trigger; there's no veto and
no Influence cost. **Not** exempt from the `expected_version` check (unlike
`DISCARD_HOLDING`/`DECLINE_PICKUP`) — this is a normal, live-polled,
game-level offer visible to both players through the ordinary view, not a
frozen per-player snapshot. Re-validates `_market_correction_still_valid`
as defense-in-depth, then **clears `pending_market_correction` to `None`
before executing either leg** (captures the moves locally first) — this is
the fix for a self-invalidation bug caught on review: without clearing
first, `_execute_swap`'s own crossing scan would see the correction still
pending after its first leg succeeds and incorrectly void it against
itself, since the first leg's locked direction trivially "crosses" the
instant it does exactly what it promised. Both captured moves then execute
through the normal `_execute_swap` choke point, and
`MARKET_CORRECTION_RESOLVED` fires with `reason=triggered` and the **full**
`moves` detail — the only resolution reason whose contents are ever
revealed live (§6's masking-vs-loud precedent: this one is loud).

### Invalidation — two distinct mechanisms, never blended
1. **"Is the market still frozen?"** — **any** executed negotiated deal
   (`ACCEPT_PROPOSAL`/`ACCEPT_POOL`) while a correction is pending resolves
   it `MARKET_RESUMED`, **unconditionally**, regardless of whether that deal
   structurally touches either move's entities. `_handle_accept_proposal`/
   `_handle_accept_pool` resolve any pending correction this way **before**
   running their own `_execute_swap` — ordering caught on review: resolving
   first means the correction is already gone by the time the deal's own
   swap runs, so the reason is deterministically `MARKET_RESUMED` even when
   the deal happens to cross a locked move (never misclassified as
   `INVALIDATED` by execution-order accident).
2. **"Is the prepared correction still structurally valid?"**
   (`_market_correction_still_valid`) — for everything that *isn't* a
   negotiated deal: a locked move's direction crossing (via the normal
   `_execute_swap` → `_invalidate_crossed_negotiations` scan — a unilateral
   burn is the live trigger for this), or `DISCARD_HOLDING` changing who
   owns a move's source/destination (the one path that never touches
   `_execute_swap` at all). Resolves `INVALIDATED`.

Both paths mean the offer a player triggers is always exactly the offer
that was constructed, or it's already gone — never silently recomputed
underneath a trigger.

### No support markers, ever
Neither ordinary nor unilateral credit — nobody authored the correction's
direction. `MARKET_CORRECTION_RESOLVED(reason=triggered)` claims both
swaps in the frontend's support-marker derivation the same way an executed
proposal/pool leg claims its own (see the frontend section below), purely
to keep them out of the "unclaimed = unilateral burn" bucket, with **zero**
crediting.

---

## 10. Ready-to-close & market close

`SET_READY_TO_CLOSE` (payload `{ready: bool}`) toggles a player's own
readiness — **strictly self-only, no aggregate anywhere**. No other player
ever learns a count, a percentage, or even that someone toggled it; the
only thing anyone else ever learns is the sudden fact of closure itself,
once `close_threshold` (`close_threshold(n)`: `n` itself for `n <= 3`,
`ceil(0.75n)` above that) is actually reached. `READY_TO_CLOSE_CHANGED` is
`ACTOR_ONLY`. Toggling is bidirectional (on then off is legal) right up
until the threshold triggers closure in the same transaction; a no-op
toggle (already at that value) doesn't even bump the game version.

---

## 11. Visibility model — `project()` / `project_events()`

Every client view — live or replay — is `project(game, audience)`, called
with one of three audiences, always derived server-side from the verified
JWT and game phase, **never client-supplied**:
- `PlayerAudience(game_player_id)` — a seated player's own view.
- `PublicAudience()` — an unauthenticated/non-seated spectator.
- `ReplayAudience()` — only reachable once `phase == SCORED`; raises
  `PermissionError` otherwise. Bypasses nearly every live redaction (full
  transparency post-game is deliberate).

**Default-deny, exhaustive, tested**: `EVENT_VISIBILITY` maps every single
`EventType` to exactly one policy (`PUBLIC`, `ACTOR_ONLY`, `SERVER_ONLY`,
`POOL_INSIDERS`); an event type missing from the dict is **omitted**
entirely from `project_events`, never defaulted to public.
`test_event_visibility_registry_is_exhaustive` asserts the dict's keys
equal `set(EventType)` exactly — adding a new `EventType` without an
explicit visibility decision fails the test suite, not just review.

| Policy | Meaning |
|---|---|
| `PUBLIC` | Visible to every audience, live. |
| `ACTOR_ONLY` | Visible only to the `PlayerAudience` matching the event's own actor. |
| `SERVER_ONLY` | Never visible live to anyone, including the actor — only `ReplayAudience` (post-`SCORED`) sees it. |
| `POOL_INSIDERS` | Existence/status/initiator stay public; only `entity_c`/`entity_d` (the pool's contents) are redacted for a non-insider, per `_pool_insiders`/`_pool_executed`. |

Two bespoke, non-table-driven redactions layered on top of the policy
lookup (both documented inline in `project_events`):
- `PROPOSAL_RESOLVED` with `reason == expired_all_passed` is rewritten to
  `withdrawn_by_initiator` for every live audience (§5's Pass masking).
- `MARKET_CORRECTION_RESOLVED`'s `moves` key is stripped unless
  `reason == triggered` or the audience is `ReplayAudience` (§9).

**Holdings redaction** (`_holding_view`/`_UNREVEALED_TO_OWNER_ZONES`):
a player's own view only ever shows `entity_id` for zones they've actually
seen — `RESERVE_UNREVEALED`, `BURNED_UNSEEN`, and `SURRENDERED_UNUSED` stay
`entity_id: null` even to their own owner, live. Once `phase == SCORED`,
**every** holding in **every** zone, for **every** player, is fully
revealed — the "permanently redacted" guarantee is live-play-only by
design; postgame is deliberately maximally transparent (the "oh, I burned
Motorboat" reveal).

**Pending-pickup frozen view** (§7) and **`pending_market_correction`'s
minimal shape while pending** (§9) are both enforced inside `project()`
itself, not by the frontend choosing to render less.

---

## 12. Command reference (payload + core legality)

Every command below goes through the generic `POST /games/{id}/commands`
envelope (`{command_id, type, expected_version, payload}`) except
`create_game`/`join_game`, which have their own dedicated routes since the
actor isn't seated yet. `expected_version` is checked against
optimistic-concurrency (`StaleVersionError` → HTTP 409) for every command
**except** `DISCARD_HOLDING`/`DECLINE_PICKUP` (§7).

| Command | Payload | Notes |
|---|---|---|
| `CANCEL_GAME` | `{}` | Host-only, `LOBBY`-only. |
| `EXTEND_LOBBY_TIMER` | `{}` | Host-only, `LOBBY`-only. |
| `START_GAME` | `{}` | Host-only, `LOBBY`-only, 2–6 players. See §2. |
| `PROPOSE_SWAP` | `{entity_a, entity_b}` | See §5. |
| `WITHDRAW_PROPOSAL` | `{proposal_id}` | Proposer-only. |
| `PASS_PROPOSAL` | `{proposal_id}` | Non-proposer, non-repeat, no own open pool on it. See §5. |
| `ACCEPT_PROPOSAL` | `{proposal_id}` | Not your own, not passed. |
| `CREATE_POOL` | `{proposal_id, entity_c, entity_d, visibility}` | See §5. |
| `WITHDRAW_POOL` | `{pool_id}` | Pool initiator only. |
| `MAKE_POOL_PUBLIC` | `{pool_id}` | Pool initiator only, private→public. |
| `DECLINE_POOL` | `{pool_id}` | Base proposer only, private pools only. |
| `ACCEPT_POOL` | `{pool_id}` | See §5. |
| `PICK_UP_RESERVE` | `{reserve_holding_id}` | See §7. |
| `DISCARD_HOLDING` | `{pending_pickup_id, holding_id_to_discard}` | **Version-exempt.** See §7. |
| `DECLINE_PICKUP` | `{pending_pickup_id}` | **Version-exempt.** See §7. |
| `BURN_RESERVE_FOR_SWAP` | `{reserve_holding_id, entity_a, entity_b}` | See §7. |
| `TRIGGER_MARKET_CORRECTION` | `{correction_id}` | 2-player only. See §9. |
| `SET_READY_TO_CLOSE` | `{ready}` | See §10. |

All fifteen apply-time transitions are re-checked ahead of *every* command
via `apply_due_time_transitions` (`engine.handle_command`'s first step),
and cheaply mirrored (side-effect-free) by `is_time_transition_due` so
`GET /games/{id}` can decide whether it's worth acquiring the write lock at
all: lobby reminder/grace auto-cancel, per-player pending-pickup timeout,
unilateral-window close, Haircut reveal, (2-player only) Market Correction
offer/expiry, and the negotiation clock's own `TIME_EXPIRED` close.

---

## 13. Frontend-derived UI logic (non-authoritative)

FastAPI decides every gameplay-legal outcome. A small amount of UI-only
logic in `apps/web/src/lib/` derives display state purely from data
`project()`/`project_events()` already made public — it is never a second
source of truth and must never diverge from the server's own decisions.

**`computeSupportMarkers` (`apps/web/src/lib/supportMarkers.ts`)** — derives,
from the public event log plus live `view.pools`, which players have
publicly and successfully pushed each market entity up (rendered as a small
badge on the market card). Locked invariants, current and verified against
the code directly (not assumed):
- **Only an `executed` (or, for Market Correction, `triggered`) resolution
  may claim a `SWAP_EXECUTED` occurrence.** `claimSwap` is called from
  exactly three places: `PROPOSAL_RESOLVED && reason === "executed"`,
  `POOL_RESOLVED && reason === "executed"`, and
  `MARKET_CORRECTION_RESOLVED && reason === "triggered"`. A `voided_market_swung`,
  `withdrawn_by_initiator`, `declined_by_target`,
  `preempted_by_other_action`, `base_proposal_voided`, `market_resumed`,
  `invalidated`, or `expired` resolution **never** reaches `claimSwap` —
  those branches all require the exact literal reason string above, nothing
  else matches. This holds structurally on the backend side too: a voided
  negotiation's own entity pair never produces a `SWAP_EXECUTED` in the
  first place (voiding never calls `_execute_swap` — only the *unrelated,
  already-executing* swap that caused the crossing does), so there is
  nothing for a non-executed resolution to wrongly claim even in principle.
- A `SWAP_EXECUTED` with no matching claim by the end of the event walk is
  structurally guaranteed to be a unilateral burn or an unclaimed Market
  Correction leg — tracked via a per-pair LIFO queue (not a flat set), since
  the same pair can legitimately swap more than once in a game.
- A player's marker (ordinary or unilateral) on an entity is **cleared
  entirely, not decremented**, the moment that same player helps that
  entity fall — regardless of which mechanism created the marker.
- `unilateralCount` (burn-for-swap) is tracked separately from ordinary
  `count` (negotiated support) and can coexist on the same player/entity.

**`useGameView.ts`** — polling with adaptive backoff; stops polling entirely
once `phase` reaches `SCORED`/`CANCELLED` (a real production incident —
an abandoned tab on a finished game hammering the backend at 1Hz
indefinitely — is what this specifically fixed).

**`haircutRisk.ts`** — pure client-side `certaintyAt`/`maxHaircutDepth` math
over an already-public, already-revealed `HaircutProfile`; nothing here is
ever computed before the server has already made the underlying data
public.

**`useEntityNotes.ts`** — private per-player "who does this remind me of"
notes: right-click (or long-press on touch) a market card for a list of
*other* seated players' names (never your own — you already know your own
holdings, so the menu excludes `view.you`), pick up to two, they stick on
the card (picking a tagged name again untags it). A `Nobody` entry (black
dot, ✕) is also offered — mutually exclusive with real names
(`MarketView.handleToggleNote`): tagging it clears any names already
there, and tagging a name clears `Nobody`, since "nobody holds this" and
"X holds this" don't compose the way two different players' names do.
Each seated player gets a distinct badge color (`_PLAYER_NOTE_COLORS` in
`page.tsx`, keyed by seat, 6 entries for the game's own max seat count) —
not just the initial, which two players can share (Tedy/Tery would
otherwise render identical badges) — and the same color swatch shows next
to each name in the tag menu so the mapping is actually learnable. Purely
a personal memory aid, **not
gameplay state at all** — `localStorage` only, scoped to this browser and
this game, never sent to the server, never read by `project()`. While a
card's note menu is open, the whole market grid
(order, positions, points/payout numbers) freezes to a snapshot taken the
instant it opened, so the menu's anchor point doesn't slide out from under
a live reorder mid-interaction; it catches up to live state in one
ordinary reorder transition once closed.

None of the above ever computes a legality decision, a cost, or a score —
those always come from the server (`project()`'s fields,
`GET /games/{id}/propose-cost` for the one live preview that exists).
