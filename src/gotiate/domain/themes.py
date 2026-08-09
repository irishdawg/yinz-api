"""Theme content — domain model §02, ThemeSet/ThemeEntityDefinition. Content,
not game rules: which named things populate the market this game, not how the
market behaves. Deliberately separate from entities.py/engine.py for that
reason.

`ThemeRepository` is the seam. `JsonFileThemeRepository` reads flat JSON files
out of `theme_data/` for now; a Supabase-backed implementation satisfies the
same Protocol later without engine.py or anything else changing — same
pattern as `persistence.repository.GameRepository`.

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
    machinery. Swap the repository, not this class, when Supabase exists."""

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


_DEFAULT_DIRECTORY = Path(__file__).parent / "theme_data"
_repository: ThemeRepository = JsonFileThemeRepository(_DEFAULT_DIRECTORY)


def get_theme_set(theme_set_id: str) -> ThemeSet:
    return _repository.get(theme_set_id)


def get_theme_repository() -> ThemeRepository:
    return _repository


def set_theme_repository(repo: ThemeRepository) -> None:
    """Swap the backing repository — a Supabase-backed one in production
    later, or a fake one in tests. Mirrors persistence.repository's
    swappability; there's deliberately no dependency-injection ceremony here
    since theme content isn't per-request authoritative state."""
    global _repository
    _repository = repo
