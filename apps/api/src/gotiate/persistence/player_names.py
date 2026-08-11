"""PlayerNameRepository — the global pool of curated display-name
placeholders (player_name_seeds). No open text entry anywhere in the
product; every create_game/join_game display_name gets validated against
this before the domain engine ever sees it (routes.py), so there's no
content-moderation surface to build at all.

Two distinct "already taken" concerns, both handled via `exclude`, not two
separate mechanisms: the name currently shown on screen (reroll only, so
"change name" always gives something different) and every display name
already held by a seated player in the game being joined (both initial
offer and reroll, resolved by routes.py from `join_code` before calling
in here) — this repository only tracks the global pool and usage counts,
it has no notion of "this game's roster" itself.

Golden names: rare (1/500), drawn only on the very first name offered,
never via reroll. The draw logic lives here, not in SQL or routes.py, so
it's one place, testable with an injectable RNG.
"""

from __future__ import annotations

import random as _random_module
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

_GOLDEN_ODDS = 1 / 500


class RandomLike(Protocol):
    def random(self) -> float: ...


class NoAvailableNameError(Exception):
    """Every eligible name is excluded -- an exhausted pool (too few seeds
    for the number of players already in the game) or, in principle, a
    genuinely empty player_name_seeds table."""


class PlayerNameRepository(Protocol):
    async def is_valid_name(self, name: str) -> bool: ...
    async def mark_name_used(self, name: str) -> None: ...
    async def offer_initial_name(self, *, exclude: set[str] = ..., rng: RandomLike = ...) -> tuple[str, bool]: ...
    async def offer_reroll_name(self, *, exclude: set[str]) -> str: ...


class InMemoryPlayerNameRepository:
    """Permissive by default (`names=None` accepts any name for validation,
    and has nothing to offer) so the existing tests using arbitrary display
    names ("Tedy", "Mortia", ...) don't need to know about the seed pool at
    all. Pass explicit `names`/`golden_names` for tests that actually
    exercise offering."""

    def __init__(self, names: set[str] | None = None, golden_names: set[str] | None = None) -> None:
        self._names = names
        self._golden_names = golden_names or set()
        self.usage: dict[str, int] = {}

    async def is_valid_name(self, name: str) -> bool:
        return self._names is None or name in self._names

    async def mark_name_used(self, name: str) -> None:
        self.usage[name] = self.usage.get(name, 0) + 1

    async def offer_initial_name(self, *, exclude: set[str] = frozenset(), rng: RandomLike = _random_module) -> tuple[str, bool]:
        if rng.random() < _GOLDEN_ODDS:
            golden_pool = self._golden_names - exclude
            if golden_pool:
                return sorted(golden_pool)[0], True
        return await self.offer_reroll_name(exclude=exclude), False

    async def offer_reroll_name(self, *, exclude: set[str]) -> str:
        pool = (self._names or set()) - self._golden_names - exclude
        if not pool:
            raise NoAvailableNameError("no eligible names left to offer")
        return sorted(pool)[0]


class PostgresPlayerNameRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def is_valid_name(self, name: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select 1 from player_name_seeds where name = %s", (name,))
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
                await cur.execute("update player_name_seeds set usage_count = usage_count + 1 where name = %s", (name,))

    async def offer_initial_name(self, *, exclude: set[str] = frozenset(), rng: RandomLike = _random_module) -> tuple[str, bool]:
        if rng.random() < _GOLDEN_ODDS:
            golden = await self._draw(is_golden=True, exclude=exclude)
            if golden is not None:
                return golden, True
        name = await self._draw(is_golden=False, exclude=exclude)
        if name is None:
            raise NoAvailableNameError("no eligible names left to offer")
        return name, False

    async def offer_reroll_name(self, *, exclude: set[str]) -> str:
        name = await self._draw(is_golden=False, exclude=exclude)
        if name is None:
            raise NoAvailableNameError("no eligible names left to offer")
        return name

    async def _draw(self, *, is_golden: bool, exclude: set[str]) -> str | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                if exclude:
                    await cur.execute(
                        "select name from player_name_seeds where is_golden = %s and not (name = any(%s)) order by random() limit 1",
                        (is_golden, list(exclude)),
                    )
                else:
                    await cur.execute(
                        "select name from player_name_seeds where is_golden = %s order by random() limit 1",
                        (is_golden,),
                    )
                row = await cur.fetchone()
                return row[0] if row else None
