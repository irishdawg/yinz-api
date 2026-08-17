"""PlayerNameRepository — the global pool of curated display-name
placeholders (player_name_seeds). Every create_game/join_game display_name
that matches a catalog entry gets validated against this before the domain
engine ever sees it (routes.py); anything else is accepted as a typed name
instead (see routes.py's _resolve_submitted_name and CURRENT_WORK.md for
that split).

Display name case is standardized to upper everywhere -- offer_name/
roll_golden_name both return the catalog's own name uppercased (a typed
name is separately uppercased in routes.py), so every method here that
matches a name *against* the catalog (is_valid_name, mark_name_used, and
exclude handling in offer_name/roll_golden_name) does so
case-insensitively -- the catalog itself stays stored in its original
curated case (player_name_seeds/theme_data-style fixtures), only what
this module hands back and matches against gets folded to upper.

Two distinct "already taken" concerns, both handled via `exclude`, not two
separate mechanisms: the name currently shown on screen (reroll only, so
"change name" always gives something different) and every display name
already held by a seated player in the game being joined (both offer and
reroll, resolved by routes.py from `join_code` before calling in here) —
this repository only tracks the global pool and usage counts, it has no
notion of "this game's roster" itself.

Golden names: rare (1/500), rolled only by `roll_golden_name` -- called by
routes.py exactly once per *actual seat* (create_game's host, join_game's
joiner), never by the free-preview `offer_name`/reroll endpoints. This is
the whole point: `offer_name` structurally cannot draw gold no matter how
many times it's called, so farming it means actually filling a real seat
in a real game (capped at 6, and creating a fresh game to get more seats
is already rate-limited at the route level) rather than just reloading a
page. The draw logic lives here, not in SQL or routes.py, so it's one
place, testable with an injectable RNG.
"""

from __future__ import annotations

import random as _random_module
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

_GOLDEN_ODDS = 1 / 500
# Draws are weighted toward under-used names rather than pure uniform
# random, so ordinary names stay roughly evenly exposed over time instead
# of one getting picked 73 times while another gets picked twice purely by
# chance -- narrow to the K least-used eligible names, then pick uniformly
# at random among just those.
_LEAST_USED_POOL_SIZE = 10


class RandomLike(Protocol):
    def random(self) -> float: ...


class NoAvailableNameError(Exception):
    """Every eligible name is excluded -- an exhausted pool (too few seeds
    for the number of players already in the game) or, in principle, a
    genuinely empty player_name_seeds table."""


class PlayerNameRepository(Protocol):
    async def is_valid_name(self, name: str) -> bool: ...
    async def mark_name_used(self, name: str) -> None: ...
    async def offer_name(self, *, exclude: set[str] = ...) -> str: ...
    async def roll_golden_name(self, *, exclude: set[str] = ...) -> str | None: ...


class InMemoryPlayerNameRepository:
    """Permissive by default (`names=None` accepts any name for validation,
    and has nothing to offer) so the existing tests using arbitrary display
    names ("Tedy", "Mortia", ...) don't need to know about the seed pool at
    all. Pass explicit `names`/`golden_names` for tests that actually
    exercise offering. `rng` is constructor-level (not per-call) so a
    forced RNG can be wired all the way through `make_client()` and
    exercised via real HTTP requests to create_game/join_game, not just
    called directly. Deliberately doesn't model `is_active` or the
    least-used-K weighting the real Postgres implementation does -- picks
    deterministically (sorted-first) for simple, predictable test
    assertions; nothing here currently needs to assert on distribution
    fairness specifically."""

    def __init__(self, names: set[str] | None = None, golden_names: set[str] | None = None, rng: RandomLike = _random_module) -> None:
        self._names = names
        self._golden_names = golden_names or set()
        self._rng = rng
        self.usage: dict[str, int] = {}

    async def is_valid_name(self, name: str) -> bool:
        return self._names is None or name.upper() in {n.upper() for n in self._names}

    async def mark_name_used(self, name: str) -> None:
        # Case-insensitive key -- name arrives uppercased (this
        # repository's own offer_name/roll_golden_name always return
        # upper), matching against whatever case self._names/golden_names
        # actually hold.
        canonical = next((n for n in (self._names or set()) | self._golden_names if n.upper() == name.upper()), name)
        self.usage[canonical] = self.usage.get(canonical, 0) + 1

    async def offer_name(self, *, exclude: set[str] = frozenset()) -> str:
        exclude_upper = {e.upper() for e in exclude}
        pool = [n for n in (self._names or set()) - self._golden_names if n.upper() not in exclude_upper]
        if not pool:
            raise NoAvailableNameError("no eligible names left to offer")
        return sorted(pool)[0].upper()

    async def roll_golden_name(self, *, exclude: set[str] = frozenset()) -> str | None:
        if self._rng.random() >= _GOLDEN_ODDS:
            return None
        exclude_upper = {e.upper() for e in exclude}
        golden_pool = [n for n in self._golden_names if n.upper() not in exclude_upper]
        if not golden_pool:
            return None
        return sorted(golden_pool)[0].upper()


class PostgresPlayerNameRepository:
    def __init__(self, pool: AsyncConnectionPool, rng: RandomLike = _random_module) -> None:
        self._pool = pool
        self._rng = rng

    async def is_valid_name(self, name: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select 1 from player_name_seeds where upper(name) = upper(%s)", (name,))
                return await cur.fetchone() is not None

    async def mark_name_used(self, name: str) -> None:
        # Best-effort, deliberately not part of PostgresGameRepository's
        # ambient transaction -- usage_count is an analytics tally, not
        # gameplay-critical state, so it doesn't need lock_for()'s
        # atomicity machinery. A crash between the two committing
        # independently could in principle leave this count slightly off;
        # it never leaves a game/player row orphaned or missing.
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("update player_name_seeds set usage_count = usage_count + 1 where upper(name) = upper(%s)", (name,))

    async def offer_name(self, *, exclude: set[str] = frozenset()) -> str:
        name = await self._draw(is_golden=False, exclude=exclude)
        if name is None:
            raise NoAvailableNameError("no eligible names left to offer")
        return name.upper()

    async def roll_golden_name(self, *, exclude: set[str] = frozenset()) -> str | None:
        if self._rng.random() >= _GOLDEN_ODDS:
            return None
        name = await self._draw(is_golden=True, exclude=exclude)
        return name.upper() if name is not None else None

    async def _draw(self, *, is_golden: bool, exclude: set[str]) -> str | None:
        # is_active = true -- deliberately not checked in is_valid_name(),
        # only here: this is what actually controls what gets offered
        # going forward, without invalidating a name someone already has
        # displayed or already submitted.
        #
        # Two-step, one query: narrow to the _LEAST_USED_POOL_SIZE eligible
        # names ordered by usage_count (random tiebreak among equal
        # counts), then pick uniformly at random from just that narrowed
        # set -- biases toward under-used names without making the pick
        # deterministic. upper(name) = any(%s) against an already
        # -uppercased exclude list -- exclude is built from live
        # display_names (already-standardized upper) mixed with a reroll's
        # own already-offered (also upper) name, so the catalog's own
        # original-case names need the same fold to actually match.
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    with candidates as (
                        select name from player_name_seeds
                        where is_golden = %s and is_active = true and not (upper(name) = any(%s))
                        order by usage_count asc, random()
                        limit %s
                    )
                    select name from candidates order by random() limit 1
                    """,
                    (is_golden, [e.upper() for e in exclude], _LEAST_USED_POOL_SIZE),
                )
                row = await cur.fetchone()
                return row[0] if row else None
