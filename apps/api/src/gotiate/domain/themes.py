"""Theme content — domain model §02, ThemeSet/ThemeEntityDefinition. Content,
not game rules: which named things populate the market this game, not how the
market behaves. Deliberately separate from entities.py/engine.py for that
reason.

`ThemeRepository` is the seam, and it's synchronous by construction: `get()`/
`list_ids()` are called directly from deep inside the pure, zero-I/O domain
layer (engine.py/projections.py — see AGENTS.md), which is not async and
structurally can't await a real query. `JsonFileThemeRepository` reads flat
JSON files out of `theme_data/` — the offline test suite's fixture (tests
must never touch real Postgres, see AGENTS.md), and the in-memory fallback
if `GOTIATE_BACKEND_DATABASE_URL` isn't set. `PostgresThemeRepository` is
what the real deployed app actually serves from: it loads the whole catalog
into an in-memory cache once via `refresh()` (an explicit async call, made
once at FastAPI startup — see main.py's lifespan), then satisfies the same
synchronous Protocol as JsonFileThemeRepository by reading that cache. A
content edit in Supabase takes effect on the next deploy/restart, not live —
see CURRENT_WORK.md.

A theme set's `entities` become the market's `theme_key`s directly (see
engine._handle_start_game). `entity_id` and `theme_key` coincide by
construction today — every theme_key is sampled at most once per market — but
the two remain conceptually distinct fields for the same reason they're
distinct in the domain model: an entity's identity *within this game* and
which content it displays as are different questions, even when today they
have the same answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from gotiate.domain.errors import NotFoundError


class ThemeEntityDefinition(BaseModel):
    theme_key: str  # unique within this ThemeSet only — never referenced across sets, so no global uniqueness requirement
    display_name: str
    ticker_symbol: str  # short, for compact display — a proposed swap can't fit two full names
    is_locked: bool = False  # always dealt into the market for this theme set, never part of the swappable pool
    logo_url: str | None = None  # a reference only — FastAPI never fetches/serves the image itself, that's Supabase Storage/CDN + frontend


class ThemeSet(BaseModel):
    theme_set_id: str
    name: str
    version: int
    entities: list[ThemeEntityDefinition]


class ThemeRepository(Protocol):
    def get(self, theme_set_id: str) -> ThemeSet: ...
    def list_ids(self) -> list[str]: ...


class JsonFileThemeRepository:
    """One JSON file per theme set: `{theme_set_id, name, version, entities}`.
    Loaded lazily, cached in memory — this is static reference content, not
    per-request I/O, so a plain synchronous cache is the right amount of
    machinery. The offline test suite's fixture, and the real app's
    fallback when no database is configured — see PostgresThemeRepository
    for what the real deployed app actually serves from."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._cache: dict[str, ThemeSet] = {}

    def get(self, theme_set_id: str) -> ThemeSet:
        cached = self._cache.get(theme_set_id)
        if cached is not None:
            return cached
        path = self._directory / f"{theme_set_id}.json"
        if not path.exists():
            raise NotFoundError(f"no theme set {theme_set_id!r} in {self._directory}")
        theme_set = ThemeSet.model_validate(json.loads(path.read_text(encoding="utf-8")))
        self._cache[theme_set_id] = theme_set
        return theme_set

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self._directory.glob("*.json"))


class PostgresThemeRepository:
    """Supabase (`theme_sets`/`theme_entities`) as the real source of truth
    for theme content, satisfying the same synchronous Protocol as
    JsonFileThemeRepository via an in-memory cache populated by an explicit
    async `refresh()` -- see this module's own docstring for why `get()`
    itself can't just query Postgres directly. `refresh()` is called once,
    at FastAPI startup (main.py's lifespan), after the same pool used for
    game/player-name persistence opens and health-checks; `get()` raises
    NotFoundError (not, say, a confusing empty-cache KeyError) if called
    before any refresh has ever succeeded.

    No per-game version pinning yet -- always resolves the latest version
    per theme_set_id, same as JsonFileThemeRepository (which has no concept
    of version at all). GameConfig.theme_set_version exists but nothing
    reads it yet; see CURRENT_WORK.md."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._cache: dict[str, ThemeSet] = {}

    async def refresh(self) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    select ts.theme_set_id, ts.version, ts.name,
                           te.theme_key, te.display_name, te.ticker_symbol, te.is_locked, te.logo_url
                    from theme_sets ts
                    join theme_entities te
                      on te.theme_set_id = ts.theme_set_id and te.theme_set_version = ts.version
                    where ts.version = (select max(version) from theme_sets ts2 where ts2.theme_set_id = ts.theme_set_id)
                    order by ts.theme_set_id, te.theme_key
                    """
                )
                rows = await cur.fetchall()

        fresh: dict[str, ThemeSet] = {}
        for theme_set_id, version, name, theme_key, display_name, ticker_symbol, is_locked, logo_url in rows:
            entity = ThemeEntityDefinition(
                theme_key=theme_key, display_name=display_name, ticker_symbol=ticker_symbol, is_locked=is_locked, logo_url=logo_url
            )
            if theme_set_id not in fresh:
                fresh[theme_set_id] = ThemeSet(theme_set_id=theme_set_id, name=name, version=version, entities=[])
            fresh[theme_set_id].entities.append(entity)

        if not fresh:
            # A misconfigured or unmigrated database is a startup-time bug,
            # not a runtime "no theme sets today" state -- fail loudly here
            # rather than let every subsequent get() raise NotFoundError
            # with no context, matching the pool's own health-check
            # philosophy in main.py's lifespan.
            raise RuntimeError("PostgresThemeRepository.refresh() found no theme content in theme_sets/theme_entities")
        self._cache = fresh

    def get(self, theme_set_id: str) -> ThemeSet:
        theme_set = self._cache.get(theme_set_id)
        if theme_set is None:
            raise NotFoundError(f"no theme set {theme_set_id!r} (or refresh() hasn't been called yet)")
        return theme_set

    def list_ids(self) -> list[str]:
        return sorted(self._cache.keys())


_DEFAULT_DIRECTORY = Path(__file__).parent / "theme_data"
_repository: ThemeRepository = JsonFileThemeRepository(_DEFAULT_DIRECTORY)


def get_theme_set(theme_set_id: str) -> ThemeSet:
    return _repository.get(theme_set_id)


def get_theme_repository() -> ThemeRepository:
    return _repository


def set_theme_repository(repo: ThemeRepository) -> None:
    """Swap the backing repository — main.py calls this once at startup with
    a freshly-`refresh()`'d PostgresThemeRepository for the real deployed
    app; the default stays JsonFileThemeRepository for everything else
    (tests, and the in-memory-fallback dev mode). Mirrors
    persistence.repository's swappability; there's deliberately no
    dependency-injection ceremony here since theme content isn't per-request
    authoritative state, and engine.py/projections.py call get_theme_set()
    directly (not via FastAPI Depends) since they're plain synchronous
    domain code with no request context at all."""
    global _repository
    _repository = repo
