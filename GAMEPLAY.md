# Gotiate — Gameplay Specification (current behavior)

**This document describes the game as it behaves today, as implemented in
`apps/api/src/gotiate/domain/` and verified by `apps/api/tests/`.** It is
authoritative for *current* rules and invariants. It intentionally contains
no history, no rationale for why a rule changed over time, and no
description of retired mechanics (there is no "Waterline" scoring model,
no Influence economy, no Market Correction, no gameplay clock, and no
Reserve/Pickup mechanic in this codebase anymore; if you find a reference
to any of them anywhere, that reference is stale).

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

A 2–6 player negotiation game, clockless except for two short local
decision windows. A shared "market" of themed entities (fictional
companies, dragons, cats — see `theme_data/`) is arranged on a linear
scale from position 1 (best) to position N (worst). Each player secretly
owns some entities (their **portfolio**). Players negotiate swaps to move
their own holdings toward better positions — each player gets a fixed
number of **Moves** (default 5), spent only to open a new negotiation and
never refunded, with **at most one negotiation open table-wide at any
time**: propose, then everyone else responds (Accept, counter with a
Pool, or Pass) until it resolves or narrows down to its final two active
participants, who may then force a resolution via **Arbitration**. Each
player also gets a small number of **Boosts** (default 2) — unilateral,
non-negotiated actions (Concentrate, Draw/Refresh, Force Swap) that expire
table-wide the instant any one player spends their last Move. At the end
of the game, the top few positions on the market carry a random chance of
being wiped to zero (**Haircut risk**) — a public probability
distribution, revealed once cumulative Moves spent crosses the halfway
mark, that turns "climb as high as possible" into a real risk/reward
decision rather than a dominant strategy.

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
  abandoned `NEGOTIATION`-phase game only ever ends once Moves run out (see
  below) or via `SET_READY_TO_CLOSE`.
- Time-based lobby transitions are re-checked cheaply on every
  `GET /games/{id}` poll (`is_time_transition_due` → `apply_due_time_transitions`),
  not only inside command handling — an abandoned lobby has nobody left to
  submit a command, so polling is the only thing that can ever fire its
  auto-cancel.

### START_GAME — what happens, in order
1. Validates: phase is `LOBBY`, actor is the host, player count is 2–6.
2. `setup.generate_starting_state(...)` produces a market order and a
   portfolio assignment for every player (see §3). Emits `MARKET_INITIALIZED`.
3. Deals: every player's portfolio holdings land in `HoldingZone.PORTFOLIO`
   — there is no reserve zone anymore. Emits `PORTFOLIO_DEALT` (payload
   carries the full setup-quality diagnostics, `SERVER_ONLY`).
4. Sets `moves_remaining = starting_moves` (default 5) and
   `boosts_remaining = starting_boosts` (default 2) for every player,
   `started_at`, `close_threshold` (`close_threshold(n)`). There is no
   gameplay clock and nothing derived from one — `started_at` is
   elapsed-time telemetry only.
5. A `HaircutProfile` is generated fresh, at random (`_generate_random_haircut_profile`
   — not chosen from a fixed list, see §9) and locked onto the game. There
   is no fixed reveal deadline; the live reveal trigger is Move-driven
   (see §4). Emits `HAIRCUT_PROFILE_SELECTED` (`SERVER_ONLY` — nobody sees
   this live, not even the fact that it happened).
6. `phase = NEGOTIATION`. Emits `GAME_STARTED`.

### Negotiation
The bulk of gameplay — see §4–§8 below. Ends via `close_market()`, triggered
by either:
- **`READY_THRESHOLD`**: enough players have `SET_READY_TO_CLOSE(ready=true)`
  to reach `close_threshold` (see §10).
- **`MOVES_EXHAUSTED`**: every seated player's own `moves_remaining` has hit
  zero **and** there is no active negotiation left to resolve
  (`engine._maybe_close_on_moves_exhausted`, checked generically after
  every command). Deliberately not the same instant as the table-wide
  Boosts expiry (§4), which fires on the *first* player to hit zero — the
  table can keep spending its last few Moves, opening and resolving
  negotiations, for a while after Boosts have already expired.
