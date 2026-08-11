"""PlayerNameRepository — the global pool of curated display-name
placeholders (player_name_seeds). No open text entry anywhere in the
product; every create_game/join_game display_name gets validated against
this before the domain engine ever sees it (routes.py), so there's no
content-moderation surface to build at all. "Already taken by another
seated player in this game" is checked in-process against that game's own
players (already loaded, part of the Game aggregate) — this table only
tracks the global pool and how often each name has been used overall.
"""

from __future__ import annotations

from typing import Protocol

from psycopg_pool import AsyncConnectionPool


class PlayerNameRepository(Protocol):
    async def is_valid_name(self, name: str) -> bool: ...
    async def mark_name_used(self, name: str) -> None: ...


class InMemoryPlayerNameRepository:
    """Permissive by default (accepts any name) so the existing tests using
    arbitrary display names ("Tedy", "Mortia", ...) don't need to know
    about the seed pool at all. Pass an explicit `allowed` set to actually
    exercise validation."""

    def __init__(self, allowed: set[str] | None = None) -> None:
        self._allowed = allowed
        self.usage: dict[str, int] = {}

    async def is_valid_name(self, name: str) -> bool:
        return self._allowed is None or name in self._allowed

    async def mark_name_used(self, name: str) -> None:
        self.usage[name] = self.usage.get(name, 0) + 1


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
