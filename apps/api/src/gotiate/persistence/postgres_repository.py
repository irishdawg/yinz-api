"""Postgres/Supabase-backed GameRepository — satisfies the same Protocol as
InMemoryGameRepository (repository.py), reading/writing the schema in
supabase/migrations/ directly (SELECT ... FOR UPDATE and multi-statement
transactions aren't expressible through PostgREST, which is why FastAPI
holds a direct Postgres connection at all — see DATABASE.md).

Two things only bite once state moves out of one shared in-memory dict into
a real concurrent store, both handled here rather than left as latent bugs:

1. Write path: `lock_for()` gives callers an ambient transaction holding
   `SELECT ... FOR UPDATE` on the game's row. Every other method joins that
   transaction if one is active (via a contextvar), so a `save()` +
   `append_events()` + `record_receipt()` sequence inside one `lock_for`
   block commits together or not at all.
2. Read path: `get()`/`get_by_join_code()` reconstruct one `Game` aggregate
   from 9 tables. Called *without* an ambient transaction (the standalone
   `GET /games/{id}` route, and the pre-lock existence-check reads in
   join_game/submit_command), those 9 SELECTs run inside a single
   `REPEATABLE READ READ ONLY` transaction so they all see one consistent
   snapshot — without it, a commit landing mid-read could reconstruct an
   aggregate that never existed at any single instant (games row at
   version 42, holdings still reflecting version 41). Called *with* an
   ambient transaction (the re-fetch-after-lock case in join_game/
   submit_command), that snapshot isn't needed: the row lock already
   excludes every other writer for this game_id, so a plain sequential read
   on the same connection is already consistent, and psycopg won't let you
   set isolation level mid-transaction anyway.
"""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from gotiate.domain.entities import (
    CancellationReason,
    CloseReason,
    CommandReceipt,
    CommandStatus,
    Game,
    GameConfig,
    GamePhase,
    GamePlayer,
    Holding,
    HoldingZone,
    MarketEntity,
    PendingPickup,
    Pool,
    PoolResolutionReason,
    PoolVisibility,
    Proposal,
    ProposalResolutionReason,
    ResolutionStatus,
    SwapIntent,
)
from gotiate.domain.events import EventType, GameEvent

_ambient_conn: contextvars.ContextVar[psycopg.AsyncConnection | None] = contextvars.ContextVar("_ambient_conn", default=None)


class PostgresGameRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Connection / transaction plumbing
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """The ambient transaction's connection if `lock_for()` is active on
        this task, otherwise a fresh pooled connection for a one-off
        autocommit operation."""
        ambient = _ambient_conn.get()
        if ambient is not None:
            yield ambient
            return
        async with self._pool.connection() as conn:
            yield conn

    @asynccontextmanager
    async def lock_for(self, game_id: str) -> AsyncIterator[None]:
        if _ambient_conn.get() is not None:
            raise RuntimeError("lock_for() called while an ambient transaction is already active — no call site should nest locks")
        async with self._pool.connection() as conn:
            async with conn.transaction():
                # For a brand-new game_id (create_game's case) there's no
                # row yet, so this locks nothing — it's still supplying the
                # same atomic transaction context every other mutating
                # route gets, not protecting a row that doesn't exist.
                await conn.execute("select id from games where id = %s for update", (game_id,))
                token = _ambient_conn.set(conn)
                try:
                    yield
                finally:
                    _ambient_conn.reset(token)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def create(self, game: Game) -> None:
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    insert into games (id, version, next_seq_no, phase, created_at, join_code,
                                        config, lobby_reminder_deadline_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        game.id,
                        game.version,
                        game.next_seq_no,
                        game.phase.value,
                        game.created_at,
                        game.join_code,
                        Json(game.config.model_dump(mode="json")),
                        game.lobby_reminder_deadline_at,
                    ),
                )
                for player in game.players:
                    await self._upsert_player(cur, game.id, player, game.holdings)
                if game.host_player_id is not None:
                    await cur.execute("update games set host_player_id = %s where id = %s", (game.host_player_id, game.id))

    async def save(self, game: Game) -> None:
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    update games set
                        version = %s, next_seq_no = %s, phase = %s, host_player_id = %s,
                        config = %s, lobby_reminder_deadline_at = %s, started_at = %s,
                        max_duration_s = %s, unilateral_cutoff_at = %s, unilateral_window_closed_at = %s,
                        close_threshold = %s, closed_at = %s, close_reason = %s, scored_at = %s,
                        waterline_revealed = %s, cancellation_reason = %s
                    where id = %s
                    """,
                    (
                        game.version,
                        game.next_seq_no,
                        game.phase.value,
                        game.host_player_id,
                        Json(game.config.model_dump(mode="json")),
                        game.lobby_reminder_deadline_at,
                        game.started_at,
                        game.max_duration_s,
                        game.unilateral_cutoff_at,
                        game.unilateral_window_closed_at,
                        game.close_threshold,
                        game.closed_at,
                        game.close_reason.value if game.close_reason else None,
                        game.scored_at,
                        game.waterline_revealed,
                        game.cancellation_reason.value if game.cancellation_reason else None,
                        game.id,
                    ),
                )
                for player in game.players:
                    await self._upsert_player(cur, game.id, player, game.holdings)
                for entity in game.market.values():
                    # theme_set_id/version live on game.config, not on
                    # MarketEntity itself — a denormalization footgun caught
                    # during design, not obvious from the Pydantic model.
                    await cur.execute(
                        """
                        insert into market_entities (game_id, entity_id, theme_set_id, theme_set_version, position)
                        values (%s, %s, %s, %s, %s)
                        on conflict (game_id, entity_id) do update set position = excluded.position
                        """,
                        (game.id, entity.entity_id, game.config.theme_set_id, game.config.theme_set_version, entity.position),
                    )
                for proposal in game.proposals.values():
                    await cur.execute(
                        """
                        insert into proposals (id, game_id, entity_a, entity_b, initiator_player_id,
                                                status, resolved_at_seq_no, resolved_by_player_id, resolution_reason)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        on conflict (id) do update set
                            status = excluded.status, resolved_at_seq_no = excluded.resolved_at_seq_no,
                            resolved_by_player_id = excluded.resolved_by_player_id,
                            resolution_reason = excluded.resolution_reason
                        """,
                        (
                            proposal.proposal_id,
                            game.id,
                            proposal.swap.entity_a,
                            proposal.swap.entity_b,
                            proposal.swap.initiator_player_id,
                            proposal.status.value,
                            proposal.resolved_at_seq_no,
                            proposal.resolved_by_player_id,
                            proposal.resolution_reason.value if proposal.resolution_reason else None,
                        ),
                    )
                for pool in game.pools.values():
                    await cur.execute(
                        """
                        insert into pools (id, game_id, base_proposal_id, initiator_player_id, visibility,
                                           status, resolved_at_seq_no, resolved_by_player_id, resolution_reason)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        on conflict (id) do update set
                            status = excluded.status, resolved_at_seq_no = excluded.resolved_at_seq_no,
                            resolved_by_player_id = excluded.resolved_by_player_id,
                            resolution_reason = excluded.resolution_reason
                        """,
                        (
                            pool.pool_id,
                            game.id,
                            pool.base_proposal_id,
                            pool.swap.initiator_player_id,
                            pool.visibility.value,
                            pool.status.value,
                            pool.resolved_at_seq_no,
                            pool.resolved_by_player_id,
                            pool.resolution_reason.value if pool.resolution_reason else None,
                        ),
                    )
                    await cur.execute(
                        """
                        insert into pool_contents (pool_id, game_id, entity_c, entity_d)
                        values (%s, %s, %s, %s)
                        on conflict (pool_id) do update set entity_c = excluded.entity_c, entity_d = excluded.entity_d
                        """,
                        (pool.pool_id, game.id, pool.swap.entity_a, pool.swap.entity_b),
                    )
                for holding in game.holdings.values():
                    await cur.execute(
                        """
                        insert into holdings (id, game_id, entity_id, owner_player_id, zone,
                                               revealed_to_owner, revealed_at_seq_no)
                        values (%s, %s, %s, %s, %s, %s, %s)
                        on conflict (id) do update set
                            owner_player_id = excluded.owner_player_id, zone = excluded.zone,
                            revealed_to_owner = excluded.revealed_to_owner,
                            revealed_at_seq_no = excluded.revealed_at_seq_no
                        """,
                        (
                            holding.holding_id,
                            game.id,
                            holding.entity_id,
                            holding.owner_player_id,
                            holding.zone.value,
                            holding.revealed_to_owner,
                            holding.revealed_at_seq_no,
                        ),
                    )
                if game.waterline_entity_id is not None:
                    await cur.execute(
                        """
                        insert into waterline (game_id, entity_id) values (%s, %s)
                        on conflict (game_id) do update set entity_id = excluded.entity_id
                        """,
                        (game.id, game.waterline_entity_id),
                    )

    async def _upsert_player(self, cur: psycopg.AsyncCursor, game_id: str, player: GamePlayer, holdings: dict[str, Holding]) -> None:
        # reserve_count_remaining has no corresponding GamePlayer field —
        # it's derived today only in projections.py, and the schema comment
        # says as much ("FastAPI's obligation to keep in sync"). Recomputed
        # here from the same holdings collection every write.
        reserve_count_remaining = sum(
            1 for h in holdings.values() if h.owner_player_id == player.game_player_id and h.zone == HoldingZone.RESERVE_UNREVEALED
        )
        # display_name stored as text, deliberately not a name_seed_id FK --
        # player_name_seeds is reference/catalog data, and a game's roster
        # must stay frozen at assignment time even if a seed name is later
        # renamed or retired (is_active). Same reasoning theme_entities
        # versioning already exists for elsewhere in this schema.
        await cur.execute(
            """
            insert into game_players (id, game_id, seat, display_name, is_golden_name, influence_available,
                                       influence_committed, influence_spent, reserve_count_remaining)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (id) do update set
                seat = excluded.seat, display_name = excluded.display_name,
                is_golden_name = excluded.is_golden_name,
                influence_available = excluded.influence_available,
                influence_committed = excluded.influence_committed,
                influence_spent = excluded.influence_spent,
                reserve_count_remaining = excluded.reserve_count_remaining
            """,
            (
                player.game_player_id,
                game_id,
                player.seat,
                player.display_name,
                player.is_golden_name,
                player.influence_available,
                player.influence_committed,
                player.influence_spent,
                reserve_count_remaining,
            ),
        )
        await cur.execute(
            """
            insert into game_player_private (game_player_id, game_id, auth_user_id, ready_to_close, pending_pickup)
            values (%s, %s, %s, %s, %s)
            on conflict (game_player_id) do update set
                ready_to_close = excluded.ready_to_close,
                pending_pickup = excluded.pending_pickup
            """,
            (
                player.game_player_id,
                game_id,
                player.auth_user_id,
                player.ready_to_close,
                Json(player.pending_pickup.model_dump(mode="json")) if player.pending_pickup else None,
            ),
        )

    async def append_events(self, events: list[GameEvent]) -> None:
        if not events:
            return
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                for event in events:
                    await cur.execute(
                        """
                        insert into event_ledger (game_id, seq_no, type, actor_game_player_id, payload, created_at)
                        values (%s, %s, %s, %s, %s, %s)
                        """,
                        (event.game_id, event.seq_no, event.type.value, event.actor_game_player_id, Json(event.payload), event.created_at),
                    )

    async def record_receipt(self, receipt: CommandReceipt) -> None:
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    insert into command_receipts (game_id, command_id, actor_game_player_id, command_type,
                                                    expected_game_version, received_at, resulting_game_version,
                                                    status, error_code, error_message,
                                                    result_event_seq_start, result_event_seq_end)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (game_id, command_id) do nothing
                    """,
                    (
                        receipt.game_id,
                        receipt.command_id,
                        receipt.actor_game_player_id,
                        receipt.command_type,
                        receipt.expected_game_version,
                        receipt.received_at,
                        receipt.resulting_game_version,
                        receipt.status.value,
                        receipt.error_code,
                        receipt.error_message,
                        receipt.result_event_seq_start,
                        receipt.result_event_seq_end,
                    ),
                )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, game_id: str) -> Game | None:
        return await self._load("id = %s", game_id)

    async def get_by_join_code(self, join_code: str) -> Game | None:
        return await self._load("join_code = %s", join_code)

    async def _load(self, where: str, param: str) -> Game | None:
        ambient = _ambient_conn.get()
        if ambient is not None:
            # Already inside lock_for's transaction, which holds FOR UPDATE
            # on this game's row — no concurrent writer can be touching any
            # of this game's rows right now, so a plain sequential read on
            # this same connection is already consistent. Also can't set
            # isolation_level/read_only here even if we wanted to: psycopg
            # only allows that outside an active transaction.
            return await self._load_rows(ambient, where, param)

        async with self._pool.connection() as conn:
            await conn.set_isolation_level(psycopg.IsolationLevel.REPEATABLE_READ)
            await conn.set_read_only(True)
            try:
                async with conn.transaction():
                    return await self._load_rows(conn, where, param)
            finally:
                # These are sticky connection-level settings, not scoped to
                # the transaction — must reset before this connection goes
                # back to the pool, or the next unrelated use of it (a
                # write) would fail against a read-only connection.
                await conn.set_isolation_level(None)
                await conn.set_read_only(None)

    async def _load_rows(self, conn: psycopg.AsyncConnection, where: str, param: str) -> Game | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"select * from games where {where}", (param,))  # noqa: S608 — where is one of two fixed literals, never user input
            game_row = await cur.fetchone()
            if game_row is None:
                return None
            game_id = str(game_row["id"])

            await cur.execute("select * from game_players where game_id = %s order by seat", (game_id,))
            player_rows = await cur.fetchall()

            await cur.execute("select * from game_player_private where game_id = %s", (game_id,))
            private_rows = {str(r["game_player_id"]): r for r in await cur.fetchall()}

            await cur.execute("select * from market_entities where game_id = %s", (game_id,))
            market_rows = await cur.fetchall()

            await cur.execute("select * from proposals where game_id = %s", (game_id,))
            proposal_rows = await cur.fetchall()

            await cur.execute("select * from pools where game_id = %s", (game_id,))
            pool_rows = await cur.fetchall()

            await cur.execute("select * from pool_contents where game_id = %s", (game_id,))
            pool_content_rows = {str(r["pool_id"]): r for r in await cur.fetchall()}

            await cur.execute("select * from holdings where game_id = %s", (game_id,))
            holding_rows = await cur.fetchall()

            await cur.execute("select * from waterline where game_id = %s", (game_id,))
            waterline_row = await cur.fetchone()

        return _to_game(game_row, player_rows, private_rows, market_rows, proposal_rows, pool_rows, pool_content_rows, holding_rows, waterline_row)

    async def get_receipt(self, game_id: str, command_id: str) -> CommandReceipt | None:
        async with self._connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("select * from command_receipts where game_id = %s and command_id = %s", (game_id, command_id))
                row = await cur.fetchone()
        if row is None:
            return None
        return CommandReceipt(
            command_id=row["command_id"],
            game_id=str(row["game_id"]),
            actor_game_player_id=str(row["actor_game_player_id"]) if row["actor_game_player_id"] else None,
            command_type=row["command_type"],
            expected_game_version=row["expected_game_version"],
            received_at=row["received_at"],
            resulting_game_version=row["resulting_game_version"],
            status=CommandStatus(row["status"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            result_event_seq_start=row["result_event_seq_start"],
            result_event_seq_end=row["result_event_seq_end"],
        )

    async def find_active_game_hosted_by(self, auth_user_id: str) -> Game | None:
        async with self._connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select g.id from games g
                    join game_players gp on gp.id = g.host_player_id
                    join game_player_private gpp on gpp.game_player_id = gp.id
                    where gpp.auth_user_id = %s and g.phase not in ('SCORED', 'CANCELLED')
                    limit 1
                    """,
                    (auth_user_id,),
                )
                row = await cur.fetchone()
        return await self.get(str(row["id"])) if row else None

    async def get_events(self, game_id: str, since_seq: int = 0) -> list[GameEvent]:
        async with self._connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "select * from event_ledger where game_id = %s and seq_no >= %s order by seq_no",
                    (game_id, since_seq),
                )
                rows = await cur.fetchall()
        return [
            GameEvent(
                game_id=str(r["game_id"]),
                seq_no=r["seq_no"],
                type=EventType(r["type"]),
                actor_game_player_id=str(r["actor_game_player_id"]) if r["actor_game_player_id"] else None,
                payload=r["payload"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


def _to_game(
    game_row: DictRow,
    player_rows: list[DictRow],
    private_rows: dict[str, DictRow],
    market_rows: list[DictRow],
    proposal_rows: list[DictRow],
    pool_rows: list[DictRow],
    pool_content_rows: dict[str, DictRow],
    holding_rows: list[DictRow],
    waterline_row: DictRow | None,
) -> Game:
    players: list[GamePlayer] = []
    for pr in player_rows:
        gpid = str(pr["id"])
        priv: dict[str, Any] = private_rows.get(gpid, {})
        pending_pickup_json = priv.get("pending_pickup")
        players.append(
            GamePlayer(
                game_player_id=gpid,
                # auth_user_id is nullable in game_player_private (ON DELETE
                # SET NULL) for an account-deletion path that doesn't exist
                # yet — no UI reaches it today, so this doesn't try to model
                # a null identity beyond not crashing on one.
                auth_user_id=str(priv["auth_user_id"]) if priv.get("auth_user_id") is not None else "",
                seat=pr["seat"],
                display_name=pr["display_name"],
                is_golden_name=pr["is_golden_name"],
                influence_available=pr["influence_available"],
                influence_committed=pr["influence_committed"],
                influence_spent=pr["influence_spent"],
                ready_to_close=priv.get("ready_to_close", False),
                pending_pickup=PendingPickup.model_validate(pending_pickup_json) if pending_pickup_json else None,
            )
        )

    market: dict[str, MarketEntity] = {}
    for mr in market_rows:
        # entity_id and theme_key coincide by construction (engine.py) —
        # the schema only stores one column for what the domain model keeps
        # as two distinct fields.
        market[mr["entity_id"]] = MarketEntity(entity_id=mr["entity_id"], theme_key=mr["entity_id"], position=mr["position"])

    proposals: dict[str, Proposal] = {}
    for pr in proposal_rows:
        pid = str(pr["id"])
        proposals[pid] = Proposal(
            proposal_id=pid,
            swap=SwapIntent(entity_a=pr["entity_a"], entity_b=pr["entity_b"], initiator_player_id=str(pr["initiator_player_id"])),
            status=ResolutionStatus(pr["status"]),
            resolved_at_seq_no=pr["resolved_at_seq_no"],
            resolved_by_player_id=str(pr["resolved_by_player_id"]) if pr["resolved_by_player_id"] else None,
            resolution_reason=ProposalResolutionReason(pr["resolution_reason"]) if pr["resolution_reason"] else None,
        )

    pools: dict[str, Pool] = {}
    for por in pool_rows:
        poid = str(por["id"])
        contents = pool_content_rows.get(poid, {})
        pools[poid] = Pool(
            pool_id=poid,
            base_proposal_id=str(por["base_proposal_id"]),
            swap=SwapIntent(
                entity_a=contents.get("entity_c", ""), entity_b=contents.get("entity_d", ""), initiator_player_id=str(por["initiator_player_id"])
            ),
            visibility=PoolVisibility(por["visibility"]),
            status=ResolutionStatus(por["status"]),
            resolved_at_seq_no=por["resolved_at_seq_no"],
            resolved_by_player_id=str(por["resolved_by_player_id"]) if por["resolved_by_player_id"] else None,
            resolution_reason=PoolResolutionReason(por["resolution_reason"]) if por["resolution_reason"] else None,
        )

    holdings: dict[str, Holding] = {}
    for hr in holding_rows:
        hid = str(hr["id"])
        holdings[hid] = Holding(
            holding_id=hid,
            entity_id=hr["entity_id"],
            owner_player_id=str(hr["owner_player_id"]) if hr["owner_player_id"] else None,
            zone=HoldingZone(hr["zone"]),
            revealed_to_owner=hr["revealed_to_owner"],
            revealed_at_seq_no=hr["revealed_at_seq_no"],
        )

    return Game(
        id=str(game_row["id"]),
        version=game_row["version"],
        next_seq_no=game_row["next_seq_no"],
        phase=GamePhase(game_row["phase"]),
        created_at=game_row["created_at"],
        join_code=game_row["join_code"],
        host_player_id=str(game_row["host_player_id"]) if game_row["host_player_id"] else None,
        config=GameConfig.model_validate(game_row["config"]),
        lobby_reminder_deadline_at=game_row["lobby_reminder_deadline_at"],
        started_at=game_row["started_at"],
        max_duration_s=game_row["max_duration_s"],
        unilateral_cutoff_at=game_row["unilateral_cutoff_at"],
        unilateral_window_closed_at=game_row["unilateral_window_closed_at"],
        close_threshold=game_row["close_threshold"],
        closed_at=game_row["closed_at"],
        close_reason=CloseReason(game_row["close_reason"]) if game_row["close_reason"] else None,
        scored_at=game_row["scored_at"],
        cancellation_reason=CancellationReason(game_row["cancellation_reason"]) if game_row["cancellation_reason"] else None,
        market=market,
        players=players,
        holdings=holdings,
        proposals=proposals,
        pools=pools,
        waterline_entity_id=waterline_row["entity_id"] if waterline_row else None,
        waterline_revealed=game_row["waterline_revealed"],
    )


def create_pool(database_url: str, *, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool:
    """Constructed with open=False -- constructing a pool is synchronous
    (only `.open()` is a coroutine), so this can run at import time; opening
    is main.py's lifespan's job, since `app = ...` must exist synchronously
    at import time for uvicorn to pick it up, but a query against an
    unopened pool would fail, so it must be open before any request is
    served."""
    pool = AsyncConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True},
        reset=_reset_connection,
    )
    return pool


async def _reset_connection(conn: psycopg.AsyncConnection) -> None:
    # Defense in depth alongside the explicit reset in _load: a connection
    # should never return to the pool with a non-default isolation level or
    # read_only flag still set.
    await conn.set_isolation_level(None)
    await conn.set_read_only(None)