- **`ABANDONED`**: `negotiation_abandonment_seconds` (default 600s/10min)
  passes with no command successfully handled for this game
  (`Game.last_activity_at`, bumped by `handle_command` on every
  successfully handled command — a rejected one never counts). Checked as
  a time-driven transition (`apply_due_time_transitions`), same as the
  pre-existing `LOBBY` reminder/grace auto-cancel. **Not** a revived
  gameplay clock — nobody sees a countdown, nobody races it, it never
  affects strategy; it exists purely so an abandoned browser tab (or a
  player who never returns) can't leave a game open forever with nobody
  able to close it.

### close_market() — exact sequence
1. `phase = CLOSING`; sets `closed_at`/`close_reason`; nulls
   `active_proposal_id` (defense-in-depth); emits `MARKET_CLOSED` (payload
   carries `reason`).
2. Every still-`OPEN` proposal resolves `MARKET_CLOSED`. Every still-`OPEN`
   pool resolves `MARKET_CLOSED` the same way. A pending Arbitration on the
   resolving proposal, if any, resolves with `ArbitrationResolutionReason
   .MARKET_CLOSED` at the same moment — Ready-to-Close (or Move exhaustion)
   always preempts a pending Arbitration draw outright; no draw ever runs.
3. Every player with a still-pending Draw/Refresh decision (§7) has it
   forcibly resolved — the drawn entity never lands in their portfolio; the
   Boost was already spent when the draw started, same as a decline or
   timeout.
4. Emits `PORTFOLIOS_REVEALED`.
5. The **one and only** random Haircut-depth draw for this game happens now
   (`draw_haircut_depth`, a single correlated pick from `haircut_profile`,
   never independent per-position rolls) and is persisted immediately onto
   `realized_haircut_depth`. `compute_final_scores` (pure, `rng`-free) then
   computes the result from that persisted depth. Emits `GAME_SCORED`
   (payload: `realized_haircut_depth`, `wiped_entity_ids`, `results`,
   `winners`).
6. `scored_at` set, `phase = SCORED`. Emits `GAME_ENDED`.

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

## 4. Moves & the single active negotiation

Every player starts with `starting_moves` (default 5) Moves — a public
roster fact (`GamePlayer.moves_remaining`), not a private currency. A Move
is spent **only** by successfully opening a new negotiation
(`PROPOSE_SWAP`) and is **never refunded**, regardless of how that
negotiation eventually resolves — accepted, expired, voided, or resolved
by Arbitration, the Move is gone either way.

**At most one bare negotiation is open table-wide at any time**
(`Game.active_proposal_id`, cleared centrally the instant the matching
proposal resolves, any reason — see `engine._resolve_proposal`, the single
chokepoint every resolution path funnels through). `PROPOSE_SWAP` is
illegal whenever `active_proposal_id` is already set, and illegal if the
actor has zero `moves_remaining`. This single constraint replaced the old
Influence economy's whole cost-gating job: since nobody can open a second
negotiation while one is in flight, and nobody can simply wait the current
one out (Pass is public and permanent — see §5), a negotiation now plays
out as a real, forced conversation with a shrinking cast of active
participants.

**Boosts expire table-wide, once, the instant any single player's own
`moves_remaining` first hits zero** (`Game.boosts_expired`, flips
`False → True` exactly once, checked generically after every command —
`engine._maybe_expire_boosts`) — not when everyone's does. This is the
unilateral cutoff, expressed as game state rather than a wall-clock timer.

**Haircut's live reveal is Move-driven**: `HAIRCUT_RISK_REVEALED` fires the
instant cumulative Moves consumed across the whole table first reaches or
crosses 50% of the table's total initial Move allocation
(`len(players) * starting_moves`, checked generically after every command
— `engine._maybe_reveal_haircut`) — see §9.

---

## 5. Negotiation: bare proposals, Pass, Pools

