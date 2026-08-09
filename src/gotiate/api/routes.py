"""Thin REST layer: parse, resolve the caller to a GamePlayer, call the
engine, persist. All rules live in domain/engine.py — nothing here decides
legality. Lobby formation (CREATE_GAME/JOIN_GAME) gets dedicated routes since
the actor isn't seated yet; everything else goes through the generic command
envelope, matching domain model §04."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from gotiate.api.deps import get_auth_user_id, get_repository
from gotiate.api.schemas import CommandRequest, CreateGameRequest, GameSummary, JoinGameRequest
from gotiate.domain import engine
from gotiate.domain.entities import CommandReceipt, CommandStatus
from gotiate.domain.errors import DomainError, IllegalCommandError, StaleVersionError
from gotiate.domain.projections import PlayerAudience, PublicAudience, project
from gotiate.persistence.repository import GameRepository

router = APIRouter(prefix="/games", tags=["games"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("", response_model=GameSummary)
async def create_game(
    body: CreateGameRequest,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
) -> GameSummary:
    game, events = engine.create_game(actor_auth_user_id=auth_user_id, display_name=body.display_name, now=_now())
    await repo.create(game)
    await repo.append_events(events)
    assert game.host_player_id is not None
    return GameSummary(game_id=game.id, join_code=game.join_code, game_player_id=game.host_player_id)


@router.post("/join", response_model=GameSummary)
async def join_game(
    body: JoinGameRequest,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
) -> GameSummary:
    game = await repo.get_by_join_code(body.join_code.upper())
    if game is None:
        raise HTTPException(status_code=404, detail="no game with that code")

    async with repo.lock_for(game.id):
        try:
            player, events = engine.join_game(game, actor_auth_user_id=auth_user_id, display_name=body.display_name, now=_now())
        except DomainError as exc:
            raise HTTPException(status_code=409, detail={"error_code": exc.error_code, "message": str(exc)}) from exc
        await repo.save(game)
        await repo.append_events(events)

    return GameSummary(game_id=game.id, join_code=game.join_code, game_player_id=player.game_player_id)


@router.get("/{game_id}")
async def get_game(
    game_id: str,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
) -> dict:
    game = await repo.get(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="no such game")
    player = game.player_by_auth_id(auth_user_id)
    audience = PlayerAudience(player.game_player_id) if player else PublicAudience()
    return project(game, audience)


@router.post("/{game_id}/commands")
async def submit_command(
    game_id: str,
    body: CommandRequest,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
) -> dict:
    game = await repo.get(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="no such game")

    async with repo.lock_for(game_id):
        existing = await repo.get_receipt(game_id, body.command_id)
        if existing is not None:
            # Idempotent replay — same command_id, don't reprocess (§08).
            return {"status": existing.status, "resulting_version": existing.resulting_game_version}

        player = game.player_by_auth_id(auth_user_id)
        if player is None:
            raise HTTPException(status_code=403, detail="you are not seated in this game")

        now = _now()
        try:
            events = engine.handle_command(
                game,
                command_type=body.type,
                payload=body.payload,
                actor_game_player_id=player.game_player_id,
                expected_version=body.expected_version,
                now=now,
            )
        except (IllegalCommandError, StaleVersionError) as exc:
            # Partial mutations from apply_due_time_transitions still happened
            # and are already reflected in `game` (mutated in place) — persist
            # them even though the submitted command itself was rejected.
            if exc.partial_events:
                await repo.save(game)
                await repo.append_events(exc.partial_events)
            await repo.record_receipt(
                CommandReceipt(
                    command_id=body.command_id,
                    game_id=game_id,
                    actor_game_player_id=player.game_player_id,
                    command_type=body.type,
                    expected_game_version=body.expected_version,
                    received_at=now,
                    status=CommandStatus.REJECTED_ILLEGAL if isinstance(exc, IllegalCommandError) else CommandStatus.REJECTED_STALE_VERSION,
                    error_code=exc.error_code,
                    error_message=str(exc),
                )
            )
            raise HTTPException(status_code=409, detail={"error_code": exc.error_code, "message": str(exc)}) from exc

        await repo.save(game)
        await repo.append_events(events)
        await repo.record_receipt(
            CommandReceipt(
                command_id=body.command_id,
                game_id=game_id,
                actor_game_player_id=player.game_player_id,
                command_type=body.type,
                expected_game_version=body.expected_version,
                received_at=now,
                resulting_game_version=game.version,
                status=CommandStatus.APPLIED,
                result_event_seq_start=events[0].seq_no if events else None,
                result_event_seq_end=events[-1].seq_no if events else None,
            )
        )

    return project(game, PlayerAudience(player.game_player_id))
