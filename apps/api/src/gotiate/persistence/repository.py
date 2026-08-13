"""GameRepository — the interface the engine and API code depend on. Currently
an in-memory implementation; a Supabase/Postgres implementation satisfies the
same Protocol later without engine or API code changing (domain model §08,
"Supabase stores and distributes state. FastAPI decides state.")."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from gotiate.domain.entities import CommandReceipt, Game, GamePhase
from gotiate.domain.events import GameEvent


class GameRepository(Protocol):
    async def create(self, game: Game) -> None: ...
    async def get(self, game_id: str) -> Game | None: ...
    async def get_by_join_code(self, join_code: str) -> Game | None: ...
    async def save(self, game: Game) -> None: ...
    async def append_events(self, events: list[GameEvent]) -> None: ...
    async def get_receipt(self, game_id: str, command_id: str) -> CommandReceipt | None: ...
    async def record_receipt(self, receipt: CommandReceipt) -> None: ...
    async def find_active_game_hosted_by(self, auth_user_id: str) -> Game | None: ...
    async def find_active_game_seated_in(self, auth_user_id: str) -> Game | None: ...
    async def get_events(self, game_id: str, since_seq: int = 0) -> list[GameEvent]: ...

    def lock_for(self, game_id: str) -> AbstractAsyncContextManager[None]:
        # asyncio.Lock (InMemoryGameRepository) and a real `SELECT ... FOR
        # UPDATE`-backed transaction (PostgresGameRepository) are both valid
        # async context managers satisfying "one writer per game" (§08) --
        # deliberately not typed as `asyncio.Lock` specifically, since that
        # was always just the in-memory stand-in, not the actual contract.
        ...


class InMemoryGameRepository:
    def __init__(self) -> None:
        self._games: dict[str, Game] = {}
        self._by_code: dict[str, str] = {}
        self._events: list[GameEvent] = []
        # Keyed by (game_id, command_id) — command_id is only a client-side
        # idempotency key, unique per request, never globally unique on its
        # own. Keying by command_id alone would let a stale/reused id from
        # one game short-circuit straight to a cached result in another,
        # skipping authorization entirely.
        self._receipts: dict[tuple[str, str], CommandReceipt] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def create(self, game: Game) -> None:
        self._games[game.id] = game
        self._by_code[game.join_code] = game.id

    async def get(self, game_id: str) -> Game | None:
        return self._games.get(game_id)

    async def get_by_join_code(self, join_code: str) -> Game | None:
        game_id = self._by_code.get(join_code)
        return self._games.get(game_id) if game_id else None

    async def save(self, game: Game) -> None:
        self._games[game.id] = game

    async def append_events(self, events: list[GameEvent]) -> None:
        self._events.extend(events)

    async def get_receipt(self, game_id: str, command_id: str) -> CommandReceipt | None:
        return self._receipts.get((game_id, command_id))

    async def record_receipt(self, receipt: CommandReceipt) -> None:
        self._receipts[(receipt.game_id, receipt.command_id)] = receipt

    async def find_active_game_hosted_by(self, auth_user_id: str) -> Game | None:
        # One in-flight hosted game per user — a real constraint, not just a
        # time-windowed rate limit, so someone can't spin up unlimited games
        # by simply spacing requests out. Full table scan is fine at V1
        # scale; a real Postgres implementation would index on
        # (host_player_id) joined through to auth_user_id, or denormalize
        # host_auth_user_id onto the games row directly.
        for game in self._games.values():
            if game.phase in (GamePhase.SCORED, GamePhase.CANCELLED) or game.host_player_id is None:
                continue
            host = game.player_by_id(game.host_player_id)
            if host.auth_user_id == auth_user_id:
                return game
        return None

    async def find_active_game_seated_in(self, auth_user_id: str) -> Game | None:
        # Same shape as find_active_game_hosted_by, just without the
        # host-only restriction -- backs the mobile sleep/re-entry fix (any
        # seated player, not just the host, should be routed straight back
        # into an active game). Full table scan, same acceptable-at-V1-scale
        # reasoning as above.
        for game in self._games.values():
            if game.phase in (GamePhase.SCORED, GamePhase.CANCELLED):
                continue
            if game.player_by_auth_id(auth_user_id) is not None:
                return game
        return None

    async def get_events(self, game_id: str, since_seq: int = 0) -> list[GameEvent]:
        events = [e for e in self._events if e.game_id == game_id and e.seq_no >= since_seq]
        return sorted(events, key=lambda e: e.seq_no)

    def lock_for(self, game_id: str) -> asyncio.Lock:
        # Stands in for `SELECT ... FOR UPDATE` on the games row (§08,
        # "one writer per game"). One asyncio.Lock per game_id, created
        # lazily, held for the duration of a single command.
        lock = self._locks.get(game_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[game_id] = lock
        return lock