### PROPOSE_SWAP → Proposal
Payload `{entity_a, entity_b}` (distinct, both must exist in the market).
Illegal whenever a negotiation is already active table-wide (§4), or the
actor has no Moves remaining. Locks `rising_entity_id` (see §6) at
creation; consumes one Move, never refunded. Public immediately:
`PROPOSAL_CREATED` is `PUBLIC`, and a `Proposal`'s
`entity_a`/`entity_b`/`rising_entity_id`/`proposer_id`/`passed_player_ids`
are always visible to everyone. **There is no `WITHDRAW_PROPOSAL`** —
spending a Move commits the table to that negotiation; the opener cannot
change their mind.

### ACCEPT_PROPOSAL
Executes the swap (§6), resolves the proposal `EXECUTED`, cascades any
still-`OPEN` pools attached to it (`resolve_sibling_pools` — the Agency
Principle: the pool's own author gets `INVALIDATED_BY_INITIATOR_ACTION` if
they're the one who just accepted directly, everyone else's sibling pool
gets `PREEMPTED_BY_OTHER_ACTION`). Cannot accept your own proposal, or one
you've `PASS`'d. **Legal even while Arbitration is active** on this
negotiation — settling normally during the 20-second window is the
intended way out, see §8.

### PASS_PROPOSAL — public, permanent, and drives narrowing
Any non-proposer may `PASS_PROPOSAL` on an open proposal they haven't
already passed, provided they don't currently hold an open Pool of their
own on it ("you can't leave the hand while your own chips are in the
pot" — only the actor's *own* pool blocks this; other players' pools never
do), and provided Arbitration hasn't already been called on it (§8 — the
candidate set locks the instant Arbitration starts). Once passed:
- **Fully public and permanent** — the opposite of anonymity. Every
  audience, live, sees `passed_player_ids` on the proposal, and
  `PROPOSAL_PASSED` itself is `PUBLIC`. A passed player keeps seeing this
  exact negotiation in their own view (never omitted the way it once was)
  but can no longer Accept/Pool/Pass it again.
- **Narrows the active participant set**: the active responders are every
  seated player except the proposer, minus everyone who's passed.
  Arbitration (§8) becomes callable the instant that set narrows to
  exactly one remaining responder — the opener plus that one player are
  "the final two."
- **Auto-expiry**: once every seated player except the proposer has passed,
  the proposal auto-resolves `EXPIRED_ALL_PASSED` — publicly, truthfully,
  with no masking.
- **Drives jury eligibility**: everyone who has passed this negotiation
  becomes its secret jury the instant Arbitration is called on it (§8).
- **Explicit non-goal, unchanged**: Pass only ever filters/narrows the
  live active-participant status of *this* negotiation. It never touches
  the event log — a passed player still sees the resulting
  `SWAP_EXECUTED`/`PROPOSAL_RESOLVED` events normally if it later executes.

### Pools — a private or public counter-offer against someone else's proposal
`CREATE_POOL` (payload `{proposal_id, entity_c, entity_d, visibility}`):
names a *second*, disjoint entity pair (no overlap with the base proposal's
own two entities) to bundle with the base proposal. Not legal for the base
proposer, anyone who's passed it, or while Arbitration is active on the
base proposal. `visibility` is `private` or `public` (each independently
toggleable via `allow_private_pools`/`allow_public_pools`). **At most one
open pool per player per base proposal** — this single-pool-per-player rule
is also what guarantees Arbitration never faces a "which Pool" ambiguity:
by the time a negotiation has narrowed to its final two, every *other*
responder was already forced to withdraw their own Pool before passing (see
below), so only the one remaining responder could possibly still hold one —
at most one Pool can ever be eligible.

- **`ACCEPT_POOL`**: executes *both* legs (base proposal's swap, then the
  pool's own swap — sequentially, each excluding the other from its own
  crossing-invalidation scan, see §6), resolves both `EXECUTED`, cascades
  sibling pools on the same base proposal. Legal for the base proposer
  (private or public pool) or, for a public pool only, any other
  non-passed, non-initiator player. **Legal even while Arbitration is
  active** — settling normally, same as `ACCEPT_PROPOSAL`.
