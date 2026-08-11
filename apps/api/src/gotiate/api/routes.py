"""Thin REST layer: parse, resolve the caller to a GamePlayer, call the
engine, persist. All rules live in domain/engine.py — nothing here decides
legality. Lobby formation (CREATE_GAME/JOIN_GAME) gets dedicated routes since
the actor isn't seated yet; everything else goes through the generic command
envelope, matching domain model §04."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from gotiate.api.deps import get_auth_user_id, get_player_name_repository, get_repository
from gotiate.api.rate_limit import limiter
from gotiate.api.schemas import CommandRequest, CreateGameRequest, GameSummary, JoinGameRequest, PlayerNameOffer
from gotiate.domain import engine
from gotiate.domain.entities import CommandReceipt, CommandStatus
from gotiate.domain.errors import DomainError, IllegalCommandError, StaleVersionError
from gotiate.domain.projections import PlayerAudience, PublicAudience, project
from gotiate.persistence.player_names import NoAvailableNameError, PlayerNameRepository
from gotiate.persistence.repository import GameRepository

_INVALID_NAME_DETAIL = {"error_code": "invalid_display_name", "message": "choose a name from the provided list"}
_NO_NAMES_AVAILABLE_DETAIL = {"error_code": "no_names_available", "message": "try again in a moment"}

router = APIRouter(prefix="/games", tags=["games"])
player_names_router = APIRouter(prefix="/player-names", tags=["player-names"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("", response_model=GameSummary)
@limiter.limit("5/hour")
async def create_game(
    request: Request,
    body: CreateGameRequest,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
    name_repo: PlayerNameRepository = Depends(get_player_name_repository),
) -> GameSummary:
    # No free-text display names, ever -- display_name must be one of the
    # curated placeholders (player_name_seeds), never open text. This is
    # the entire content-moderation story: nothing here needs a filter
    # because nothing here was ever typed by a user.
    if not await name_repo.is_valid_name(body.display_name):
        raise HTTPException(status_code=422, detail=_INVALID_NAME_DETAIL)

    # One in-flight hosted game per user — deliberately not just a time-
    # windowed rate limit, so spacing requests out doesn't get around it.
    existing_game = await repo.find_active_game_hosted_by(auth_user_id)
    if existing_game is not None:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "active_game_exists", "message": "finish or end your current game before starting another", "game_id": existing_game.id},
        )

    game, events = engine.create_game(
        actor_auth_user_id=auth_user_id, display_name=body.display_name, now=_now(), expected_player_count=body.expected_player_count
    )
    # No existing row to lock for a brand-new game id -- lock_for() here is
    # supplying the same atomic transaction context every other mutating
    # route gets (create + append_events land together or not at all), not
    # protecting a row that doesn't exist yet.
    async with repo.lock_for(game.id):
        await repo.create(game)
        await repo.append_events(events)
    await name_repo.mark_name_used(body.display_name)
    assert game.host_player_id is not None
    return GameSummary(game_id=game.id, join_code=game.join_code, game_player_id=game.host_player_id)


@router.post("/join", response_model=GameSummary)
@limiter.limit("10/5minutes")
async def join_game(
    request: Request,
    body: JoinGameRequest,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
    name_repo: PlayerNameRepository = Depends(get_player_name_repository),
) -> GameSummary:
    if not await name_repo.is_valid_name(body.display_name):
        raise HTTPException(status_code=422, detail=_INVALID_NAME_DETAIL)

    game = await repo.get_by_join_code(body.join_code.upper())
    if game is None:
        raise HTTPException(status_code=404, detail="no game with that code")

    async with repo.lock_for(game.id):
        # Re-fetch under the lock -- the read above is existence-check-only.
        # A Postgres get() returns a fresh snapshot every call (unlike the
        # in-memory repository, where it's the same live object reference),
        # so without this the engine could validate expected_version against
        # a game.version that's already stale by the time the lock lands.
        game = await repo.get(game.id)
        if game is None:
            raise HTTPException(status_code=404, detail="no game with that code")
        # Checked against this game's own already-loaded players, not
        # player_name_seeds -- that table only tracks the global pool, not
        # who's using what name in which game right now.
        if any(p.display_name == body.display_name for p in game.players):
            raise HTTPException(status_code=409, detail={"error_code": "name_taken", "message": "that name is already taken in this game — pick another"})
        try:
            player, events = engine.join_game(game, actor_auth_user_id=auth_user_id, display_name=body.display_name, now=_now())
        except DomainError as exc:
            raise HTTPException(status_code=409, detail={"error_code": exc.error_code, "message": str(exc)}) from exc
        await repo.save(game)
        await repo.append_events(events)
    await name_repo.mark_name_used(body.display_name)

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
        # Re-fetch under the lock, same reasoning as join_game -- and
        # re-derive `player` from this fresh game, not the pre-lock one,
        # since it's the same class of staleness (a player's
        # ready_to_close/influence could equally be stale between the two
        # reads, even though today's 403 check below doesn't depend on it).
        game = await repo.get(game_id)
        if game is None:
            raise HTTPException(status_code=404, detail="no such game")

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


async def _names_already_in_game(repo: GameRepository, join_code: str | None) -> set[str]:
    # join_code is only present when offering a name for the *join* flow --
    # create_game has no roster yet to collide with. A bad/expired code
    # just means an empty exclude set, not an error; the actual join
    # attempt (routes above) is where a bad code gets a real 404.
    if not join_code:
        return set()
    game = await repo.get_by_join_code(join_code.upper())
    return {p.display_name for p in game.players} if game else set()


@player_names_router.get("/initial", response_model=PlayerNameOffer)
@limiter.limit("5/hour")
async def offer_initial_name(
    request: Request,
    join_code: str | None = None,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
    name_repo: PlayerNameRepository = Depends(get_player_name_repository),
) -> PlayerNameOffer:
    # Golden-eligible -- strictly rate-limited (unlike reroll below) since
    # each call is an independent 1/500 draw and there's no game/player row
    # yet at this point to pin "you already got your one initial roll" to.
    exclude = await _names_already_in_game(repo, join_code)
    try:
        name, is_golden = await name_repo.offer_initial_name(exclude=exclude)
    except NoAvailableNameError:
        raise HTTPException(status_code=503, detail=_NO_NAMES_AVAILABLE_DETAIL)
    return PlayerNameOffer(name=name, is_golden=is_golden)


@player_names_router.get("/reroll", response_model=PlayerNameOffer)
async def offer_reroll_name(
    request: Request,
    exclude: str,
    join_code: str | None = None,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
    name_repo: PlayerNameRepository = Depends(get_player_name_repository),
) -> PlayerNameOffer:
    # Never golden -- no extra rate limit beyond the global default, since
    # there's nothing here worth farming.
    already_taken = await _names_already_in_game(repo, join_code)
    already_taken.add(exclude)
    try:
        name = await name_repo.offer_reroll_name(exclude=already_taken)
    except NoAvailableNameError:
        raise HTTPException(status_code=503, detail=_NO_NAMES_AVAILABLE_DETAIL)
    return PlayerNameOffer(name=name, is_golden=False)