- **`PASS_POOL`**: public, permanent, and scoped only to that Pool. For a
  private Pool, only the base proposer may Pass; because they are its sole
  eligible accepter, that immediately resolves it `EXPIRED_ALL_PASSED`.
  For a public Pool, every player currently eligible to accept it may Pass
  independently; once all eligible accepters have passed, it resolves the
  same way. A player who later `PASS_PROPOSAL`s stops counting as eligible
  for every public Pool on that base. Pool-pass identities are projected
  publicly as `passed_player_ids`, and a passer can still accept the base
  proposal or a sibling Pool. The Pool initiator uses `WITHDRAW_POOL`
  instead. Illegal while Arbitration is active. `DECLINE_POOL` remains a
  compatibility alias with these same semantics for older clients.
- **`WITHDRAW_POOL`**: the pool's own initiator only. Resolves
  `WITHDRAWN_BY_INITIATOR`. Illegal while Arbitration is active. A player
  holding an open Pool cannot `PASS_PROPOSAL` until they withdraw it first
  — this is also what guarantees a passed player never carries a live Pool
  into jury eligibility.
- **`MAKE_POOL_PUBLIC`**: the pool's own initiator only, private → public,
  one-way. Illegal while Arbitration is active.
- **Private-pool-reveal-on-execution**: a private pool's contents
  (`entity_c`/`entity_d`/`rising_entity_id`) stay hidden from everyone but
  its initiator and the base proposal's own author (`_pool_insiders`) —
  forever, if it's declined/withdrawn/preempted/invalidated and never
  executes. The **instant** it executes, its contents become public as part
  of that transaction (`_pool_executed`), both in the live view and in the
  event log (`PRIVATE_POOL_CREATED`'s `POOL_INSIDERS` visibility flips to
  fully visible once `_pool_executed(pool)` is true). **Also**: a still
  -private eligible Pool is forced public the instant Arbitration is called
  on its base proposal (§8) — jurors need to know what's actually on the
  table before voting.

---

## 6. Market-direction locking & crossing invalidation

**A swap's "rising" direction is locked once, at authoring time** —
`SwapIntent.rising_entity_id`, computed via `_rising_entity` at the moment a
bare proposal, pool leg, or unilateral Force Swap Boost is created — and
**never recomputed** while it's pending. The frontend must pin its own
Visualize UI to this locked value, never to a live position comparison.

**`_execute_swap`** is the *sole* choke point where two entities' positions
ever change (call sites: `ACCEPT_PROPOSAL`, both legs of `ACCEPT_POOL`, a
Force Swap Boost (§7), and the Arbitration machine draw's own base/pool
outcome execution (§8)). After swapping, it emits `SWAP_EXECUTED` and calls
`_invalidate_crossed_negotiations`, which scans every *other* still-`OPEN`
proposal/pool whose own two entities intersect the just-moved pair. If a
scanned negotiation's live-recomputed `_rising_entity` no longer matches
its own locked `rising_entity_id`, it's voided **loudly, never masked**:
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

---

## 7. Boosts — Concentrate, Draw/Refresh, Force Swap

Every player starts with `starting_boosts` (default 2) Boosts — a public
roster fact (`GamePlayer.boosts_remaining`). A Boost is a unilateral,
non-negotiated action: no counterparty, no Accept/Decline from anyone else.
Legal only during `NEGOTIATION`, only while `boosts_remaining >= 1`, only
while `Game.boosts_expired` is still false (§4), and only while there is no
active Arbitration on the table's one active negotiation (§8) — all three
gates are checked by `USE_BOOST`'s shared legality check. All three types
submit through one command, `USE_BOOST`, discriminated by a `boost_type`
field (matches `CREATE_POOL`'s own `visibility`-field envelope style).

### Concentrate
Payload `{boost_type: "concentrate", holding_id_to_discard,
entity_id_to_duplicate}`. Discards one owned holding, duplicates an entity
the player already owns at least one copy of — legal even if the discarded
holding and the duplicated entity are the same (nets to no change in copy
count, not a cap violation just because it names the same entity twice).
The resulting copy count of the duplicated entity may never exceed
`concentrate_max_copies` (default 3).

### Draw / Refresh
Payload `{boost_type: "draw"}` to start; two dedicated follow-up commands
resolve it. **Eligibility — every current market entity the player owns
zero copies of — is computed once**, at the moment `USE_BOOST(draw)` is
submitted, and never recomputed for the rest of the decision (locked,
exactly like a proposal's `rising_entity_id`): discarding a holding during
the decision can never retroactively make some other entity eligible as
"the draw." One entity is drawn uniformly at random from that eligible set
and revealed privately to the player, who then either:
- **`RESOLVE_BOOST_DRAW`** (payload `{pending_boost_draw_id,
  holding_id_to_discard}`, `holding_id_to_discard` must be one of the
  original five): the discarded holding → `DISCARDED`, the drawn entity
  becomes a new `PORTFOLIO` holding.
- **`DECLINE_BOOST_DRAW`** ("Skip", payload `{pending_boost_draw_id}`):
  keeps the original five untouched.
- **Timeout**: `apply_due_time_transitions` forces the same outcome as Skip
  once `boost_draw_decision_seconds` (default 20.0s) elapses.

**The Boost is spent the instant `USE_BOOST(draw)` succeeds, regardless of
the eventual outcome** — declining or timing out never refunds it; only
whether the drawn entity actually lands in the portfolio differs between
the three resolutions.

**Frozen-view guarantee**, reused from the retired Reserve/Pickup mechanic
under a new name: the acting player's entire `project()` response is
pinned to a `cached_view` snapshot rendered once, at draw time, until the
decision resolves. Other players' own views are completely unaffected. A
pending Draw/Refresh decision also blocks that player specifically from
every other command (`PROPOSE_SWAP`, `PASS_PROPOSAL`, `CREATE_POOL`, ...)
until it resolves — self-scoped, it never blocks anyone else.

`RESOLVE_BOOST_DRAW` and `DECLINE_BOOST_DRAW` are the **only** two commands
exempt from the optimistic-concurrency `expected_version` check
(`_VERSION_EXEMPT_COMMANDS`) — a frozen client genuinely cannot know the
live game version while the rest of the game keeps moving around it.

### Force Swap
Payload `{boost_type: "force_swap", entity_a, entity_b}`. Alters the
market unilaterally — the named entities need not be owned by the actor at
all. No decision window; resolves synchronously through the same
`_execute_swap` choke point as any negotiated swap (§6), so it can void
other open negotiations exactly like an accepted deal would.

**Reversal lock.** Force Swap sets a single, table-wide `protected_pair`
(public and unconditional in `project()`) naming the two entities it just
swapped. While that lock is in place, `PROPOSE_SWAP` or `CREATE_POOL`
naming exactly that pair (either order) is illegal — `IllegalCommandError`,
"this pair was just Force Swapped and is locked against a direct
reverse". This is a durability fix for a real underpowered-Boost problem:
a Force Swap costs a whole Boost, but the exact reverse could otherwise be
undone for the price of a mere Move. The lock is narrow by design:
- It blocks only a **direct** reverse of the exact protected pair — a Move
  involving either entity against a third one is unaffected.
- Another Force Swap is **never** blocked by it, including one that
  targets the same pair — a Boost undoing a Boost is fair, same-cost play.
  That new Force Swap simply overwrites `protected_pair` with its own
  result (see `_use_boost_force_swap`).
- The lock clears the instant either protected entity actually moves again
  through an *executed* negotiated swap (`ACCEPT_PROPOSAL` or
  `ACCEPT_POOL`, inside `_execute_swap`) — there is no wall-clock timer.
- A single global slot, not per-player or a list: the next Force Swap
  anywhere unconditionally overwrites it, regardless of which entities it
  targets.

---

## 8. Arbitration

Once a negotiation's active participant set (§5) narrows to exactly one
remaining responder — the opener plus that one player, "the final two" —
**and that responder has an open Pool on the base proposal**, either of
them may `CALL_ARBITRATION` (payload `{}`). No Pool means there is no
competing offer to arbitrate, so a bare proposal can never enter
Arbitration (including immediately in a two-player game). Irreversible:
there is no un-call, and once called, nothing may add a new Pool, remove the
eligible one, or narrow the participant set further until it resolves.
Settling normally (`ACCEPT_PROPOSAL` or `ACCEPT_POOL`) remains fully legal
throughout the window — the intended way out if the pair actually agrees
before time runs out.

Calling Arbitration:
- Forces the one eligible Pool (§5 — the remaining responder's own, if they
  have one) public if it was still private — jurors need to know what's
  actually on the table. Emits `ARBITRATION_POOL_REVEALED` (`PUBLIC`) only
  when this transition actually happens.
- Starts an irreversible `arbitration_window_seconds` (default 20.0s)
  last-chance window. Emits `ARBITRATION_CALLED` (`PUBLIC`) — the final two
  already know who called it; this is the public starting gun.
- Locks in caller-role-dependent starting weights
  (`GameConfig.arbitration_base_weights`, default `{originator: {base: 30,
  pool: 40, neither: 40}, other: {base: 40, pool: 30, neither: 40}}`) —
  deliberately weights, not percentages, they don't need to sum to 100.
  All three candidates (`base`, `pool`, `neither`) are always present,
  because the open-Pool precondition guarantees a concrete Pool outcome.
- No Boosts are legal for anyone for the duration of the window (§7).

**The secret jury**: every player who has already passed this negotiation
becomes its jury the instant Arbitration is called. Each may
`CAST_ARBITRATION_VOTE` (payload `{vote: "base" | "pool" | "neither"}`,
`"pool"` only offered if eligible) exactly once. A vote is additive and
cumulative, independent of every other juror's: `+arbitration_vote_bonus`
(default 10) to the voted choice, `-arbitration_vote_penalty` (default 5)
to each of the other legal choices, floored at zero as it accumulates.
Never shown to any live audience, including the two active participants
themselves — only "a juror has voted" (never which choice) is ever
projected live (`Proposal.pending_arbitration.voted_player_ids`).
Normalization happens exactly once, at the actual draw, from these
accumulated weights — the jury can lean on the machine, never become it.

**Resolution**, in priority order:
1. **Settled normally** — an `ACCEPT_PROPOSAL` or `ACCEPT_POOL` lands
   during the window. `ArbitrationResolutionReason.SETTLED_NORMALLY`; no
   draw ever runs.
2. **Preempted** — Ready-to-Close (or Move exhaustion) closes the market
   before the window elapses. `ArbitrationResolutionReason.MARKET_CLOSED`;
   no draw ever runs.
3. **The window elapses with neither of the above** —
   `apply_due_time_transitions` runs a single weighted random draw
   (`engine._resolve_arbitration_via_machine`) over the final, jury
   -adjusted weights. Exactly three possible outcomes, no fourth "chaos"
   option:
   - `MACHINE_BASE`: the base proposal executes, exactly as an
     `ACCEPT_PROPOSAL` would.
   - `MACHINE_POOL`: both the base proposal and the eligible Pool execute,
     exactly as an `ACCEPT_POOL` would.
   - `MACHINE_NEITHER`: the negotiation resolves `ARBITRATION_NEITHER` —
     no deal, no market change. The Move the opener spent to open it is
     still never refunded.

`ARBITRATION_RESOLVED` is `PUBLIC` at the policy level (the fact of how it
ended, and any resulting `SWAP_EXECUTED`, are already publicly observable
through other events regardless), but `base_weights`/`final_weights`/
`votes` are stripped from its payload for every live audience — including
the two active participants. Unlocked in full only for `ReplayAudience`.

**Telemetry**: nothing new is needed to answer "how long did the table sit
at two active participants before someone called it" — fully
reconstructable postgame by replaying `PROPOSAL_PASSED` events in order
against the seated roster (or, in a 2-player game, it's simply the base
proposal's own `created_at`, since the active set is 2 from the instant it
opens).

---

## 9. Haircut risk & final scoring

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
  index) — a randomly generated profile's own effective depth can land
  earlier (see below), and reading it here would leak the profile's shape
  before reveal.
- **Reveal**: hidden until the live Move-driven reveal trigger fires (§4)
  — `HAIRCUT_RISK_REVEALED` fires the instant cumulative Moves consumed
  across the table first reaches or crosses 50% of the table's total
  initial Move allocation. A game that closes before crossing that
  threshold never sees the live event, but `project()` unconditionally
  reveals the profile once `phase == SCORED` regardless.
- **Countdown**: `haircut_reveal_in_moves` in `project()` — how many more
  Moves the table must burn before that trigger fires (`(total_initial +
  1) // 2 - consumed`, floored at 0). The Move-driven stand-in for the
  retired gameplay clock's visible timer; `null` once revealed, once
  scored, or in `LOBBY`. Not a leak — per-player `moves_remaining` is
  already public and `HAIRCUT_RISK_REVEALED` is a public event, so this
  is only the same arithmetic centralized server-side.
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

## 10. Ready-to-close & market close

`SET_READY_TO_CLOSE` (payload `{ready: bool}`) toggles a player's own
readiness — **self-only for the entire live game, no aggregate anywhere**.
No other player ever learns a count, a percentage, or even that someone
toggled it; the only thing anyone else ever learns is the sudden fact of
closure itself, once `close_threshold` (`close_threshold(n)`: `n` itself
for `n <= 3`, `ceil(0.75n)` above that) is actually reached.
`READY_TO_CLOSE_CHANGED` is `ACTOR_ONLY`. Toggling is bidirectional (on
then off is legal) right up until the threshold triggers closure in the
same transaction; a no-op toggle (already at that value) doesn't even bump
the game version.

**One post-close exception**: once the market has closed *because* the
ready threshold was reached (`close_reason == READY_THRESHOLD`),
`project()` exposes every player's final `ready_to_close` to all live
audiences — the results leaderboard marks the voters "voted to close".
Nothing is revealed before close, and no other `CloseReason` exposes it.
Replay exposure is still governed solely by
`GameConfig.ready_to_close_revealed_in_replay`. See
`_project_player` and `test_ready_to_close_revealed_to_all_after_ready_threshold_close`.

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

One bespoke, non-table-driven redaction layered on top of the policy
lookup (documented inline in `project_events`):
- `ARBITRATION_RESOLVED`'s `base_weights`/`final_weights`/`votes` keys are
  stripped for every live audience, including the two active participants
  — never shown live to anyone, only `ReplayAudience` (§8).

**Holdings redaction**: the cadence/economy redesign removed every
unrevealed-to-owner zone the old Reserve/Pickup mechanic needed —
`HoldingZone` now has exactly two members, `PORTFOLIO` and `DISCARDED`,
and a player's own holdings list is always fully revealed to them, live,
in every zone. (The one remaining live-play redaction pattern — a frozen
`cached_view` while a decision is pending — is Draw/Refresh's own, see §7,
not a holdings-zone redaction at all.) Once `phase == SCORED`, every
holding, for every player, is fully revealed to everyone — postgame is
deliberately maximally transparent, there's just less left to reveal than
there used to be.

**Pending Draw/Refresh's frozen view** (§7) and **Arbitration's own
pending state** (`pending_arbitration`'s public fields vs. its always
-hidden weights/votes, §8) are both enforced inside `project()` itself,
not by the frontend choosing to render less.

---

## 12. Command reference (payload + core legality)

Every command below goes through the generic `POST /games/{id}/commands`
envelope (`{command_id, type, expected_version, payload}`) except
`create_game`/`join_game`, which have their own dedicated routes since the
actor isn't seated yet. `expected_version` is checked against
optimistic-concurrency (`StaleVersionError` → HTTP 409) for every command
**except** `RESOLVE_BOOST_DRAW`/`DECLINE_BOOST_DRAW` (§7).

| Command | Payload | Notes |
|---|---|---|
| `CANCEL_GAME` | `{}` | Host-only, `LOBBY`-only. |
| `EXTEND_LOBBY_TIMER` | `{}` | Host-only, `LOBBY`-only. |
| `START_GAME` | `{}` | Host-only, `LOBBY`-only, 2–6 players. See §2. |
| `PROPOSE_SWAP` | `{entity_a, entity_b}` | Move-gated, single-active-negotiation-gated. May not directly reverse a Force-Swap-protected pair. See §4–5, §7 (reversal lock). |
| `PASS_PROPOSAL` | `{proposal_id}` | Non-proposer, non-repeat, no own open pool on it, no active Arbitration. See §5. |
| `ACCEPT_PROPOSAL` | `{proposal_id}` | Not your own, not passed. Legal during Arbitration. |
| `CREATE_POOL` | `{proposal_id, entity_c, entity_d, visibility}` | See §5. May not directly reverse a Force-Swap-protected pair — see §7. |
| `WITHDRAW_POOL` | `{pool_id}` | Pool initiator only. Illegal during Arbitration. |
| `MAKE_POOL_PUBLIC` | `{pool_id}` | Pool initiator only, private→public. Illegal during Arbitration. |
| `PASS_POOL` | `{pool_id}` | Eligible accepter, non-repeat, non-initiator. Private resolves on the base proposer's Pass; public resolves once all eligible accepters Pass. Illegal during Arbitration. |
| `DECLINE_POOL` | `{pool_id}` | Compatibility alias for `PASS_POOL`. |
| `ACCEPT_POOL` | `{pool_id}` | See §5. Legal during Arbitration. |
| `CALL_ARBITRATION` | `{}` | Either of the final two active participants, while the remaining responder has an open Pool. See §8. |
| `CAST_ARBITRATION_VOTE` | `{vote}` | Jury-only (already-passed players). See §8. |
| `USE_BOOST` | `{boost_type, ...}` | `boost_type`: `concentrate` \| `draw` \| `force_swap`. See §7. |
| `RESOLVE_BOOST_DRAW` | `{pending_boost_draw_id, holding_id_to_discard}` | **Version-exempt.** See §7. |
| `DECLINE_BOOST_DRAW` | `{pending_boost_draw_id}` | **Version-exempt.** See §7. |
| `SET_READY_TO_CLOSE` | `{ready}` | See §10. |

There is no gameplay clock. Time-driven transitions are re-checked ahead
of *every* command via `apply_due_time_transitions`
(`engine.handle_command`'s first step), and cheaply mirrored
(side-effect-free) by `is_time_transition_due` so `GET /games/{id}` can
decide whether it's worth acquiring the write lock at all: lobby
reminder/grace auto-cancel, the `NEGOTIATION`-phase abandonment backstop
(§2), Arbitration's own 20-second window elapsing, and each player's own
Draw/Refresh decision window elapsing. Every other
trigger (Boost expiry, the Haircut reveal, the Move-exhaustion endgame) is
a direct, synchronous consequence of a command, not something that needs
polling to notice.

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
- **Only an `executed` resolution may claim a `SWAP_EXECUTED`
  occurrence.** `claimSwap` is called from exactly two places:
  `PROPOSAL_RESOLVED && reason === "executed"` and `POOL_RESOLVED &&
  reason === "executed"`. A `voided_market_swung`, `expired_all_passed`,
  `declined_by_target`, `preempted_by_other_action`,
  `base_proposal_voided`, `withdrawn_by_initiator`, `arbitration_neither`,
  or `market_closed` resolution **never** reaches `claimSwap` — those
  branches all require the exact literal reason string above, nothing else
  matches. This holds structurally on the backend side too: a voided
  negotiation's own entity pair never produces a `SWAP_EXECUTED` in the
  first place (voiding never calls `_execute_swap` — only the *unrelated,
  already-executing* swap that caused the crossing does), so there is
  nothing for a non-executed resolution to wrongly claim even in principle.
- A `SWAP_EXECUTED` with no matching claim by the end of the event walk is
  structurally guaranteed to be a unilateral Force Swap Boost — tracked via
  a per-pair LIFO queue (not a flat set), since the same pair can
  legitimately swap more than once in a game.
- A player's marker (ordinary or unilateral) on an entity is **cleared
  entirely, not decremented**, the moment that same player helps that
  entity fall — regardless of which mechanism created the marker.
- `unilateralCount` (Force Swap) is tracked separately from ordinary
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
those always come from the server (`project()`'s fields).
